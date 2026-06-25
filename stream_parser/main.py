import argparse
import json
import os
import sys
import traceback
from urllib.parse import urlparse

from stream_parser.models import ParserResult, StreamCandidate
from stream_parser.extractors.generic_static import GenericStaticExtractor
from stream_parser.extractors.browser_network import BrowserNetworkExtractor
from stream_parser.stream_validator import StreamValidator


def _base_dir() -> str:
    """
    Directory where the running program actually lives.
    For a PyInstaller build this is the folder next to stream_parser.exe,
    NOT the temporary _MEIPASS unpack dir — the browser must sit in a
    permanent folder beside the exe, not in a per-run temp directory.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# Make Playwright look for browsers in a "browsers" folder next to the exe
# instead of the per-user profile (%LOCALAPPDATA%\ms-playwright). This makes
# the release self-contained: copy the folder and it runs — no `playwright
# install` needed on the target machine.
# Only forced for the frozen build, so local dev keeps using the normal
# profile-installed browsers. An explicitly set PLAYWRIGHT_BROWSERS_PATH is
# respected and never overwritten.
if getattr(sys, "frozen", False) and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(_base_dir(), "browsers")


def print_json_result(result: ParserResult):
    import sys
    output = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
    # Force UTF-8 output — important for the exe on Windows with a cp1251 console
    sys.stdout.buffer.write((output + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _is_direct_stream_url(url: str) -> bool:
    """
    Checks whether the URL itself is a direct audio stream rather than a
    page with a player. Signals: an audio file extension, or a stream-like
    path on a non-standard port (typical Icecast/Shoutcast).
    """
    if not url:
        return False

    try:
        parsed = urlparse(url.lower())
    except Exception:
        return False

    path = parsed.path

    audio_extensions = (".mp3", ".aac", ".ogg", ".opus", ".flac", ".wav",
                        ".m3u8", ".m3u", ".pls", ".xspf")
    if any(path.endswith(ext) for ext in audio_extensions):
        return True

    # Non-standard port + stream-like path (typical Icecast/Shoutcast)
    port = parsed.port
    if port and port not in (80, 443, 8080, 8443):
        stream_tokens = ("/stream", "/live", "/audio", "/listen",
                         "/icecast", "/shoutcast", "/;", "/1", "/2", "/3")
        if not path or path == "/" or any(path.startswith(t) for t in stream_tokens):
            return True

    return False


def _parse_pls_urls(text: str, base_url: str = "") -> list:
    """Extract stream URLs from a PLS (INI-style) playlist, in entry order.

    PLS is not M3U: it is an INI file whose entries look like
    ``File1=http://host:8000/;``. Returns absolute http(s) URLs only,
    de-duplicated, preserving the FileN ordering.
    """
    if not text:
        return []

    import re
    from urllib.parse import urljoin

    pairs = []
    for line in text.splitlines():
        match = re.match(r"\s*File\s*(\d+)\s*=\s*(.+?)\s*$", line, re.IGNORECASE)
        if match:
            index = int(match.group(1))
            entry = match.group(2).strip()
            if entry:
                pairs.append((index, entry))

    pairs.sort(key=lambda pair: pair[0])

    urls = []
    seen = set()
    for _, entry in pairs:
        if not entry.lower().startswith(("http://", "https://")):
            if base_url:
                entry = urljoin(base_url, entry)
            else:
                continue
        if entry not in seen:
            seen.add(entry)
            urls.append(entry)

    return urls


def _is_container_playlist(url: str, content_type: str = "") -> bool:
    """True if the URL/content is a container playlist that points to a real
    stream inside (.pls / .m3u / .xspf), as opposed to a directly-playable
    stream. HLS manifests (.m3u8 / Apple mpegurl) are intentionally excluded —
    media players open those directly.
    """
    try:
        path = urlparse((url or "").lower()).path
    except Exception:
        path = ""

    if path.endswith(".m3u8"):
        return False

    if path.endswith((".m3u", ".pls", ".xspf")):
        return True

    ct = (content_type or "").lower()
    # PLS content types are unambiguous container playlists (never HLS).
    if "x-scpls" in ct or "pls+xml" in ct:
        return True

    return False


def _cand_get(cand, name, default=None):
    """Read a field from a StreamCandidate without depending on its internal
    attribute naming: prefer its serialized dict (the camelCase JSON keys used
    on output), fall back to a direct attribute lookup.
    """
    try:
        data = cand.to_dict()
        if name in data:
            return data[name]
    except Exception:
        pass
    return getattr(cand, name, default)


def _expand_playlist_candidates(candidates, timeout, debug, diagnostics):
    """Replace any container-playlist candidate (.pls / .m3u / .xspf) with its
    inner stream URL. This runs on the output of BOTH extractors so that a
    discovered playlist (e.g. a Shoutcast listen.pls referenced on the page) is
    resolved to a real stream the media player can open, instead of handing the
    playlist file back as if it were the stream. A playlist that cannot be
    resolved to a playable stream is dropped (it is not usable for playback).
    """
    if not candidates:
        return candidates

    from stream_parser.http_client import HttpClient

    validator = StreamValidator(timeout=timeout, debug=debug)
    http = HttpClient(timeout=timeout, debug=debug)

    expanded = []
    for cand in candidates:
        cand_url = _cand_get(cand, "finalUrl") or _cand_get(cand, "url") or ""
        cand_ct = _cand_get(cand, "contentType", "") or ""

        if not _is_container_playlist(cand_url, cand_ct):
            expanded.append(cand)
            continue

        diagnostics.append(f"Candidate is a container playlist; expanding to inner stream: {cand_url}")

        try:
            playlist_text = http.download_text_safe(cand_url)
        except Exception:
            playlist_text = ""

        path = urlparse(cand_url.lower()).path
        is_pls = (
            path.endswith(".pls")
            or "x-scpls" in cand_ct.lower()
            or "pls+xml" in cand_ct.lower()
            or (bool(playlist_text) and "[playlist]" in playlist_text.lower())
        )

        resolved = None  # tuple (stream_url, status_code, content_type)

        if is_pls:
            for inner in _parse_pls_urls(playlist_text, base_url=cand_url):
                v = validator.validate(inner)
                if v.is_playable:
                    resolved = (v.final_url or inner, v.status_code, v.content_type)
                    break
        else:
            m3u_result = validator._try_parse_m3u_response(cand_url, playlist_text)
            if m3u_result and m3u_result.is_playable:
                resolved = (
                    m3u_result.final_url or cand_url,
                    m3u_result.status_code,
                    m3u_result.content_type,
                )

        if resolved:
            stream_url, status_code, content_type = resolved
            expanded.append(StreamCandidate(
                url=stream_url,
                title=_cand_get(cand, "title", "") or "",
                source=_cand_get(cand, "source", "direct_url") or "direct_url",
                confidence=_cand_get(cand, "confidence", 95) or 95,
                reason="Expanded from playlist candidate. Validated as playable audio stream.",
                originUrl=_cand_get(cand, "originUrl", "") or "",
                originType="playlist",
                originalUrl=stream_url,
                stableUrl=stream_url,
                originAction=_cand_get(cand, "originAction", "") or "",
                qualityHint=_cand_get(cand, "qualityHint", "unknown") or "unknown",
                qualityScore=_cand_get(cand, "qualityScore", 0) or 0,
                isTemporary=False,
                requiresFreshDiscovery=False,
                isPlayable=True,
                httpStatusCode=status_code,
                contentType=content_type,
                finalUrl=stream_url
            ))
            diagnostics.append(f"  Playlist expanded -> {stream_url}")
        else:
            diagnostics.append(f"  Playlist expansion failed; dropping playlist candidate: {cand_url}")

    return expanded


def main():
    parser = argparse.ArgumentParser(description="Universal stream URL parser for RadioApp.")
    parser.add_argument("--url", required=True, help="Radio station page URL.")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP/browser timeout in seconds.")
    parser.add_argument("--debug", action="store_true", help="Print debug logs to stderr.")
    args = parser.parse_args()

    diagnostics = []

    try:
        # Direct audio stream link — validate without running the extractors
        if _is_direct_stream_url(args.url):
            # A container playlist (.pls / .m3u / .xspf) is NOT itself a stream —
            # it points to one or more real stream URLs inside. Parse it and
            # return the inner stream. (.m3u8 is an HLS manifest that players open
            # directly, so it is intentionally NOT treated as a container here and
            # falls through to direct validation below.)
            parsed_input = urlparse(args.url.lower())
            is_container_playlist = any(
                parsed_input.path.endswith(ext) for ext in (".m3u", ".pls", ".xspf")
            )
            if is_container_playlist:
                diagnostics.append("Input URL is a container playlist. Downloading and parsing.")
                from stream_parser.http_client import HttpClient
                http = HttpClient(timeout=args.timeout, debug=args.debug)
                playlist_text = http.download_text_safe(args.url)
                validator = StreamValidator(timeout=args.timeout, debug=args.debug)

                # PLS is INI-style (File1=...), which the M3U parser cannot read;
                # detect it by extension or the [playlist] header and parse it
                # explicitly. Everything else goes through the M3U parser.
                is_pls = parsed_input.path.endswith(".pls") or (
                    bool(playlist_text) and "[playlist]" in playlist_text.lower()
                )

                inner_candidate = None

                if is_pls:
                    pls_urls = _parse_pls_urls(playlist_text, base_url=args.url)
                    diagnostics.append(f"PLS playlist parsed. Entries found: {len(pls_urls)}.")
                    for entry_url in pls_urls:
                        v = validator.validate(entry_url)
                        if v.is_playable:
                            inner_candidate = StreamCandidate(
                                url=v.final_url or entry_url,
                                title="",
                                source="direct_url",
                                confidence=99,
                                reason="Extracted from PLS playlist. Validated as playable audio stream.",
                                originUrl=args.url,
                                originType="playlist",
                                originalUrl=v.final_url or entry_url,
                                stableUrl=v.final_url or entry_url,
                                originAction="",
                                qualityHint="unknown",
                                qualityScore=0,
                                isTemporary=False,
                                requiresFreshDiscovery=False,
                                isPlayable=True,
                                httpStatusCode=v.status_code,
                                contentType=v.content_type,
                                finalUrl=v.final_url or entry_url
                            )
                            break
                else:
                    m3u_result = validator._try_parse_m3u_response(args.url, playlist_text)
                    if m3u_result and m3u_result.is_playable:
                        inner_candidate = StreamCandidate(
                            url=m3u_result.final_url or args.url,
                            title="",
                            source="direct_url",
                            confidence=99,
                            reason="Parsed from playlist. Validated as playable audio stream.",
                            originUrl=args.url,
                            originType="playlist",
                            originalUrl=m3u_result.final_url or args.url,
                            stableUrl=m3u_result.final_url or args.url,
                            originAction="",
                            qualityHint="unknown",
                            qualityScore=0,
                            isTemporary=False,
                            requiresFreshDiscovery=False,
                            isPlayable=True,
                            httpStatusCode=m3u_result.status_code,
                            contentType=m3u_result.content_type,
                            finalUrl=m3u_result.final_url or args.url
                        )

                # A container playlist is handled here exclusively: return the
                # inner stream if found, otherwise report failure. We never fall
                # through to validate the playlist URL itself as a stream — doing
                # so would return the .pls/.m3u file (e.g. audio/x-scpls) as if it
                # were playable audio, which media players (LibVLC) then reject.
                if inner_candidate:
                    result = ParserResult(
                        success=True,
                        inputUrl=args.url,
                        effectiveUrl=args.url,
                        title="",
                        candidates=[inner_candidate],
                        diagnostics=diagnostics,
                        error=None
                    )
                else:
                    diagnostics.append("Playlist contained no playable stream entries.")
                    result = ParserResult(
                        success=False,
                        inputUrl=args.url,
                        effectiveUrl=args.url,
                        title="",
                        candidates=[],
                        diagnostics=diagnostics,
                        error="No playable stream found inside the playlist."
                    )
                print_json_result(result)
                return

            diagnostics.append("Input URL looks like a direct audio stream. Validating directly.")
            validator = StreamValidator(timeout=args.timeout, debug=args.debug)
            validation = validator.validate(args.url)

            if validation.is_playable:
                parsed = urlparse(args.url.lower())
                ext = parsed.path.rsplit(".", 1)[-1] if "." in parsed.path else ""
                quality_hint = "unknown"
                quality_score = 0

                candidate = StreamCandidate(
                    url=args.url,
                    title="",
                    source="direct_url",
                    confidence=99,
                    reason="Input URL is a direct audio stream. Validated as playable.",
                    originUrl=args.url,
                    originType="direct_input",
                    originalUrl=args.url,
                    stableUrl=args.url,
                    originAction="",
                    qualityHint=quality_hint,
                    qualityScore=quality_score,
                    isTemporary=False,
                    requiresFreshDiscovery=False,
                    isPlayable=True,
                    httpStatusCode=validation.status_code,
                    contentType=validation.content_type,
                    finalUrl=validation.final_url or args.url
                )

                result = ParserResult(
                    success=True,
                    inputUrl=args.url,
                    effectiveUrl=args.url,
                    title="",
                    candidates=[candidate],
                    diagnostics=diagnostics,
                    error=None
                )
                print_json_result(result)
                return
            else:
                diagnostics.append(f"Direct stream validation failed: {validation.reason}")
                result = ParserResult(
                    success=False,
                    inputUrl=args.url,
                    candidates=[],
                    diagnostics=diagnostics,
                    error=validation.reason
                )
                print_json_result(result)
                return

        static_extractor = GenericStaticExtractor(timeout=args.timeout, debug=args.debug)
        if static_extractor.can_handle(args.url):
            try:
                static_result = static_extractor.discover(args.url)
            except Exception as ex:
                static_result = ParserResult(success=False, inputUrl=args.url, candidates=[], diagnostics=["Static extractor failed"], error=str(ex))
            diagnostics.extend(static_result.diagnostics)
            if static_result.success and static_result.candidates:
                static_result.candidates = _expand_playlist_candidates(
                    static_result.candidates, args.timeout, args.debug, diagnostics
                )
                if static_result.candidates:
                    static_result.diagnostics = diagnostics
                    print_json_result(static_result)
                    return

        browser_extractor = BrowserNetworkExtractor(timeout=args.timeout, debug=args.debug)
        browser_result = browser_extractor.discover(args.url)
        diagnostics.extend(browser_result.diagnostics)

        if browser_result.success and browser_result.candidates:
            browser_result.candidates = _expand_playlist_candidates(
                browser_result.candidates, args.timeout, args.debug, diagnostics
            )
            if browser_result.candidates:
                browser_result.diagnostics = diagnostics
                print_json_result(browser_result)
                return

        result = ParserResult(success=False, inputUrl=args.url, candidates=[], diagnostics=diagnostics, error="No playable stream candidates found.")
        print_json_result(result)

    except Exception as ex:
        if args.debug:
            print(traceback.format_exc(), file=sys.stderr)
        result = ParserResult(success=False, inputUrl=args.url, candidates=[], diagnostics=diagnostics, error=str(ex))
        print_json_result(result)
        sys.exit(1)


if __name__ == "__main__":
    main()