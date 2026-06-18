import sys
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import requests


@dataclass
class StreamValidationResult:
    is_playable: bool
    status_code: int = 0
    content_type: str = ""
    final_url: str = ""
    reason: str = ""


class StreamValidator:
    def __init__(self, timeout: int = 10, debug: bool = False):
        self.timeout = timeout
        self.debug = debug

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Icy-MetaData": "1",
        })

    def log(self, message: str):
        if self.debug:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[{ts}] {message}", file=sys.stderr)

    def validate(self, url: str, _in_fallback: bool = False) -> StreamValidationResult:
        if not url:
            return StreamValidationResult(
                is_playable=False,
                reason="Empty URL."
            )

        try:
            self.log(f"[VALIDATE] Checking stream candidate: {url}")

            connect_timeout = min(self.timeout, 10)

            # Strategy: HEAD first (no body transfer, safe for live streams).
            # Fall back to GET with stream=True only if HEAD is not supported
            # or returns an ambiguous result with empty content-type.
            response = None
            used_method = "HEAD"

            try:
                response = self.session.head(
                    url,
                    timeout=(connect_timeout, 5),
                    allow_redirects=True
                )

                head_status = response.status_code
                head_ct = response.headers.get("Content-Type", "").lower()

                # If HEAD is not supported or rejected, fall through to GET.
                # 405 = Method Not Allowed, 400 = Bad Request.
                # Some Icecast/Shoutcast servers return 400 on HEAD.
                if head_status in (400, 405, 502):
                    response.close()
                    response = None

                # If HEAD succeeded but content-type is empty or vague,
                # try a brief GET to get proper headers from the stream
                elif head_status in (200, 206) and not head_ct:
                    response.close()
                    response = None

            except Exception:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                response = None

            if response is None:
                used_method = "GET"
                response = self.session.get(
                    url,
                    timeout=(connect_timeout, 5),
                    allow_redirects=True,
                    stream=True
                )

            try:
                status_code = response.status_code
                content_type = response.headers.get("Content-Type", "")
                final_url = response.url

                is_audio = self._looks_like_audio_response(
                    status_code=status_code,
                    content_type=content_type,
                    final_url=final_url,
                    headers=response.headers
                )

                self.log(
                    f"[VALIDATE] Status={status_code}, "
                    f"ContentType={content_type}, "
                    f"FinalUrl={final_url}, "
                    f"Playable={is_audio} [{used_method}]"
                )

                if is_audio:
                    return StreamValidationResult(
                        is_playable=True,
                        status_code=status_code,
                        content_type=content_type,
                        final_url=final_url,
                        reason="Looks like audio stream."
                    )

                # If we got HTML from a bare IP or non-standard port,
                # it might be a Shoutcast/Icecast status page — try standard stream paths.
                lower_ct = content_type.lower()
                if "text/html" in lower_ct and status_code == 200:
                    parsed_url = urlparse(final_url)
                    host = parsed_url.netloc.lower()
                    import re as _re
                    is_bare_ip = bool(_re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+[.][0-9]+(:[0-9]+)?", host))
                    # Also trigger for non-standard ports (Icecast/Shoutcast on :8000, :8080 etc.)
                    port_match = _re.search(r":(\d+)$", host)
                    has_nonstandard_port = (
                        port_match is not None and
                        int(port_match.group(1)) not in (80, 443, 8443)
                    )
                    if (is_bare_ip or has_nonstandard_port) and not _in_fallback:
                        self.log(f"[VALIDATE] HTML from Shoutcast/Icecast host, trying stream fallback: {final_url}")
                        fallback = self._try_shoutcast_icecast_fallback(final_url)
                        if fallback is not None:
                            return fallback

                # Check if response is an M3U playlist (even with octet-stream content-type)
                lower_ct = content_type.lower() if content_type else ""
                is_m3u_ct = any(t in lower_ct for t in (
                    "mpegurl", "x-scpls", "x-mpegurl", "octet-stream", "audio/x-"
                ))
                is_m3u_url = any(url.lower().endswith(ext) for ext in (".m3u", ".m3u8", ".pls"))

                if (is_m3u_ct or is_m3u_url) and response is not None:
                    try:
                        text = response.text
                        m3u_result = self._try_parse_m3u_response(url, text)
                        if m3u_result is not None:
                            return m3u_result
                    except Exception:
                        pass

                return StreamValidationResult(
                    is_playable=False,
                    status_code=status_code,
                    content_type=content_type,
                    final_url=final_url,
                    reason="Response does not look like audio stream."
                )

            finally:
                try:
                    response.close()
                except Exception:
                    pass

        except Exception as ex:
            ex_str = str(ex)
            ex_type = type(ex).__name__

            # ICY 200 OK — Icecast/SHOUTcast non-standard HTTP response.
            # requests raises BadStatusLine("ICY 200 OK") because it's not
            # a valid HTTP status line. But it means the stream IS alive.
            if "ICY 200 OK" in ex_str or "ICY 200" in ex_str:
                self.log(
                    f"[VALIDATE] ICY/Icecast stream detected (non-standard HTTP). "
                    f"Treating as playable. Url: {url}"
                )
                return StreamValidationResult(
                    is_playable=True,
                    status_code=200,
                    content_type="audio/mpeg",
                    final_url=url,
                    reason="ICY 200 OK — Icecast/SHOUTcast stream."
                )

            self.log(f"[VALIDATE] Failed: {url} | {ex_type}: {ex}")

            return StreamValidationResult(
                is_playable=False,
                reason=str(ex)
            )

    def _try_parse_m3u_response(self, url: str, text: str) -> "StreamValidationResult | None":
        """
        Если ответ содержит M3U плейлист (список http:// URL) —
        парсим и валидируем первый рабочий URL.
        """
        if not text:
            return None

        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        stream_urls = []

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("http://") or line.startswith("https://"):
                stream_urls.append(line)

        if not stream_urls:
            return None

        self.log(f"[VALIDATE] M3U response detected, found {len(stream_urls)} URLs in: {url}")

        for stream_url in stream_urls[:5]:
            result = self.validate(stream_url)
            if result.is_playable:
                self.log(f"[VALIDATE] M3U stream validated: {stream_url}")
                return result

        return None

    def _try_shoutcast_icecast_fallback(self, url: str) -> "StreamValidationResult | None":
        """
        Если URL отдаёт HTML-страницу статуса Shoutcast/Icecast —
        пробуем стандартные пути потоков: /live, /;, /stream, /1 и т.д.
        Возвращает первый рабочий результат или None.
        """
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        base = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

        stream_paths = [
            "/live",
            "/;",
            "/stream",
            "/stream/1",
            "/1",
            "/;stream.mp3",
            "/;stream.aac",
            "/audio",
            "/listen",
        ]

        for path in stream_paths:
            candidate_url = base + path
            try:
                result = self.validate(candidate_url, _in_fallback=True)
                if result.is_playable:
                    self.log(f"[VALIDATE] Shoutcast/Icecast fallback found: {candidate_url}")
                    return result
            except Exception:
                continue

        return None

    def _looks_like_audio_response(
        self,
        status_code: int,
        content_type: str,
        final_url: str,
        headers: dict
    ) -> bool:
        if status_code < 200 or status_code >= 400:
            return False

        lower_content_type = (content_type or "").lower()
        lower_url = (final_url or "").lower()

        has_icy_headers = any(
            key.lower().startswith("icy-")
            for key in headers.keys()
        )

        parsed = urlparse(lower_url)
        path = parsed.path.lower()

        if lower_content_type.startswith("audio/"):
            return True

        if "mpegurl" in lower_content_type:
            return True

        if "application/ogg" in lower_content_type:
            return True

        if "application/vnd.apple.mpegurl" in lower_content_type:
            return True

        if "application/x-mpegurl" in lower_content_type:
            return True

        if has_icy_headers:
            return True

        if path.endswith(".m3u8"):
            return True

        if path.endswith(".m3u"):
            return True

        if path.endswith(".pls"):
            return True

        if path.endswith(".mp3"):
            return True

        if path.endswith(".aac"):
            return True

        if path.endswith(".ogg"):
            return True

        if path.endswith(".opus"):
            return True

        return False