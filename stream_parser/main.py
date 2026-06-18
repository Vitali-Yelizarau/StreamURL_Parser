import argparse
import json
import sys
import traceback
from urllib.parse import urlparse

from stream_parser.models import ParserResult, StreamCandidate
from stream_parser.extractors.generic_static import GenericStaticExtractor
from stream_parser.extractors.browser_network import BrowserNetworkExtractor
from stream_parser.stream_validator import StreamValidator

def print_json_result(result: ParserResult):
    import sys
    output = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
    # Принудительно пишем в UTF-8 — важно для exe на Windows с cp1251 консолью
    sys.stdout.buffer.write((output + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()

def _is_direct_stream_url(url: str) -> bool:
    """
    Проверяет что URL сам по себе является прямым аудио-потоком,
    а не страницей с плеером. Признаки: аудио-расширение или
    stream-like путь на нестандартном порту.
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

    # Нестандартный порт + stream-like путь (типичный Icecast/Shoutcast)
    port = parsed.port
    if port and port not in (80, 443, 8080, 8443):
        stream_tokens = ("/stream", "/live", "/audio", "/listen",
                         "/icecast", "/shoutcast", "/;", "/1", "/2", "/3")
        if not path or path == "/" or any(path.startswith(t) for t in stream_tokens):
            return True

    return False

def main():
    parser = argparse.ArgumentParser(description="Universal stream URL parser for RadioApp.")
    parser.add_argument("--url", required=True, help="Radio station page URL.")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP/browser timeout in seconds.")
    parser.add_argument("--debug", action="store_true", help="Print debug logs to stderr.")
    args = parser.parse_args()

    diagnostics = []

    try:
        # Прямая ссылка на аудио-поток — валидируем без запуска экстракторов
        if _is_direct_stream_url(args.url):
            # M3U/PLS плейлист — парсим содержимое вместо прямой валидации
            parsed_input = urlparse(args.url.lower())
            is_playlist = any(parsed_input.path.endswith(ext)
                              for ext in (".m3u", ".m3u8", ".pls", ".xspf"))
            if is_playlist:
                diagnostics.append("Input URL looks like a playlist. Downloading and parsing.")
                from stream_parser.http_client import HttpClient
                http = HttpClient(timeout=args.timeout, debug=args.debug)
                playlist_text = http.download_text_safe(args.url)
                validator = StreamValidator(timeout=args.timeout, debug=args.debug)
                m3u_result = validator._try_parse_m3u_response(args.url, playlist_text)
                if m3u_result and m3u_result.is_playable:
                    candidate = StreamCandidate(
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
                    diagnostics.append("Playlist parsing failed or no playable streams found.")

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
                static_result.diagnostics = diagnostics
                print_json_result(static_result)
                return

        browser_extractor = BrowserNetworkExtractor(timeout=args.timeout, debug=args.debug)
        browser_result = browser_extractor.discover(args.url)
        diagnostics.extend(browser_result.diagnostics)

        if browser_result.success and browser_result.candidates:
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