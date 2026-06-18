import json
import re
import sys
from typing import List, Set
from urllib.parse import urlparse, urlunparse, unquote

from playwright.sync_api import sync_playwright

from stream_parser.models import ParserResult, StreamCandidate

# Когда запускается из PyInstaller exe — браузеры лежат рядом с exe
# Устанавливаем PLAYWRIGHT_BROWSERS_PATH чтобы Playwright их нашёл
import sys as _sys
import os as _os
if getattr(_sys, 'frozen', False):
    # Запущен как exe — ищем папку ms-playwright рядом с exe
    _exe_dir = _os.path.dirname(_sys.executable)
    _browsers_path = _os.path.join(_exe_dir, 'ms-playwright')
    if _os.path.isdir(_browsers_path):
        _os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', _browsers_path)
    else:
        # Fallback: стандартный путь Playwright в AppData
        _pw_home = _os.path.join(
            _os.environ.get('LOCALAPPDATA', _os.path.expanduser('~')),
            'ms-playwright'
        )
        _os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', _pw_home)
from stream_parser.stream_validator import StreamValidator


HTTP_URL_PATTERN = re.compile(
    r'https?://[^\s"\'\\<>\]}{,\x00-\x1f]+',
    re.IGNORECASE
)

STREAM_URL_PATTERN = re.compile(
    r'https?://[^\s"\'\\<>\]}{,\x00-\x1f]+'
    r'\.(?:mp3|aac|ogg|opus|m3u8|m3u|pls|xspf|flac|wav)'
    r'(?:[^\s"\'\\<>\]}{,\x00-\x1f]*)?'
    r'|https?://[^\s"\'\\<>\]}{,\x00-\x1f]*'
    r'(?:/stream|/streams|/live|/icecast|/shoutcast|/listen|/audio|/aac|/mp3|/radio|/playlist)'
    r'[^\s"\'\\<>\]}{,\x00-\x1f]*',
    re.IGNORECASE
)


class BrowserNetworkExtractor:
    MAX_CANDIDATES_TO_VALIDATE = 160
    MAX_TEXT_BODY_BYTES = 1000000
    MAX_HOOK_TEXT_CHARS = 900000
    MAX_CLICK_CANDIDATES = 14
    CLICK_ROUNDS = 2

    def __init__(self, timeout: int = 15, debug: bool = False):
        self.timeout = timeout
        self.debug = debug
        self.validator = StreamValidator(timeout=timeout, debug=debug)
        self._current_action_description = ""

    def log(self, message: str):
        if self.debug:
            from datetime import datetime
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[{ts}] {message}", file=sys.stderr, flush=True)

    def can_handle(self, url: str) -> bool:
        if not url:
            return False

        parsed = urlparse(url)
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)

    def discover(self, url: str) -> ParserResult:
        diagnostics: List[str] = []
        candidates: List[StreamCandidate] = []

        diagnostics.append("Browser network extractor selected.")
        diagnostics.append("No platform-specific URL generation is used.")
        diagnostics.append("Browser instrumentation is installed before page load.")
        diagnostics.append("Network response capture is enabled.")
        diagnostics.append("JSON/text/JS body scanning is enabled.")
        diagnostics.append("Hooked fetch/XHR response text scanning is enabled.")
        diagnostics.append("Inline script, data-attribute and iframe scanning is enabled.")
        diagnostics.append("Minimal play/HD click fallback is enabled.")

        browser = None
        context = None

        try:
            with sync_playwright() as playwright:
                self.log("[BROWSER] Launching Chromium")

                browser = playwright.chromium.launch(headless=True)

                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0 Safari/537.36"
                    ),
                    viewport={
                        "width": 1366,
                        "height": 768
                    }
                )

                # Install hooks before any page JS runs. This is critical for
                # API-driven players such as radio.net and iframe players such
                # as onlineradiobox.
                context.add_init_script(self._browser_hook_script())

                page = context.new_page()

                page.on(
                    "response",
                    lambda response: self._handle_response(
                        response=response,
                        candidates=candidates,
                        diagnostics=diagnostics
                    )
                )

                self.log(f"[BROWSER] Opening page: {url}")

                try:
                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=self.timeout * 1000
                    )
                    diagnostics.append("Browser DOMContentLoaded reached.")
                except Exception as ex:
                    diagnostics.append(
                        f"Browser goto warning: {type(ex).__name__}: {ex}"
                    )

                # Увеличенное ожидание — сайт рендерит плеер через JS,
                # и появление рекламных фреймов может задерживать инициализацию.
                page.wait_for_timeout(4000)

                # Пытаемся закрыть consent/cookie overlay если он есть —
                # он может блокировать клики по кнопкам плеера.
                self._try_dismiss_consent_overlay(page=page, diagnostics=diagnostics)

                self._collect_all_runtime_candidates(
                    page=page,
                    candidates=candidates,
                    diagnostics=diagnostics,
                    origin_url=url,
                    phase="after DOMContentLoaded"
                )

                self._try_start_existing_media_elements_in_all_frames(
                    page=page,
                    diagnostics=diagnostics
                )

                page.wait_for_timeout(1200)

                self._collect_all_runtime_candidates(
                    page=page,
                    candidates=candidates,
                    diagnostics=diagnostics,
                    origin_url=url,
                    phase="after media play attempt"
                )

                already_clicked: Set[str] = set()

                # Ждём появления кнопки плеера — она может рендериться позже
                # чем мы начинаем искать. Пробуем несколько типичных селекторов.
                self._wait_for_player_button(page=page, diagnostics=diagnostics)

                priority_clicked = self._try_click_priority_title_buttons(
                    page=page,
                    stream_candidates=candidates,
                    diagnostics=diagnostics,
                    already_clicked=already_clicked
                )

                diagnostics.append(f"Priority title-button clicks: {priority_clicked}")

                page.wait_for_timeout(2500)

                self._collect_all_runtime_candidates(
                    page=page,
                    candidates=candidates,
                    diagnostics=diagnostics,
                    origin_url=url,
                    phase="after priority title clicks"
                )

                for round_index in range(self.CLICK_ROUNDS):
                    # Ранний выход: если уже есть кандидаты пойманные прямым перехватом
                    # сети (resource_type=media или audio content-type) — они надёжны
                    # и дальнейшие клики ничего не добавят.
                    # Кандидаты из inline-скриптов или хуков НЕ считаются — они ещё
                    # не валидированы и могут быть ложными.
                    high_confidence_sources = {"browser_network", "browser_media_element"}
                    if any(
                        c.source in high_confidence_sources
                        and c.contentType
                        and "octet-stream" not in c.contentType.lower()
                        and c.contentType.lower().startswith(("audio/", "video/"))
                            or (c.source in high_confidence_sources
                                and ("mpeg" in (c.contentType or "").lower()
                                     or "mpegurl" in (c.contentType or "").lower()
                                     or "ogg" in (c.contentType or "").lower()))
                        for c in candidates
                    ):
                        diagnostics.append(
                            f"Click rounds skipped: high-confidence stream already captured before round {round_index + 1}."
                        )
                        break

                    diagnostics.append(f"Click round started: {round_index + 1}")

                    clicked_count = self._try_click_play_elements(
                        page=page,
                        diagnostics=diagnostics,
                        already_clicked=already_clicked
                    )

                    diagnostics.append(
                        f"Click round finished: {round_index + 1}, clicked: {clicked_count}"
                    )

                    # Увеличенное ожидание после клика — некоторые плееры
                    # (mytuner, radiostationusa) запускают поток медленно
                    wait_ms = 4000 if clicked_count > 0 else 2000
                    # После клика пробуем вызвать внутренние функции плеера напрямую.
                    # Некоторые плееры (MyTuner) используют external_player флаг
                    # который в headless режиме перенаправляет на window.open вместо
                    # реального запуска потока. Вызываем update() напрямую.
                    try:
                        page.evaluate(
                            """
                            () => {
                                // MyTuner: force external_player=false и вызвать update()
                                if (typeof window.external_player !== 'undefined') {
                                    window.external_player = false;
                                }
                                // Вызвать update() напрямую если есть
                                if (typeof window.update === 'function') {
                                    try { window.update(); } catch(e) {}
                                }
                                // Или playRadio() с пропуском external_player
                                if (typeof window.playRadio === 'function') {
                                    try { window.playRadio(); } catch(e) {}
                                }
                                // Попробовать другие типичные функции плееров
                                const tryFns = ['play', 'startPlay', 'startStream', 'startRadio',
                                                'radioPlay', 'playerPlay', 'initPlayer'];
                                for (const fn of tryFns) {
                                    if (typeof window[fn] === 'function') {
                                        try { window[fn](); } catch(e) {}
                                    }
                                }
                            }
                            """
                        )
                        diagnostics.append("Attempted direct JS player function calls.")
                    except Exception as ex:
                        diagnostics.append(f"Direct JS call failed: {ex}")

                    # После клика ждём появления src на audio/video элементах
                    # (некоторые плееры типа MyTuner устанавливают src асинхронно)
                    try:
                        page.wait_for_function(
                            """
                            () => {
                                const els = document.querySelectorAll('audio, video');
                                for (const el of els) {
                                    if (el.src && el.src.startsWith('http')) return true;
                                    if (el.currentSrc && el.currentSrc.startsWith('http')) return true;
                                }
                                return false;
                            }
                            """,
                            timeout=5000
                        )
                        diagnostics.append("Media element src appeared after click.")
                    except Exception:
                        page.wait_for_timeout(wait_ms)

                    self._collect_all_runtime_candidates(
                        page=page,
                        candidates=candidates,
                        diagnostics=diagnostics,
                        origin_url=url,
                        phase=f"after click round {round_index + 1}"
                    )

                page.wait_for_timeout(4500)

                self._collect_all_runtime_candidates(
                    page=page,
                    candidates=candidates,
                    diagnostics=diagnostics,
                    origin_url=url,
                    phase="final"
                )

        except Exception as ex:
            diagnostics.append(
                f"Browser network extraction failed: {type(ex).__name__}: {ex}"
            )

            return ParserResult(
                success=False,
                inputUrl=url,
                effectiveUrl=url,
                title="",
                candidates=[],
                diagnostics=diagnostics,
                error=str(ex)
            )

        finally:
            self._current_action_description = ""

            try:
                if context is not None:
                    context.close()
            except Exception:
                pass

            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass

        candidates = self._deduplicate_candidates(candidates)

        diagnostics.append(
            f"Browser candidate count before query normalization: {len(candidates)}"
        )

        candidates = self._add_query_stripped_candidates(candidates, diagnostics)
        candidates = self._deduplicate_candidates(candidates)

        diagnostics.append(
            f"Browser candidate count before validation: {len(candidates)}"
        )

        candidates = candidates[:self.MAX_CANDIDATES_TO_VALIDATE]
        candidates = self._validate_candidates(candidates)

        diagnostics.append(
            f"Browser playable candidate count after validation: {len(candidates)}"
        )

        candidates = self._sort_candidates(candidates)

        diagnostics.append(
            f"Browser playable candidate count after sorting: {len(candidates)}"
        )

        return ParserResult(
            success=len(candidates) > 0,
            inputUrl=url,
            effectiveUrl=url,
            title="",
            candidates=candidates,
            diagnostics=diagnostics,
            error=None if candidates else "No playable stream candidates found by browser network extractor."
        )

    # ---------------------------------------------------------------------
    # Browser instrumentation
    # ---------------------------------------------------------------------

    def _browser_hook_script(self) -> str:
        return r"""
        (() => {
            if (window.__streamParserHooksInstalled) {
                return;
            }

            window.__streamParserHooksInstalled = true;
            window.__streamParserCapturedUrls = window.__streamParserCapturedUrls || [];
            window.__streamParserCapturedTexts = window.__streamParserCapturedTexts || [];

            const MAX_TEXT_LENGTH = 900000;

            function pushUrl(value, source) {
                try {
                    if (!value) {
                        return;
                    }

                    let text = '';

                    if (typeof value === 'string') {
                        text = value;
                    } else if (value && typeof value.url === 'string') {
                        text = value.url;
                    } else {
                        text = String(value);
                    }

                    if (!/^https?:\/\//i.test(text)) {
                        return;
                    }

                    window.__streamParserCapturedUrls.push({
                        url: text,
                        source: source || 'browser_hook',
                        time: Date.now()
                    });
                } catch (e) {
                }
            }

            function pushText(text, source, url) {
                try {
                    if (!text || typeof text !== 'string') {
                        return;
                    }

                    if (text.length > MAX_TEXT_LENGTH) {
                        return;
                    }

                    window.__streamParserCapturedTexts.push({
                        text: text,
                        source: source || 'hooked_response_body',
                        url: url || '',
                        time: Date.now()
                    });
                } catch (e) {
                }
            }

            function isTextLikeContentType(contentType) {
                const ct = String(contentType || '').toLowerCase();

                return ct.includes('json')
                    || ct.includes('text')
                    || ct.includes('javascript')
                    || ct.includes('xml')
                    || ct.includes('mpegurl')
                    || ct.includes('x-mpegurl');
            }

            function patchFetch() {
                try {
                    if (!window.fetch || window.fetch.__streamParserPatched) {
                        return;
                    }

                    const originalFetch = window.fetch;

                    const patchedFetch = function(input, init) {
                        try {
                            pushUrl(input, 'fetch_request');
                        } catch (e) {
                        }

                        const promise = originalFetch.apply(this, arguments);

                        try {
                            return promise.then(response => {
                                try {
                                    pushUrl(response && response.url, 'fetch_response');

                                    const contentType = response && response.headers
                                        ? (response.headers.get('content-type') || '')
                                        : '';

                                    if (response && isTextLikeContentType(contentType)) {
                                        response.clone().text()
                                            .then(text => pushText(text, 'fetch_response_body', response.url || ''))
                                            .catch(() => {});
                                    }
                                } catch (e) {
                                }

                                return response;
                            });
                        } catch (e) {
                            return promise;
                        }
                    };

                    patchedFetch.__streamParserPatched = true;
                    window.fetch = patchedFetch;
                } catch (e) {
                }
            }

            function patchXhr() {
                try {
                    if (!window.XMLHttpRequest || window.XMLHttpRequest.prototype.__streamParserPatched) {
                        return;
                    }

                    const proto = window.XMLHttpRequest.prototype;
                    const originalOpen = proto.open;
                    const originalSend = proto.send;

                    proto.open = function(method, url) {
                        try {
                            this.__streamParserUrl = url;
                            pushUrl(url, 'xhr_open');
                        } catch (e) {
                        }

                        return originalOpen.apply(this, arguments);
                    };

                    proto.send = function() {
                        try {
                            this.addEventListener('load', function() {
                                try {
                                    const responseUrl = this.responseURL || this.__streamParserUrl || '';
                                    pushUrl(responseUrl, 'xhr_load');

                                    const contentType = this.getResponseHeader('content-type') || '';

                                    if (isTextLikeContentType(contentType)) {
                                        let text = '';

                                        try {
                                            if (typeof this.responseText === 'string') {
                                                text = this.responseText;
                                            }
                                        } catch (e) {
                                        }

                                        if (text) {
                                            pushText(text, 'xhr_response_body', responseUrl);
                                        }
                                    }
                                } catch (e) {
                                }
                            });
                        } catch (e) {
                        }

                        return originalSend.apply(this, arguments);
                    };

                    proto.__streamParserPatched = true;
                } catch (e) {
                }
            }

            function patchAudioConstructor() {
                try {
                    if (!window.Audio || window.Audio.__streamParserPatched) {
                        return;
                    }

                    const OriginalAudio = window.Audio;

                    const PatchedAudio = function(src) {
                        try {
                            pushUrl(src, 'Audio_constructor');
                        } catch (e) {
                        }

                        return new OriginalAudio(src);
                    };

                    PatchedAudio.prototype = OriginalAudio.prototype;
                    PatchedAudio.__streamParserPatched = true;

                    window.Audio = PatchedAudio;
                } catch (e) {
                }
            }

            function patchSrcProperty(proto, label) {
                try {
                    if (!proto || proto.__streamParserSrcPatched) {
                        return;
                    }

                    const descriptor = Object.getOwnPropertyDescriptor(proto, 'src');

                    if (!descriptor || !descriptor.set) {
                        proto.__streamParserSrcPatched = true;
                        return;
                    }

                    Object.defineProperty(proto, 'src', {
                        configurable: true,
                        enumerable: descriptor.enumerable,
                        get: function() {
                            return descriptor.get.call(this);
                        },
                        set: function(value) {
                            try {
                                pushUrl(value, label + '.src_set');
                            } catch (e) {
                            }

                            return descriptor.set.call(this, value);
                        }
                    });

                    proto.__streamParserSrcPatched = true;
                } catch (e) {
                }
            }

            function patchSetAttribute() {
                try {
                    if (!window.Element || window.Element.prototype.__streamParserSetAttributePatched) {
                        return;
                    }

                    const originalSetAttribute = window.Element.prototype.setAttribute;

                    window.Element.prototype.setAttribute = function(name, value) {
                        try {
                            const lowerName = String(name || '').toLowerCase();

                            if (
                                lowerName === 'src'
                                || lowerName === 'href'
                                || lowerName === 'url'
                                || lowerName === 'data-src'
                                || lowerName === 'data-url'
                                || lowerName === 'data-stream'
                                || lowerName === 'data-file'
                                || lowerName === 'data-audio'
                                || lowerName === 'data-media'
                                || lowerName === 'data-playlist'
                                || lowerName === 'data-href'
                                || lowerName === 'data-link'
                            ) {
                                pushUrl(value, 'setAttribute:' + lowerName);
                            }
                        } catch (e) {
                        }

                        return originalSetAttribute.apply(this, arguments);
                    };

                    window.Element.prototype.__streamParserSetAttributePatched = true;
                } catch (e) {
                }
            }

            patchFetch();
            patchXhr();
            patchAudioConstructor();
            patchSrcProperty(window.HTMLMediaElement && window.HTMLMediaElement.prototype, 'HTMLMediaElement');
            patchSrcProperty(window.HTMLAudioElement && window.HTMLAudioElement.prototype, 'HTMLAudioElement');
            patchSrcProperty(window.HTMLVideoElement && window.HTMLVideoElement.prototype, 'HTMLVideoElement');
            patchSrcProperty(window.HTMLSourceElement && window.HTMLSourceElement.prototype, 'HTMLSourceElement');
            patchSetAttribute();
        })();
        """

    def _collect_hooked_urls_from_frame(
        self,
        frame,
        candidates: List[StreamCandidate],
        diagnostics: List[str],
        origin_url: str,
        phase: str,
        frame_index: int
    ):
        try:
            captured = frame.evaluate(
                """
                () => {
                    const data = window.__streamParserCapturedUrls || [];
                    window.__streamParserCapturedUrls = [];
                    return data;
                }
                """
            )

            if not captured:
                return

            diagnostics.append(
                f"Browser hooks captured {len(captured)} URL(s). Phase: {phase}, Frame: {frame_index}"
            )

            accepted = 0

            for item in captured:
                if not item:
                    continue

                captured_url = item.get("url") if isinstance(item, dict) else None
                hook_source = item.get("source") if isinstance(item, dict) else "browser_hook"

                if not captured_url:
                    continue

                if not self._is_url_potentially_stream(captured_url):
                    continue

                accepted += 1

                self._append_candidate(
                    candidates=candidates,
                    url=captured_url,
                    source="browser_hook",
                    confidence=82,
                    reason=f"Captured by browser JS hook: {hook_source}.",
                    origin_url=origin_url,
                    origin_type=f"browser_hook_{hook_source}",
                    action_description=self._current_action_description,
                    infer_quality_from_action=True
                )

            if accepted:
                diagnostics.append(
                    f"Browser hooks accepted {accepted} stream-like URL(s). Phase: {phase}, Frame: {frame_index}"
                )

        except Exception as ex:
            if "detached" not in str(ex).lower() and "destroyed" not in str(ex).lower():
                diagnostics.append(
                    f"Failed to collect browser hook URLs from frame {frame_index}: {type(ex).__name__}: {ex}"
                )

    def _collect_hooked_texts_from_frame(
        self,
        frame,
        candidates: List[StreamCandidate],
        diagnostics: List[str],
        origin_url: str,
        phase: str,
        frame_index: int
    ):
        try:
            captured = frame.evaluate(
                """
                () => {
                    const data = window.__streamParserCapturedTexts || [];
                    window.__streamParserCapturedTexts = [];
                    return data;
                }
                """
            )

            if not captured:
                return

            diagnostics.append(
                f"Browser hooks captured {len(captured)} response text(s). Phase: {phase}, Frame: {frame_index}"
            )

            accepted = 0

            for item in captured:
                if not item:
                    continue

                if isinstance(item, dict):
                    text = item.get("text") or ""
                    hook_source = item.get("source") or "hooked_response_body"
                    response_url = item.get("url") or origin_url
                else:
                    text = str(item)
                    hook_source = "hooked_response_body"
                    response_url = origin_url

                found = self._extract_stream_urls_from_text(text)

                for stream_url in found:
                    accepted += 1

                    self._append_candidate(
                        candidates=candidates,
                        url=stream_url,
                        source="hooked_response_body",
                        confidence=86,
                        reason=f"Found in hooked browser response body: {hook_source}.",
                        origin_url=response_url,
                        origin_type=f"hooked_response_body_{hook_source}",
                        action_description=self._current_action_description,
                        infer_quality_from_action=True
                    )

            if accepted:
                diagnostics.append(
                    f"Hooked response body scan accepted {accepted} URL candidate(s). Phase: {phase}, Frame: {frame_index}"
                )

        except Exception as ex:
            if "detached" not in str(ex).lower() and "destroyed" not in str(ex).lower():
                diagnostics.append(
                    f"Failed to collect hooked response texts from frame {frame_index}: {type(ex).__name__}: {ex}"
                )

    # ---------------------------------------------------------------------
    # Network response processing
    # ---------------------------------------------------------------------

    def _handle_response(
        self,
        response,
        candidates: List[StreamCandidate],
        diagnostics: List[str]
    ):
        # FIX 1: Вся обработка тела ответа обёрнута в try/except с явной
        # проверкой что URL не является потоковым медиа-эндпоинтом.
        # response.body() на живом аудио-стриме блокирует поток навсегда —
        # это и было причиной зависания на onlineradiobox.com.
        try:
            response_url = response.url
            status = response.status
            headers = response.headers or {}
            content_type = headers.get("content-type", "")
            resource_type = response.request.resource_type

            if self._should_scan_response_body(
                url=response_url,
                content_type=content_type,
                resource_type=resource_type,
                status_code=status
            ):
                self._scan_response_body(
                    response=response,
                    candidates=candidates,
                    diagnostics=diagnostics,
                    content_type=content_type,
                    resource_type=resource_type
                )

            if not self._is_network_stream_like_url(
                url=response_url,
                content_type=content_type,
                resource_type=resource_type,
                status_code=status
            ):
                return

            cleaned = self._clean_url(response_url)

            if not cleaned:
                return

            action_description = self._current_action_description

            self.log(
                f"[BROWSER] Stream-like response captured. "
                f"Status={status}, ContentType={content_type}, "
                f"ResourceType={resource_type}, "
                f"Action={self._shorten(action_description)}, "
                f"Url={self._shorten(cleaned)}"
            )

            diagnostics.append(
                f"Stream-like browser response captured. "
                f"Status: {status}, ContentType: {content_type}, "
                f"ResourceType: {resource_type}, "
                f"Action: {self._shorten(action_description)}, "
                f"Url: {self._shorten(cleaned)}"
            )

            candidate_confidence = self._get_candidate_confidence(
                url=cleaned,
                content_type=content_type,
                resource_type=resource_type
            )

            self._append_candidate(
                candidates=candidates,
                url=cleaned,
                source="browser_network",
                confidence=candidate_confidence,
                reason="Captured from browser network response.",
                origin_url=response_url,
                origin_type="browser_response",
                action_description=action_description,
                infer_quality_from_action=True,
                captured_content_type=content_type,
                captured_status_code=status
            )

        except Exception as ex:
            self.log(
                f"[BROWSER] Failed to inspect response: {type(ex).__name__}: {ex}"
            )

    def _should_scan_response_body(
        self,
        url: str,
        content_type: str,
        resource_type: str,
        status_code: int
    ) -> bool:
        if status_code < 200 or status_code >= 400:
            return False

        if not url:
            return False

        if self._is_noise_or_ad_url(url):
            return False

        lower_type = (content_type or "").lower()
        lower_resource = (resource_type or "").lower()
        lower_url = url.lower().split("?", 1)[0]
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()

        # Никогда не читаем тело медиа-ресурсов, аудио/видео потоков и HTML-страниц.
        # HTML создаёт мусорных кандидатов из навигационных ссылок (o.tavr.media/roksbal и т.п.)
        # Аудио/медиа приводят к зависанию при чтении живого потока.
        if lower_resource in ("image", "font", "stylesheet", "media", "document"):
            return False

        if lower_type.startswith("audio/"):
            return False

        if lower_type.startswith("video/"):
            return False

        if "text/html" in lower_type:
            return False

        if "mpegurl" in lower_type or "x-mpegurl" in lower_type:
            return True

        if lower_resource == "media":
            return False

        path = parsed.path.lower()
        audio_extensions = (".mp3", ".aac", ".ogg", ".opus", ".flac", ".wav")
        if any(path.endswith(ext) for ext in audio_extensions):
            return False

        # Никогда не читаем тело если URL сам выглядит как потоковый эндпоинт —
        # даже если content-type ещё не определён. Это предотвращает зависание
        # на onlineradiobox и аналогичных плеерах где стрим запрашивается через fetch.
        if self._is_url_potentially_stream(url):
            return False

        if "json" in lower_type:
            return True

        if "javascript" in lower_type:
            return True

        # Только text/plain и text/xml — не text/html
        if lower_type.startswith("text/plain") or lower_type.startswith("text/xml"):
            return True

        if lower_url.endswith(".js") or lower_url.endswith(".json"):
            return True

        # Явно сканируем API-эндпоинты (radio.net, onlineradiobox и подобные).
        if host.startswith("api.") or "/api/" in lower_url:
            return True

        return False

    def _scan_response_body(
        self,
        response,
        candidates: List[StreamCandidate],
        diagnostics: List[str],
        content_type: str,
        resource_type: str
    ):
        try:
            body = response.body()

            if not body:
                return

            if len(body) > self.MAX_TEXT_BODY_BYTES:
                diagnostics.append(
                    f"Skipped large response body scan: {self._shorten(response.url)}, bytes: {len(body)}"
                )
                return

            text = body.decode("utf-8", errors="ignore")
            found = self._extract_stream_urls_from_text(text)

            if not found:
                return

            diagnostics.append(
                f"Response body scan found {len(found)} URL candidate(s). "
                f"ResourceType: {resource_type}, ContentType: {content_type}, Url: {self._shorten(response.url)}"
            )

            for stream_url in found:
                self._append_candidate(
                    candidates=candidates,
                    url=stream_url,
                    source="response_body",
                    confidence=78,
                    reason="Found in browser response body.",
                    origin_url=response.url,
                    origin_type="response_body",
                    action_description=self._current_action_description,
                    infer_quality_from_action=True
                )

        except Exception as ex:
            diagnostics.append(
                f"Response body scan failed: {type(ex).__name__}: {ex}, Url: {self._shorten(response.url)}"
            )
            self.log(f"[BROWSER] Response body scan failed: {type(ex).__name__}: {ex}")

    # ---------------------------------------------------------------------
    # Runtime page extraction
    # ---------------------------------------------------------------------

    def _collect_all_runtime_candidates(
        self,
        page,
        candidates: List[StreamCandidate],
        diagnostics: List[str],
        origin_url: str,
        phase: str
    ):
        frames = list(page.frames)

        diagnostics.append(
            f"Runtime extraction phase: {phase}, frame count: {len(frames)}"
        )

        for frame_index, frame in enumerate(frames):
            try:
                frame_url = frame.url or origin_url

                if frame_url and self._is_noise_or_ad_url(frame_url):
                    continue

                self._collect_hooked_urls_from_frame(
                    frame=frame,
                    candidates=candidates,
                    diagnostics=diagnostics,
                    origin_url=frame_url,
                    phase=phase,
                    frame_index=frame_index
                )

                self._collect_hooked_texts_from_frame(
                    frame=frame,
                    candidates=candidates,
                    diagnostics=diagnostics,
                    origin_url=frame_url,
                    phase=phase,
                    frame_index=frame_index
                )

                self._collect_media_element_urls_from_frame(
                    frame=frame,
                    candidates=candidates,
                    diagnostics=diagnostics,
                    origin_url=frame_url,
                    phase=phase,
                    frame_index=frame_index
                )

                self._extract_inline_scripts_and_attributes_from_frame(
                    frame=frame,
                    candidates=candidates,
                    diagnostics=diagnostics,
                    origin_url=frame_url,
                    phase=phase,
                    frame_index=frame_index
                )

                self._collect_performance_resource_urls_from_frame(
                    frame=frame,
                    candidates=candidates,
                    diagnostics=diagnostics,
                    origin_url=frame_url,
                    phase=phase,
                    frame_index=frame_index
                )

            except Exception as ex:
                # Detached frames are normal (ad iframes), skip silently
                err_str = str(ex)
                if "detached" not in err_str.lower() and "destroyed" not in err_str.lower():
                    diagnostics.append(
                        f"Runtime extraction failed in frame {frame_index}: {type(ex).__name__}: {ex}"
                    )

    def _collect_media_element_urls_from_frame(
        self,
        frame,
        candidates: List[StreamCandidate],
        diagnostics: List[str],
        origin_url: str,
        phase: str,
        frame_index: int
    ):
        try:
            media_urls = frame.evaluate(
                """
                () => {
                    const result = [];

                    for (const element of document.querySelectorAll('audio, video, source')) {
                        if (element.currentSrc) {
                            result.push(element.currentSrc);
                        }

                        if (element.src) {
                            result.push(element.src);
                        }

                        const srcAttr = element.getAttribute('src');

                        if (srcAttr) {
                            result.push(srcAttr);
                        }
                    }

                    return Array.from(new Set(result)).filter(x => !!x);
                }
                """
            )

            diagnostics.append(
                f"Media element URLs found: {len(media_urls)}. Phase: {phase}, Frame: {frame_index}"
            )

            for media_url in media_urls:
                cleaned = self._clean_url(media_url)

                if not cleaned:
                    continue

                if not self._is_url_potentially_stream(cleaned):
                    continue

                self._append_candidate(
                    candidates=candidates,
                    url=cleaned,
                    source="browser_media_element",
                    confidence=82,
                    reason="Found in browser media element.",
                    origin_url=origin_url,
                    origin_type=f"browser_media_element_frame_{frame_index}",
                    action_description=self._current_action_description,
                    infer_quality_from_action=True
                )

        except Exception as ex:
            if "detached" not in str(ex).lower() and "destroyed" not in str(ex).lower():
                diagnostics.append(
                    f"Failed to collect media element URLs from frame {frame_index}: {type(ex).__name__}: {ex}"
                )

    def _extract_inline_scripts_and_attributes_from_frame(
        self,
        frame,
        candidates: List[StreamCandidate],
        diagnostics: List[str],
        origin_url: str,
        phase: str,
        frame_index: int
    ):
        try:
            texts = frame.evaluate(
                """
                () => {
                    const result = [];

                    for (const s of document.querySelectorAll('script:not([src])')) {
                        const text = (s.textContent || '').trim();

                        if (text.length > 0 && text.length < 500000) {
                            result.push(text);
                        }
                    }

                    const attrs = [
                        'src',
                        'href',
                        'url',
                        'data-src',
                        'data-url',
                        'data-stream',
                        'data-file',
                        'data-audio',
                        'data-media',
                        'data-playlist',
                        'data-href',
                        'data-link'
                    ];

                    const selector = attrs.map(x => '[' + x + ']').join(',');

                    for (const el of document.querySelectorAll(selector)) {
                        for (const attr of attrs) {
                            const val = el.getAttribute(attr);

                            if (val) {
                                result.push(val);
                            }
                        }
                    }

                    return result;
                }
                """
            )

            found_all: List[str] = []

            for text in texts or []:
                found_all.extend(self._extract_stream_urls_from_text(text))

            found_all = self._unique_strings(found_all)

            if found_all:
                diagnostics.append(
                    f"Inline/data extraction found {len(found_all)} URL candidate(s). "
                    f"Phase: {phase}, Frame: {frame_index}"
                )
                for _u in found_all:
                    diagnostics.append(f"  Inline candidate: {self._shorten(_u, 120)}")

            for stream_url in found_all:
                self._append_candidate(
                    candidates=candidates,
                    url=stream_url,
                    source="inline_or_data_attribute",
                    confidence=76,
                    reason="Found in inline script or DOM attribute.",
                    origin_url=origin_url,
                    origin_type=f"inline_or_data_attribute_frame_{frame_index}",
                    action_description=self._current_action_description,
                    infer_quality_from_action=True
                )

        except Exception as ex:
            if "detached" not in str(ex).lower() and "destroyed" not in str(ex).lower():
                diagnostics.append(
                    f"Failed to extract inline scripts/data attributes from frame {frame_index}: {type(ex).__name__}: {ex}"
                )

    def _collect_performance_resource_urls_from_frame(
        self,
        frame,
        candidates: List[StreamCandidate],
        diagnostics: List[str],
        origin_url: str,
        phase: str,
        frame_index: int
    ):
        try:
            entries = frame.evaluate(
                """
                () => {
                    if (!performance || !performance.getEntriesByType) {
                        return [];
                    }

                    return performance
                        .getEntriesByType('resource')
                        .map(x => ({
                            name: x.name || '',
                            initiatorType: x.initiatorType || ''
                        }))
                        .filter(x => !!x.name);
                }
                """
            )

            if not entries:
                return

            accepted = 0

            for item in entries:
                if not item:
                    continue

                candidate_url = item.get("name")
                initiator = item.get("initiatorType") or "resource"

                if not self._is_url_potentially_stream(candidate_url):
                    continue

                accepted += 1

                self._append_candidate(
                    candidates=candidates,
                    url=candidate_url,
                    source="performance_resource",
                    confidence=72,
                    reason=f"Found in performance resource entries. Initiator: {initiator}.",
                    origin_url=origin_url,
                    origin_type=f"performance_resource_{initiator}_frame_{frame_index}",
                    action_description=self._current_action_description,
                    infer_quality_from_action=True
                )

            if accepted:
                diagnostics.append(
                    f"Performance resource scan accepted {accepted} URL candidate(s). "
                    f"Phase: {phase}, Frame: {frame_index}"
                )

        except Exception as ex:
            if "detached" not in str(ex).lower() and "destroyed" not in str(ex).lower():
                diagnostics.append(
                    f"Failed to collect performance resource URLs from frame {frame_index}: {type(ex).__name__}: {ex}"
                )

    def _wait_for_player_button(self, page, diagnostics: List[str]):
        """
        Ждёт появления кнопки плеера в DOM.
        Приоритет — кнопки с title (они нужны _try_click_priority_title_buttons).
        Класс-based селекторы используются только как запасной вариант.
        """
        # Title-based selectors — именно их ищет _try_click_priority_title_buttons
        title_selectors = [
            'button[title*="play" i]',
            '[role="button"][title*="play" i]',
            'button[title*="definition" i]',
            'button[title*="playback" i]',
            '[onclick][title*="play" i]',
        ]

        # Ждём любой из title-based кнопок до 5 секунд суммарно
        for selector in title_selectors:
            try:
                page.wait_for_selector(selector, timeout=1500, state="attached")
                diagnostics.append(f"Player button appeared: {selector}")
                return
            except Exception:
                continue

        # Запасной вариант — кнопки по классу (jp-play, class*=play)
        fallback_selectors = [
            '[class*="jp-play"]',
            'button[class*="play" i]',
        ]

        for selector in fallback_selectors:
            try:
                page.wait_for_selector(selector, timeout=1000, state="attached")
                diagnostics.append(f"Player button appeared (fallback): {selector}")
                return
            except Exception:
                continue

        diagnostics.append("Player button wait timed out for all selectors.")

    def _try_dismiss_consent_overlay(self, page, diagnostics: List[str]):
        """
        Пытается закрыть overlay с consent/cookie/GDPR который может блокировать
        кнопки плеера. Ищет типичные кнопки принятия и кликает первую найденную.
        """
        try:
            clicked = page.evaluate(
                """
                () => {
                    const consentSelectors = [
                        '[class*="consent"] button[class*="accept" i]',
                        '[class*="consent"] button[class*="agree" i]',
                        '[class*="cookie"] button[class*="accept" i]',
                        '[class*="cookie"] button[class*="agree" i]',
                        '[id*="consent"] button',
                        '[id*="cookie"] button',
                        'button[id*="accept" i]',
                        'button[class*="accept" i]',
                        '[aria-label*="accept" i]',
                        '[aria-label*="agree" i]',
                        '.fc-button-label',
                        '.fc-cta-consent',
                        '#didomi-notice-agree-button',
                        '.didomi-continue-without-agreeing',
                        '[class*="gdpr"] button',
                        '.qc-cmp2-summary-buttons button:first-child'
                    ];

                    for (const selector of consentSelectors) {
                        try {
                            const el = document.querySelector(selector);
                            if (el) {
                                el.click();
                                return selector;
                            }
                        } catch (e) {}
                    }

                    return null;
                }
                """
            )

            if clicked:
                diagnostics.append(f"Consent overlay dismissed: {clicked}")
                page.wait_for_timeout(1000)
            else:
                diagnostics.append("No consent overlay found.")

        except Exception as ex:
            diagnostics.append(f"Consent overlay dismissal failed: {type(ex).__name__}: {ex}")

    def _try_start_existing_media_elements_in_all_frames(self, page, diagnostics: List[str]):
        frames = list(page.frames)
        total = 0

        for frame_index, frame in enumerate(frames):
            try:
                # Используем синхронный вариант без await — async evaluate
                # с await на кросс-origin фреймах может зависнуть навсегда
                # если play() возвращает Promise который не резолвится.
                started_urls = frame.evaluate(
                    """
                    () => {
                        const result = [];

                        for (const element of document.querySelectorAll('audio, video')) {
                            const url = element.currentSrc || element.src || element.getAttribute('src') || '';

                            if (url) {
                                result.push(url);
                            }

                            try {
                                const p = element.play();
                                if (p && p.catch) {
                                    p.catch(() => {});
                                }
                            } catch (e) {
                            }
                        }

                        return Array.from(new Set(result)).filter(x => !!x);
                    }
                    """
                )

                total += len(started_urls)

            except Exception as ex:
                err = str(ex)
                if "detached" not in err.lower() and "destroyed" not in err.lower():
                    diagnostics.append(
                        f"Failed to start media elements in frame {frame_index}: {type(ex).__name__}: {ex}"
                    )

        diagnostics.append(f"Tried to start existing media elements in all frames. URLs: {total}")

    # ---------------------------------------------------------------------
    # Click fallback
    # ---------------------------------------------------------------------

    def _try_click_priority_title_buttons(
        self,
        page,
        stream_candidates: List[StreamCandidate],
        diagnostics: List[str],
        already_clicked: Set[str]
    ) -> int:
        try:
            click_candidates = page.evaluate(
                """
                () => {
                    const selectors = [
                        'button[title*="high definition" i]',
                        '[role="button"][title*="high definition" i]',
                        '[onclick][title*="high definition" i]',
                        'button[title*="quality" i]',
                        '[role="button"][title*="quality" i]',
                        '[onclick][title*="quality" i]',
                        'button[title*="start playback" i]',
                        '[role="button"][title*="start playback" i]',
                        '[onclick][title*="start playback" i]',
                        'button[title*="play" i]',
                        '[role="button"][title*="play" i]',
                        '[onclick][title*="play" i]'
                    ];

                    const result = [];
                    const seen = new Set();

                    function normalize(value) {
                        return String(value || '').toLowerCase();
                    }

                    function makeKey(element) {
                        const rect = element.getBoundingClientRect();

                        return [
                            element.tagName.toLowerCase(),
                            element.id || '',
                            element.className || '',
                            element.getAttribute('role') || '',
                            element.getAttribute('aria-label') || '',
                            element.getAttribute('title') || '',
                            element.getAttribute('data-quality') || '',
                            element.getAttribute('data-bitrate') || '',
                            element.getAttribute('href') || '',
                            Math.round(rect.left),
                            Math.round(rect.top),
                            Math.round(rect.width),
                            Math.round(rect.height)
                        ].map(normalize).join('|');
                    }

                    function getDescription(element) {
                        return [
                            element.tagName.toLowerCase(),
                            element.id ? '#' + element.id : '',
                            element.className ? '.' + String(element.className).replace(/\\s+/g, '.') : '',
                            element.getAttribute('role') ? 'role=' + element.getAttribute('role') : '',
                            element.getAttribute('aria-label') ? 'aria=' + element.getAttribute('aria-label') : '',
                            element.getAttribute('title') ? 'title=' + element.getAttribute('title') : '',
                            element.getAttribute('data-quality') ? 'data-quality=' + element.getAttribute('data-quality') : '',
                            element.getAttribute('data-bitrate') ? 'data-bitrate=' + element.getAttribute('data-bitrate') : '',
                            element.getAttribute('href') ? 'href=' + element.getAttribute('href') : '',
                            element.textContent ? 'text=' + element.textContent.trim().slice(0, 80) : ''
                        ].filter(x => !!x).join(' ');
                    }

                    for (let selectorIndex = 0; selectorIndex < selectors.length; selectorIndex++) {
                        const selector = selectors[selectorIndex];

                        for (const element of document.querySelectorAll(selector)) {
                            if (seen.has(element)) {
                                continue;
                            }

                            seen.add(element);

                            const title = normalize(element.getAttribute('title'));
                            const classAndId = normalize(element.className + ' ' + element.id);
                            const description = getDescription(element);
                            const key = makeKey(element);

                            let score = 0;

                            if (title.includes('high definition')) {
                                score += 1000;
                            }

                            if (/(^|[^a-z0-9])(hd|hq)([^a-z0-9]|$)/i.test(classAndId)) {
                                score += 500;
                            }

                            if (title.includes('quality')) {
                                score += 300;
                            }

                            if (title.includes('start playback')) {
                                score += 100;
                            }

                            if (title.includes('play')) {
                                score += 80;
                            }

                            score += Math.max(0, 50 - selectorIndex);

                            result.push({
                                key: key,
                                score: score,
                                description: description
                            });
                        }
                    }

                    result.sort((a, b) => b.score - a.score);

                    return result.slice(0, 20);
                }
                """
            )

            diagnostics.append(f"Priority title-button candidates found: {len(click_candidates or [])}")

            # Диагностика: перечисляем ВСЕ кнопки с title на странице,
            # чтобы понять почему приоритетные кнопки не найдены.
            if not click_candidates:
                try:
                    all_titled = page.evaluate(
                        """
                        () => {
                            return Array.from(document.querySelectorAll('[title]'))
                                .filter(el => {
                                    const t = (el.tagName || '').toLowerCase();
                                    return t === 'button' || el.getAttribute('role') === 'button' || !!el.getAttribute('onclick');
                                })
                                .slice(0, 20)
                                .map(el => el.tagName.toLowerCase() + ' title=' + el.getAttribute('title'));
                        }
                        """
                    )
                    for item in (all_titled or []):
                        diagnostics.append(f"All titled interactive elements: {item}")
                except Exception:
                    pass

            clicked_count = 0

            for candidate in click_candidates or []:
                key = candidate.get("key", "")
                description = candidate.get("description", "")

                if not key:
                    continue

                if key in already_clicked:
                    continue

                already_clicked.add(key)

                self._current_action_description = description

                diagnostics.append(
                    f"Priority title-button candidate. Score: {candidate.get('score')}, Description: {description}"
                )

                self.log(
                    f"[BROWSER] Priority title-button click: {self._shorten(description)}"
                )

                clicked = page.evaluate(self._dom_click_by_key_script(), key)

                if clicked:
                    clicked_count += 1
                    diagnostics.append(
                        f"Priority title-button clicked: {description}"
                    )
                    page.wait_for_timeout(2200)

                    self._collect_all_runtime_candidates(
                        page=page,
                        candidates=stream_candidates,
                        diagnostics=diagnostics,
                        origin_url=description,
                        phase="after priority title-button click"
                    )
                else:
                    diagnostics.append(
                        f"Priority title-button was not clicked: {description}"
                    )

                self._current_action_description = ""

            return clicked_count

        except Exception as ex:
            diagnostics.append(
                f"Failed to click priority title buttons: {type(ex).__name__}: {ex}"
            )
            self._current_action_description = ""
            return 0

    def _try_click_play_elements(
        self,
        page,
        diagnostics: List[str],
        already_clicked: Set[str]
    ) -> int:
        click_candidates = self._find_click_candidates(page, diagnostics)

        if not click_candidates:
            diagnostics.append("No clickable play/listen/quality candidates were found.")
            return 0

        diagnostics.append(
            f"Clickable play/listen/quality candidates found: {len(click_candidates)}"
        )

        clicked_count = 0

        for candidate in click_candidates[:self.MAX_CLICK_CANDIDATES]:
            try:
                x = float(candidate.get("x", 0))
                y = float(candidate.get("y", 0))
                description = candidate.get("description", "")
                key = candidate.get("key", description)

                if not key:
                    continue

                if key in already_clicked:
                    continue

                if x <= 0 or y <= 0:
                    continue

                already_clicked.add(key)
                self._current_action_description = description

                self.log(
                    f"[BROWSER] Clicking candidate: {self._shorten(description)} at {x},{y}"
                )

                # Try multiple click strategies:
                # 1. Call onclick handler directly if present
                # 2. dispatchEvent on element and parent
                # 3. Native mouse click as fallback
                try:
                    page.evaluate(
                        """
                        ([cx, cy]) => {
                            const el = document.elementFromPoint(cx, cy);
                            if (!el) return;

                            // Try calling onclick directly
                            const onclickAttr = el.getAttribute('onclick');
                            if (onclickAttr) {
                                try { el.onclick && el.onclick(); } catch(e) {}
                                try { eval(onclickAttr); } catch(e) {}
                            }

                            // Walk up to find onclick
                            let parent = el.parentElement;
                            for (let i = 0; i < 4 && parent; i++) {
                                const pOnclick = parent.getAttribute('onclick');
                                if (pOnclick) {
                                    try { parent.onclick && parent.onclick(); } catch(e) {}
                                    try { eval(pOnclick); } catch(e) {}
                                    break;
                                }
                                parent = parent.parentElement;
                            }

                            // Also dispatchEvent
                            el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, clientX: cx, clientY: cy}));
                        }
                        """,
                        [x, y]
                    )
                except Exception:
                    pass

                page.mouse.click(x, y)

                clicked_count += 1
                diagnostics.append(
                    f"Clicked play/listen/quality candidate: {description}"
                )

                page.wait_for_timeout(2200)

            except Exception as ex:
                diagnostics.append(
                    f"Failed to click candidate: {type(ex).__name__}: {ex}"
                )

            finally:
                self._current_action_description = ""

        diagnostics.append(f"Clicked play/listen/quality candidates: {clicked_count}")

        return clicked_count

    def _find_click_candidates(self, page, diagnostics: List[str]):
        try:
            candidates = page.evaluate(
                """
                () => {
                    const result = [];

                    const actionRegex = /(^|[^a-zа-яёіїєґ0-9])(play|listen|start|слухати|слушать|слухай|onair|on-air)([^a-zа-яёіїєґ0-9]|$)/i;
                    const qualityRegex = /(^|[^a-z0-9])(hd|hq|high|quality|definition|bitrate|kbps|128|192|256|320)([^a-z0-9]|$)/i;

                    const negativeWords = [
                        'pause',
                        'stop',
                        'advert',
                        'ads',
                        'ad-',
                        'banner',
                        'google',
                        'doubleclick',
                        'bidmatic',
                        'cookie',
                        'consent',
                        'privacy',
                        'facebook',
                        'twitter',
                        'telegram',
                        'instagram',
                        'share',
                        'social',
                        'playlist',
                        'track',
                        'artist',
                        'song',
                        'welcome to',
                        'fundingchoices',
                        'fc-header',
                        'listen to the station',
                        'listen to the best',
                        'listen to hard',
                        'listen to heavy',
                        'listen to metal',
                        'listen to rock',
                        'listen to jazz',
                        'listen to pop',
                        'listen to classic',
                        'radio stations',
                        '/genre/',
                        'similar',
                        'station_similar'
                    ];

                    function normalize(value) {
                        return String(value || '').toLowerCase();
                    }

                    function hasAction(value) {
                        return actionRegex.test(normalize(value));
                    }

                    function hasQuality(value) {
                        return qualityRegex.test(normalize(value));
                    }

                    function hasNegative(value) {
                        const lower = normalize(value);
                        return negativeWords.some(word => lower.includes(word));
                    }

                    function isVisible(element) {
                        const style = window.getComputedStyle(element);
                        const rect = element.getBoundingClientRect();

                        if (!rect) {
                            return false;
                        }

                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                            return false;
                        }

                        if (rect.width < 8 || rect.height < 8) {
                            return false;
                        }

                        if (rect.bottom < 0 || rect.right < 0) {
                            return false;
                        }

                        if (rect.top > window.innerHeight || rect.left > window.innerWidth) {
                            return false;
                        }

                        return true;
                    }

                    function isInteractive(element) {
                        const tagName = element.tagName.toLowerCase();
                        const id = (element.id || '').toLowerCase();
                        const cls = (element.className || '').toLowerCase();

                        return tagName === 'button'
                            || tagName === 'a'
                            || element.getAttribute('role') === 'button'
                            || !!element.getAttribute('onclick')
                            || !!element.getAttribute('data-action')
                            || !!element.getAttribute('data-play')
                            || !!element.getAttribute('data-player')
                            // img/div/span with play-related id or class
                            || /play/.test(id)
                            || /listen/.test(id)
                            || /play(-button|btn|icon)?/.test(cls);
                    }

                    function getAttributeHaystack(element) {
                        return [
                            element.tagName,
                            element.id,
                            element.className,
                            element.getAttribute('role'),
                            element.getAttribute('aria-label'),
                            element.getAttribute('title'),
                            element.getAttribute('alt'),
                            element.getAttribute('data-action'),
                            element.getAttribute('data-player'),
                            element.getAttribute('data-play'),
                            element.getAttribute('data-url'),
                            element.getAttribute('data-quality'),
                            element.getAttribute('data-bitrate'),
                            element.getAttribute('href')
                        ].map(normalize).join(' ');
                    }

                    function getTextHaystack(element) {
                        const text = normalize(element.textContent);

                        if (text.startsWith('http') && text.includes('play.tavr.media')) {
                            return '';
                        }

                        if (text.length > 80) {
                            return '';
                        }

                        return text;
                    }

                    function isNavigationAnchor(element, attributeHaystack, textHaystack) {
                        const tagName = element.tagName.toLowerCase();

                        if (tagName !== 'a') {
                            return false;
                        }

                        const href = normalize(element.getAttribute('href'));

                        if (!href) {
                            return false;
                        }

                        if (href === '#' || href.startsWith('#') || href.startsWith('javascript:')) {
                            return false;
                        }

                        if (hasAction(attributeHaystack) || hasQuality(attributeHaystack)) {
                            return false;
                        }

                        return true;
                    }

                    const selector = [
                        'button',
                        '[role="button"]',
                        '[onclick]',
                        '[title*="play" i]',
                        '[title*="listen" i]',
                        '[title*="definition" i]',
                        '[title*="quality" i]',
                        '[aria-label*="play" i]',
                        '[aria-label*="listen" i]',
                        '[class*="play" i]',
                        '[id*="play" i]',
                        '[class*="listen" i]',
                        '[id*="listen" i]',
                        '[class*="audio-play" i]',
                        '[class*="player-play" i]',
                        '[class*="jp-play" i]',
                        '[class*="glyphicon-play" i]',
                        '[class*="btn-primary" i]',
                        '[class*="btn-play" i]',
                        '[id*="radio-stream" i]',
                        '[id*="player" i]',
                        '[class*="hd" i]',
                        '[id*="hd" i]',
                        '[class*="hq" i]',
                        '[id*="hq" i]',
                        '[data-action]',
                        '[data-play]',
                        '[data-player]',
                        '[data-quality]',
                        '[data-bitrate]',
                        'a[href="#"]',
                        'a[href^="javascript:"]'
                    ].join(',');

                    const elements = Array.from(document.querySelectorAll(selector));

                    for (const element of elements) {
                        if (!isVisible(element)) {
                            continue;
                        }

                        if (!isInteractive(element)) {
                            continue;
                        }

                        const attributeHaystack = getAttributeHaystack(element);
                        const textHaystack = getTextHaystack(element);
                        const fullHaystack = attributeHaystack + ' ' + textHaystack;

                        if (hasNegative(fullHaystack)) {
                            continue;
                        }

                        const attributeHasAction = hasAction(attributeHaystack);
                        const textHasAction = hasAction(textHaystack);
                        const attributeHasQuality = hasQuality(attributeHaystack);
                        const textHasQuality = hasQuality(textHaystack);

                        if (!attributeHasAction && !textHasAction && !attributeHasQuality && !textHasQuality) {
                            continue;
                        }

                        if (isNavigationAnchor(element, attributeHaystack, textHaystack)) {
                            continue;
                        }

                        const rect = element.getBoundingClientRect();

                        let score = 0;

                        if (attributeHasQuality) {
                            score += 90;
                        }

                        if (attributeHasAction) {
                            score += 70;
                        }

                        if (textHasAction) {
                            score += 35;
                        }

                        if (textHasQuality) {
                            score += 20;
                        }

                        if (element.tagName.toLowerCase() === 'button') {
                            score += 25;
                        }

                        if (element.getAttribute('role') === 'button') {
                            score += 20;
                        }

                        if (hasAction(element.getAttribute('aria-label'))) {
                            score += 25;
                        }

                        if (hasAction(element.getAttribute('title'))) {
                            score += 20;
                        }

                        if (hasQuality(element.getAttribute('title'))) {
                            score += 80;
                        }

                        const classAndId = normalize(element.className + ' ' + element.id);

                        if (/(^|[^a-z0-9])play([^a-z0-9]|$)/i.test(classAndId)) {
                            score += 35;
                        }

                        if (/(^|[^a-z0-9])listen([^a-z0-9]|$)/i.test(classAndId)) {
                            score += 30;
                        }

                        if (/(^|[^a-z0-9])(hd|hq)([^a-z0-9]|$)/i.test(classAndId)) {
                            score += 80;
                        }

                        const description = [
                            element.tagName.toLowerCase(),
                            element.id ? '#' + element.id : '',
                            element.className ? '.' + String(element.className).replace(/\\s+/g, '.') : '',
                            element.getAttribute('role') ? 'role=' + element.getAttribute('role') : '',
                            element.getAttribute('aria-label') ? 'aria=' + element.getAttribute('aria-label') : '',
                            element.getAttribute('title') ? 'title=' + element.getAttribute('title') : '',
                            element.getAttribute('data-quality') ? 'data-quality=' + element.getAttribute('data-quality') : '',
                            element.getAttribute('data-bitrate') ? 'data-bitrate=' + element.getAttribute('data-bitrate') : '',
                            element.getAttribute('href') ? 'href=' + element.getAttribute('href') : '',
                            element.textContent ? 'text=' + element.textContent.trim().slice(0, 80) : ''
                        ].filter(x => !!x).join(' ');

                        const key = [
                            element.tagName.toLowerCase(),
                            element.id || '',
                            element.className || '',
                            element.getAttribute('role') || '',
                            element.getAttribute('aria-label') || '',
                            element.getAttribute('title') || '',
                            element.getAttribute('data-quality') || '',
                            element.getAttribute('data-bitrate') || '',
                            element.getAttribute('href') || '',
                            Math.round(rect.left),
                            Math.round(rect.top),
                            Math.round(rect.width),
                            Math.round(rect.height)
                        ].map(normalize).join('|');

                        result.push({
                            x: rect.left + rect.width / 2,
                            y: rect.top + rect.height / 2,
                            score: score,
                            description: description,
                            key: key
                        });
                    }

                    result.sort((a, b) => b.score - a.score);
                    return result.slice(0, 40);
                }
                """
            )

            for item in candidates or []:
                diagnostics.append(
                    f"Click candidate. Score: {item.get('score')}, Description: {item.get('description')}"
                )

            return candidates or []

        except Exception as ex:
            diagnostics.append(
                f"Failed to find click candidates: {type(ex).__name__}: {ex}"
            )
            return []

    def _dom_click_by_key_script(self) -> str:
        return """
        (targetKey) => {
            function normalize(value) {
                return String(value || '').toLowerCase();
            }

            function makeKey(element) {
                const rect = element.getBoundingClientRect();

                return [
                    element.tagName.toLowerCase(),
                    element.id || '',
                    element.className || '',
                    element.getAttribute('role') || '',
                    element.getAttribute('aria-label') || '',
                    element.getAttribute('title') || '',
                    element.getAttribute('data-quality') || '',
                    element.getAttribute('data-bitrate') || '',
                    element.getAttribute('href') || '',
                    Math.round(rect.left),
                    Math.round(rect.top),
                    Math.round(rect.width),
                    Math.round(rect.height)
                ].map(normalize).join('|');
            }

            const selector = [
                'button[title]',
                '[role="button"][title]',
                '[onclick][title]',
                'button[aria-label]',
                '[role="button"][aria-label]',
                '[onclick][aria-label]'
            ].join(',');

            const elements = Array.from(document.querySelectorAll(selector));

            for (const element of elements) {
                if (makeKey(element) !== targetKey) {
                    continue;
                }

                try {
                    element.scrollIntoView({
                        block: 'center',
                        inline: 'center'
                    });
                } catch (e) {
                }

                const rect = element.getBoundingClientRect();
                const x = rect.left + rect.width / 2;
                const y = rect.top + rect.height / 2;

                try {
                    element.dispatchEvent(new PointerEvent('pointerdown', {
                        bubbles: true,
                        cancelable: true,
                        clientX: x,
                        clientY: y
                    }));
                } catch (e) {
                }

                try {
                    element.dispatchEvent(new MouseEvent('mousedown', {
                        bubbles: true,
                        cancelable: true,
                        clientX: x,
                        clientY: y
                    }));

                    element.dispatchEvent(new MouseEvent('mouseup', {
                        bubbles: true,
                        cancelable: true,
                        clientX: x,
                        clientY: y
                    }));

                    element.dispatchEvent(new MouseEvent('click', {
                        bubbles: true,
                        cancelable: true,
                        clientX: x,
                        clientY: y
                    }));
                } catch (e) {
                }

                try {
                    element.click();
                } catch (e) {
                }

                return true;
            }

            return false;
        }
        """

    # ---------------------------------------------------------------------
    # URL extraction and candidate handling
    # ---------------------------------------------------------------------

    def _extract_stream_urls_from_text(self, text: str) -> List[str]:
        if not text:
            return []

        normalized = self._normalize_text_for_url_scan(text)

        result: List[str] = []
        seen = set()

        def add_url(raw_url: str, require_potential_stream: bool):
            cleaned = self._clean_url(raw_url)

            if not cleaned:
                return

            if self._is_noise_or_ad_url(cleaned):
                return

            if require_potential_stream and not self._is_url_potentially_stream(cleaned):
                return

            key = cleaned.lower()

            if key in seen:
                return

            seen.add(key)
            result.append(cleaned)

        # First parse JSON semantically. This catches fields such as:
        # - radio.net: broadcastUrl
        # - onlineradiobox: stream
        for candidate in self._extract_urls_from_json_like_text(normalized):
            add_url(candidate, require_potential_stream=False)

        # Then scan raw text with stream-specific and generic URL regexes.
        for match in STREAM_URL_PATTERN.findall(normalized):
            add_url(match, require_potential_stream=True)

        for match in HTTP_URL_PATTERN.findall(normalized):
            add_url(match, require_potential_stream=True)

        return result

    def _extract_urls_from_json_like_text(self, text: str) -> List[str]:
        result: List[str] = []

        if not text:
            return result

        try:
            data = json.loads(text)
        except Exception:
            return result

        strong_keys = {
            "stream",
            "streamurl",
            "stream_url",
            "broadcasturl",
            "broadcast_url",
            "playbackurl",
            "playback_url",
            "audio",
            "audiourl",
            "audio_url",
            "mediaurl",
            "media_url",
            "file",
            "src",
            "source"
        }

        weak_keys = {
            "url",
            "href",
            "link"
        }

        def walk(value, parent_key: str = ""):
            if isinstance(value, dict):
                for key, child in value.items():
                    walk(child, str(key or "").lower())
                return

            if isinstance(value, list):
                for child in value:
                    walk(child, parent_key)
                return

            if not isinstance(value, str):
                return

            candidate = value.strip()

            if not candidate.startswith(("http://", "https://")):
                return

            normalized_key = parent_key.replace("-", "").replace("_", "").lower()

            if normalized_key in strong_keys:
                result.append(candidate)
                return

            if normalized_key in weak_keys and self._is_url_potentially_stream(candidate):
                result.append(candidate)
                return

            if self._is_url_potentially_stream(candidate):
                result.append(candidate)

        walk(data)

        return result

    def _normalize_text_for_url_scan(self, text: str) -> str:
        normalized = str(text)

        normalized = normalized.replace("\\/", "/")
        normalized = normalized.replace("&amp;", "&")
        normalized = normalized.replace("\\u0026", "&")
        normalized = normalized.replace("\\u003d", "=")
        normalized = normalized.replace("\\u003a", ":")
        normalized = normalized.replace("\\u002f", "/")

        try:
            normalized = unquote(normalized)
        except Exception:
            pass

        return normalized

    def _append_candidate(
        self,
        candidates: List[StreamCandidate],
        url: str,
        source: str,
        confidence: int,
        reason: str,
        origin_url: str,
        origin_type: str,
        action_description: str,
        infer_quality_from_action: bool,
        captured_content_type: str = "",
        captured_status_code: int = 0
    ):
        cleaned = self._clean_url(url)

        if not cleaned:
            return

        if self._is_js_template_or_malformed_url(cleaned):
            return

        if self._is_noise_or_ad_url(cleaned):
            return

        quality_hint, quality_score = self._infer_quality(
            url=cleaned,
            action_description=action_description if infer_quality_from_action else ""
        )

        stable_url = self._strip_query(cleaned)
        is_temporary = self._has_query(cleaned)

        candidate = StreamCandidate(
            url=cleaned,
            title="",
            source=source,
            confidence=confidence,
            reason=reason,
            originUrl=origin_url,
            originType=origin_type,
            originalUrl=cleaned,
            stableUrl=stable_url,
            originAction=action_description,
            qualityHint=quality_hint,
            qualityScore=quality_score,
            isTemporary=is_temporary,
            requiresFreshDiscovery=is_temporary
        )

        # Store content_type and status captured at network interception time
        # so _validate_candidates can use them for pre-validation.
        if captured_content_type:
            candidate.contentType = captured_content_type
        if captured_status_code:
            candidate.httpStatusCode = captured_status_code

        candidates.append(candidate)

    def _add_query_stripped_candidates(
        self,
        candidates: List[StreamCandidate],
        diagnostics: List[str]
    ) -> List[StreamCandidate]:
        result: List[StreamCandidate] = list(candidates)

        for candidate in candidates:
            if not candidate.url:
                continue

            stripped_url = self._strip_query(candidate.url)

            if not stripped_url:
                continue

            if stripped_url.lower() == candidate.url.lower():
                continue

            diagnostics.append(
                f"Added query-stripped candidate for validation: {self._shorten(stripped_url)}"
            )

            result.append(StreamCandidate(
                url=stripped_url,
                title=candidate.title,
                source="browser_network_canonical",
                confidence=max(candidate.confidence - 5, 60),
                reason="Query-stripped variant derived from captured browser/runtime media URL.",
                originUrl=candidate.url,
                originType="query_stripped_variant",
                originalUrl=candidate.originalUrl or candidate.url,
                stableUrl=stripped_url,
                originAction=candidate.originAction,
                qualityHint=candidate.qualityHint,
                qualityScore=candidate.qualityScore,
                isTemporary=False,
                requiresFreshDiscovery=False
            ))

        return result

    def _validate_candidates(self, candidates: List[StreamCandidate]) -> List[StreamCandidate]:
        validated: List[StreamCandidate] = []

        for candidate in candidates:
            # Кандидаты пойманные Playwright напрямую как audio/* или media —
            # они уже валидированы самим браузером, дополнительная проверка не нужна
            # и может зависнуть (голые IP, нестандартные порты, Icecast).
            already_confirmed = (
                candidate.source == "browser_network"
                and candidate.contentType
                and (
                    candidate.contentType.lower().startswith("audio/")
                    or "mpeg" in candidate.contentType.lower()
                    or "ogg" in candidate.contentType.lower()
                    or "mpegurl" in candidate.contentType.lower()
                )
                # application/octet-stream is used by analytics/telemetry,
                # NOT a reliable indicator of audio stream
                and "octet-stream" not in candidate.contentType.lower()
            )

            if already_confirmed:
                # HTTP 206 Partial Content = браузер запросил Range для файла.
                # Живые потоки всегда отвечают 200. Исключаем 206-кандидаты
                # с путями файлового хранилища (/upload/, /files/, /media/ и т.п.)
                status = candidate.httpStatusCode or 0
                url_lower = (candidate.url or "").lower()
                is_file_download = (
                    status == 206
                    and any(token in url_lower for token in (
                        "/upload/", "/uploads/", "/files/", "/file/",
                        "/media/", "/content/", "/iblock/", "/storage/",
                        "/static/", "/assets/", "/audio/tracks/", "/tracks/"
                    ))
                )

                if is_file_download:
                    candidate.isPlayable = False
                    candidate.reason = candidate.reason + " Rejected: HTTP 206 from file storage path — likely a track, not a live stream."
                    continue

                candidate.isPlayable = True
                candidate.httpStatusCode = candidate.httpStatusCode or 200
                candidate.finalUrl = candidate.finalUrl or candidate.url
                candidate.confidence = max(candidate.confidence, 95)
                candidate.reason = candidate.reason + " Pre-validated: captured directly as audio stream by browser."
                validated.append(candidate)
                continue

            validation = self.validator.validate(candidate.url)

            candidate.isPlayable = validation.is_playable
            candidate.httpStatusCode = validation.status_code
            candidate.contentType = validation.content_type
            candidate.finalUrl = validation.final_url

            if validation.is_playable:
                candidate.confidence = max(candidate.confidence, 95)

                if candidate.source == "browser_network_canonical":
                    candidate.reason = candidate.reason + " Query-stripped variant validated as playable audio stream."
                else:
                    candidate.reason = candidate.reason + " Validated as playable audio stream."

                validated.append(candidate)
            else:
                candidate.reason = candidate.reason + f" Validation failed: {validation.reason}"

        return self._collapse_temporary_duplicates(self._sort_candidates(validated))

    def _collapse_temporary_duplicates(self, candidates: List[StreamCandidate]) -> List[StreamCandidate]:
        """
        Если для одного потока есть и временный URL (с nonce/query, isTemporary=True)
        и валидированный стабильный (query-stripped, isTemporary=False) —
        убираем временный дубликат из финального результата.
        Это решает проблему с 4 кандидатами вместо 2 для радио рокс.
        """
        stable_keys: set = set()

        for candidate in candidates:
            stable_url = (candidate.stableUrl or candidate.url or "").lower()
            if stable_url and not candidate.isTemporary:
                stable_keys.add(stable_url)

        result = []
        for candidate in candidates:
            stable_url = (candidate.stableUrl or candidate.url or "").lower()
            if candidate.isTemporary and stable_url in stable_keys:
                continue
            result.append(candidate)

        return result

    def _sort_candidates(self, candidates: List[StreamCandidate]) -> List[StreamCandidate]:
        return sorted(
            candidates,
            key=lambda x: (
                x.qualityScore,
                0 if x.isTemporary else 1,
                x.confidence
            ),
            reverse=True
        )

    def _deduplicate_candidates(self, candidates: List[StreamCandidate]) -> List[StreamCandidate]:
        result: List[StreamCandidate] = []
        seen = set()

        for candidate in candidates:
            if not candidate.url:
                continue

            key = candidate.url.lower()

            if key in seen:
                continue

            seen.add(key)
            result.append(candidate)

        return self._sort_candidates(result)

    # ---------------------------------------------------------------------
    # Filtering and classification
    # ---------------------------------------------------------------------

    def _is_js_template_or_malformed_url(self, url: str) -> bool:
        """
        Отсеивает URL, которые являются артефактами JS-кода, а не настоящими
        адресами потоков. Например:
          http://cdn.onlineradiobox.com/js/'+href+
          http://cdn.onlineradiobox.com/js/"+opts.stream+
          https://${t}.${this.scriptDomain}/microplayer`;return
        """
        if not url:
            return True

        value = str(url).strip()
        lower = value.lower()

        if not lower.startswith(("http://", "https://")):
            return True

        bad_tokens = [
            "'+", "+'", '"+', '+"', "`+", "+`",
            "+opts.", "+data.", "+href", "+src", "+stream",
            "${", "};", "{", "%22+", "%27+",
            "quote/", "undefined", "null/"
        ]

        if any(token in value for token in bad_tokens):
            return True

        if any(ch in value for ch in ['"', "'", "`", "<", ">", "\\"]):
            return True

        try:
            parsed = urlparse(lower)
        except Exception:
            return True

        path = parsed.path.lower()
        host = parsed.netloc.lower()

        static_extensions = (
            ".js", ".css", ".png", ".jpg", ".jpeg", ".webp", ".gif",
            ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".map"
        )

        if path.endswith(static_extensions):
            return True

        if "cdn.onlineradiobox.com" in host and path.startswith("/js/"):
            return True

        if "onlineradiobox.com" in host and (
            path.startswith("/track/")
            or path.startswith("/ping/")
            or path.startswith("/js/")
        ):
            return True

        return False

    def _is_network_stream_like_url(
        self,
        url: str,
        content_type: str,
        resource_type: str,
        status_code: int
    ) -> bool:
        if not url:
            return False

        if status_code < 200 or status_code >= 400:
            return False

        cleaned = self._clean_url(url)

        if self._is_noise_or_ad_url(cleaned):
            return False

        lower_content_type = (content_type or "").lower()
        lower_resource_type = (resource_type or "").lower()

        if lower_resource_type == "document":
            return False

        if "text/html" in lower_content_type:
            return False

        if "text/css" in lower_content_type:
            return False

        if "javascript" in lower_content_type:
            return False

        if lower_resource_type == "media":
            return True

        if lower_content_type.startswith("audio/"):
            return True

        if "mpegurl" in lower_content_type:
            return True

        if "application/vnd.apple.mpegurl" in lower_content_type:
            return True

        if "application/x-mpegurl" in lower_content_type:
            return True

        if "application/ogg" in lower_content_type:
            return True

        return self._is_url_potentially_stream(cleaned)

    def _is_url_potentially_stream(self, url: str) -> bool:
        if not url:
            return False

        cleaned = self._clean_url(url)

        if self._is_noise_or_ad_url(cleaned):
            return False

        lower = cleaned.lower()
        parsed = urlparse(lower)
        host = parsed.netloc.lower()
        path = parsed.path.lower()

        blocked_extensions = [
            ".css",
            ".js",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".gif",
            ".svg",
            ".ico",
            ".woff",
            ".woff2",
            ".ttf",
            ".eot",
            ".mp4",
            ".webm",
            ".avi",
            ".mov",
            ".pdf",
            ".zip",
            ".rar",
            ".7z",
            ".json"
        ]

        if any(path.endswith(ext) for ext in blocked_extensions):
            return False

        non_stream_path_tokens = [
            "/music/",
            "/artist/",
            "/artists/",
            "/song/",
            "/songs/",
            "/news/",
            "/article/",
            "/blog/",
            "/static/",
            "/assets/",
            "/privacy",
            "/terms"
        ]

        if any(token in path for token in non_stream_path_tokens):
            return False

        playlist_extensions = [
            ".m3u8",
            ".m3u",
            ".pls",
            ".xspf"
        ]

        if any(path.endswith(ext) for ext in playlist_extensions):
            return True

        audio_extensions = [
            ".mp3",
            ".aac",
            ".ogg",
            ".opus",
            ".flac",
            ".wav"
        ]

        if any(path.endswith(ext) for ext in audio_extensions):
            return True

        stream_path_tokens = [
            "/stream",
            "/streams",
            "/live",
            "/listen",
            "/icecast",
            "/shoutcast",
            "/audio",
            "/aac",
            "/mp3",
            "/play/",
            "/hls/",
            "/dash/",
            "/playlist",
            "/manifest",
            "/chunklist",
            "/index.m3u",
            "/master"
        ]

        if any(token in path for token in stream_path_tokens):
            return True

        stream_host_tokens = [
            "stream",
            "streams",
            "icecast",
            "shoutcast",
            "listen",
            "live",
            "webradio",
            "cast"
        ]

        if any(token in host for token in stream_host_tokens):
            return True

        if self._looks_like_extensionless_audio_endpoint(host, path):
            return True

        return False

    def _looks_like_extensionless_audio_endpoint(self, host: str, path: str) -> bool:
        if not host:
            return False

        # Голый IP-адрес как хост — почти всегда Icecast/Shoutcast стрим
        import re as _re
        if _re.fullmatch(r'[0-9]+[.][0-9]+[.][0-9]+[.][0-9]+(:[0-9]+)?', host):
            return True

        # Нестандартный порт (не 80/443/8080/8443) — признак Icecast/Shoutcast/SHOUTcast
        # Типичные порты: 8000, 8444, 9720, 19800 и т.д.
        port_match = _re.search(r':(\d+)$', host)
        if port_match:
            port = int(port_match.group(1))
            if port not in (80, 443, 8080, 8443):
                return True

        if not path or path == "/":
            return False

        last_segment = path.rsplit("/", 1)[-1]

        if not last_segment:
            return False

        if "." in last_segment:
            return False

        if len(path) > 180:
            return False

        non_stream_path_tokens = [
            "/api/",
            "/page/",
            "/news/",
            "/article/",
            "/blog/",
            "/static/",
            "/assets/",
            "/music/",
            "/artist/",
            "/song/",
            "/songs/",
            "/playlist/",
            "/privacy",
            "/terms"
        ]

        if any(token in path for token in non_stream_path_tokens):
            return False

        host_tokens = [
            "cdn",
            "media",
            "audio",
            "cast",
            "ice",
            "stream",
            "player"
        ]

        if any(token in host for token in host_tokens):
            return True

        return False

    def _is_noise_or_ad_url(self, url: str) -> bool:
        if not url:
            return False

        lower = url.lower()

        noise_tokens = [
            "1-second-of-silence",
            "silence.mp3",
            "/silence",
            "blank.mp3",
            "empty.mp3",
            "/demo/site/audio/",
            "/demo/audio/",
            "/favicon",
            "/logo",
            "/sprite",
            "/analytics",
            "/tracker",
            "/pixel"
        ]

        if any(token in lower for token in noise_tokens):
            return True

        ad_tokens = [
            "doubleclick.net",
            "googlesyndication.com",
            "googletagmanager.com",
            "google-analytics.com",
            "googleadservices.com",
            "securepubads.g.doubleclick.net",
            "fundingchoicesmessages.google.com",
            "pagead2.googlesyndication.com",
            "adservice.",
            "adserver.",
            "adsystem.",
            "bidmatic.",
            "adform.",
            "admixer.",
            "creativecdn.com",
            "prebid.",
            "casalemedia.com",
            "dsum-sec.",
            "rubiconproject.com",
            "openx.net",
            "pubmatic.com",
            "appnexus.com",
            "smartadserver.com",
            "truste.com",
            "trustarc.com",
            "aomedia.org",
            "w3.org",
            "schema.org",
            "xmlns.com",
            "fuseplatform.net",
            "btloader.com",
            "inmobi-choice.io",
            "inmobi.com",
            "navvy.media.net",
            "pb-logs.media.net",
            "unrulymedia.com",
            "liveramp.",
            "mediarithmics.",
            "smartstream.tv",
            "mediametrie.",
            "streamonkey.de",
            "r2b2.io",
            "id5-sync.com",
            "fuseplatform.",
            "/telemetry/",
            "/noconsent",
            "/prebid",
            "/pv?",
            "/exd?",
            "doubleverify.com",
            "adnxs.com",
            "moatads.com",
            "adsafeprotected.com",
            "360yield.com",
            "yieldlab.net",
            "yieldmo.com",
            "criteo.com",
            "criteo.net",
            "taboola.com",
            "outbrain.com",
            "sharethrough.com",
            "spotxchange.com",
            "spotx.tv",
            "liveintent.com",
            "mediamath.com",
            "turn.com",
            "adsrvr.org",
            "thetradedesk.com",
            "quantserve.com",
            "scorecardresearch.com",
            "bluekai.com",
            "demdex.net",
            "everesttech.net",
            "2mdn.net",
            "match?",
            "/match?",
            "sync?",
            "/sync?",
            "usersync",
            "user-sync",
            "cookiesync",
            "cookie-sync",
            "/ads/",
            "/advert",
            "/banner",
            "/bids",
            "/win-notify",
            "/imp-delivery",
            "/rum?"
        ]

        if any(token in lower for token in ad_tokens):
            return True

        return False

    def _get_candidate_confidence(
        self,
        url: str,
        content_type: str,
        resource_type: str
    ) -> int:
        lower_url = (url or "").lower()
        lower_content_type = (content_type or "").lower()
        lower_resource_type = (resource_type or "").lower()

        if lower_resource_type == "media":
            return 96

        if lower_content_type.startswith("audio/"):
            return 96

        if "mpegurl" in lower_content_type:
            return 90

        if ".m3u8" in lower_url:
            return 90

        if "/stream" in lower_url:
            return 85

        return 72

    def _infer_quality(self, url: str, action_description: str):
        haystack = f"{url or ''} {action_description or ''}".lower()

        if (
            re.search(r"(^|[^a-z0-9])hd([^a-z0-9]|$)", haystack)
            or re.search(r"(^|[^a-z0-9])hq([^a-z0-9]|$)", haystack)
            or "high definition" in haystack
            or "high quality" in haystack
            or "320kbps" in haystack
            or "320k" in haystack
            or "256kbps" in haystack
            or "256k" in haystack
        ):
            return "hd", 100

        if (
            "192kbps" in haystack
            or "192k" in haystack
            or "128kbps" in haystack
            or "128k" in haystack
        ):
            return "high", 80

        if (
            "standard definition" in haystack
            or "standard quality" in haystack
            or "low quality" in haystack
            or re.search(r"(^|[^a-z0-9])sd([^a-z0-9]|$)", haystack)
        ):
            return "standard", 50

        return "unknown", 0

    def _strip_query(self, url: str) -> str:
        if not url:
            return ""

        parsed = urlparse(url)

        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            "",
            ""
        ))

    def _has_query(self, url: str) -> bool:
        if not url:
            return False

        parsed = urlparse(url)
        return bool(parsed.query)

    def _clean_url(self, url: str) -> str:
        if not url:
            return ""

        cleaned = str(url).strip().strip("\"'")

        # Strip trailing punctuation BUT preserve trailing semicolon after slash
        # since /; is a valid Shoutcast stream path.
        if cleaned.endswith(";") and cleaned.endswith("/;"):
            pass  # keep /; intact
        else:
            cleaned = cleaned.rstrip(".,;)]}")

        cleaned = cleaned.replace("\\/", "/")
        cleaned = cleaned.replace("&amp;", "&")
        cleaned = cleaned.replace("\\u0026", "&")
        cleaned = cleaned.replace("\\u003d", "=")
        cleaned = cleaned.replace("\\u003a", ":")
        cleaned = cleaned.replace("\\u002f", "/")

        return cleaned

    def _shorten(self, value: str, max_length: int = 280) -> str:
        if not value:
            return ""

        if len(value) <= max_length:
            return value

        return value[:max_length] + "...[truncated]"

    def _unique_strings(self, values: List[str]) -> List[str]:
        result: List[str] = []
        seen = set()

        for value in values:
            if not value:
                continue

            key = value.lower()

            if key in seen:
                continue

            seen.add(key)
            result.append(value)

        return result