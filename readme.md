# stream_parser

A universal internet radio stream URL extractor. Given a radio station page URL,
it discovers the direct audio stream URL suitable for playback in media players
(VLC, NAudio, etc.).

---

## How It Works

1. **Generic static extractor** — downloads the page HTML and linked JS/JSON/playlist
   files, then scans for stream-like URLs using regex and BeautifulSoup.
   Fast and cheap; works for most stations that embed the stream URL in their page source.

2. **Browser network extractor** — launches a headless Chromium browser (via Playwright),
   instruments fetch/XHR/Audio hooks, and intercepts all network traffic while the page
   loads and plays. Along the way it dismisses cookie-consent overlays, skips pre-roll
   video ads, and clicks play / "play in HD" controls — including buttons that live
   inside cross-origin iframes (e.g. TuneIn embed players). Used as a fallback when the
   static extractor finds nothing. Works for JS-heavy players (MyTuner, TAVR, radio.net,
   TuneIn-embedded WordPress sites, etc.).

3. **Stream validator** — validates each candidate URL via HEAD/GET requests,
   handles ICY/Shoutcast responses, follows redirects, and parses M3U/PLS playlists.

Results are returned as JSON on stdout. Debug logs go to stderr.

---

## Requirements

- Python 3.9+
- A virtual environment with dependencies installed (see [Setup](#setup))
- Chromium headless shell installed for Playwright (see [Setup](#setup))

---

## Setup

```powershell
# Create virtual environment
python -m venv .venv

# Install dependencies. Call pip via `python -m` rather than the
# .venv\Scripts\pip.exe wrapper: the wrapper hard-codes the venv's original
# absolute path and stops working if the project folder is ever moved.
.venv\Scripts\python.exe -m pip install -r requirements.txt

# Install the Chromium headless shell for Playwright (for running from source /
# dev mode). The parser always launches headless, so the lightweight
# headless-shell build is all that is needed — it is roughly half the size of the
# full Chromium download. For a release build, install into a local browsers\
# folder instead — see "Building the Executable".
.venv\Scripts\python.exe -m playwright install chromium-headless-shell
```

### Dependencies (`requirements.txt`)

| Package | Purpose |
|---|---|
| `requests` | HTTP requests for static extraction and stream validation |
| `beautifulsoup4` | HTML parsing |
| `lxml` | Fast HTML/XML parser backend for BeautifulSoup |
| `playwright` | Headless Chromium browser automation |
| `greenlet` | Required by Playwright's sync API |
| `pyinstaller` | Packaging the standalone `.exe` (build only) |

> **Pin the Playwright version** (e.g. `playwright==1.60.0`). The Chromium browser
> revision is tied to the exact Playwright version. If pip silently upgrades
> Playwright, the browser bundled in `browsers\` no longer matches and the exe
> fails at `Launching Chromium`. Let Playwright pull its own matching `greenlet`
> (it pins a compatible version automatically).

---

## Usage

### Production (JSON output only)

```powershell
.\.venv\Scripts\python.exe -m stream_parser.main --url "https://example.com/radio" --timeout 30
```

Output is a single JSON object on stdout. Suitable for piping into files or
reading programmatically from C# / other host processes.

```powershell
# Save result to file
.\.venv\Scripts\python.exe -m stream_parser.main --url "https://example.com/radio" --timeout 30 > result.json
```

> **Include the scheme in `--url`.** The browser engine cannot navigate to a bare
> host like `www.example.com/listen` and will fail with "Cannot navigate to
> invalid URL", producing no candidates. Always pass a full URL beginning with
> `http://` or `https://`.

### Debug mode (JSON + detailed logs)

```powershell
.\.venv\Scripts\python.exe -m stream_parser.main --url "https://www.rockfm.de/webradio/metal" --debug > result.json 2> debug.log
```

- **stdout** → JSON result
- **stderr** → step-by-step diagnostics (HTTP downloads, browser events, validation)

You can also split them in PowerShell like this to see debug output live while
still saving the JSON:

```powershell
.\.venv\Scripts\python.exe -m stream_parser.main --url "https://www.rockfm.de/webradio/metal" --debug 2>&1 | Tee-Object -FilePath debug.log | Where-Object { $_ -notlike "\[HTTP\]*" -and $_ -notlike "\[BROWSER\]*" -and $_ -notlike "\[VALIDATE\]*" }
```

### Direct stream / playlist input

You can also pass a direct stream URL or an M3U/PLS playlist directly —
the parser will validate it without running the extractors:

```powershell
# Direct Icecast stream
.\.venv\Scripts\python.exe -m stream_parser.main --url "http://stream.example.com:8000/;" --timeout 15

# M3U playlist
.\.venv\Scripts\python.exe -m stream_parser.main --url "https://example.com/static/128kbps.m3u" --timeout 15
```

---

## Arguments

| Argument | Type | Default | Description |
|---|---|---|---|
| `--url` | string | **required** | Radio station page URL or direct stream/playlist URL (must include `http://` or `https://`) |
| `--timeout` | int | `20` | Timeout in seconds for HTTP requests and browser operations |
| `--debug` | flag | off | Print detailed diagnostic logs to stderr |

---

## Output Format

```json
{
  "success": true,
  "inputUrl": "https://example.com/radio",
  "effectiveUrl": "https://example.com/radio",
  "title": "",
  "candidates": [
    {
      "url": "https://stream.example.com/live",
      "source": "browser_network",
      "confidence": 96,
      "qualityHint": "hd",
      "qualityScore": 100,
      "isPlayable": true,
      "isTemporary": false,
      "httpStatusCode": 200,
      "contentType": "audio/mpeg",
      "finalUrl": "https://stream.example.com/live",
      "stableUrl": "https://stream.example.com/live",
      "originAction": "button title=click to play in high definition"
    }
  ],
  "diagnostics": ["..."],
  "error": null
}
```

### Key fields

| Field | Description |
|---|---|
| `candidates[0]` | Best stream candidate (sorted by quality score, then confidence) |
| `finalUrl` | Actual URL after redirects — **use this for playback** |
| `stableUrl` | URL with query params stripped — use for deduplication |
| `qualityHint` | `hd`, `high`, `standard`, or `unknown` |
| `isTemporary` | `true` if the URL contains a timestamp and expires |
| `source` | How the URL was found: `browser_network`, `generic_static`, `direct_url`, etc. |

> **Important:** Always use `finalUrl` for playback, not `url`.
> Some streams redirect to a different host.

> **Note on host casing:** `url` / `stableUrl` may preserve the host casing as it
> appeared on the page (e.g. `mETaLraDio.Net`), while `finalUrl` is normalized.
> When deduplicating by `stableUrl`, compare hosts case-insensitively.

---

## Building the Executable

The parser can be packaged into a standalone `.exe` using PyInstaller. The result
is a folder (`dist\stream_parser\`) containing the executable, its dependencies,
and a bundled Chromium headless shell, so it runs on machines without Python or
Playwright installed.

> **Build and browser must come from the same venv / Playwright version.** The
> Chromium revision is pinned to the installed Playwright version. If you build
> the exe with one Playwright version but bundle a browser from another, the exe
> fails at `Launching Chromium`. Keep `playwright` pinned in `requirements.txt`
> and run all steps below from the same environment.

When running as a frozen build, `main.py` sets `PLAYWRIGHT_BROWSERS_PATH` to a
`browsers\` folder **next to the exe**, so the release looks for its browser
there instead of the per-user profile (`%LOCALAPPDATA%\ms-playwright`). All you
have to do is put the browser there.

### 1. Clean Python cache (recommended before each build)

```powershell
Get-ChildItem -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force
Get-ChildItem -Recurse -Filter "*.pyc" | Remove-Item -Force
```

### 2. Install the Chromium headless shell into a local `browsers\` folder

Point `PLAYWRIGHT_BROWSERS_PATH` at a `browsers\` folder in the project root and
install there (not into the profile), so the browser can be shipped with the
build. Install only the headless shell — the parser always launches headless, so
the full Chromium binary is not needed and would roughly double the bundle size:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\browsers"
.venv\Scripts\python.exe -m playwright install chromium-headless-shell
```

Keep this terminal open — `PLAYWRIGHT_BROWSERS_PATH` lives only in the current
session, and it is also needed by the next step so PyInstaller's `--collect-all`
does not pull the browser from the profile instead.

> If you previously ran `playwright install chromium` (which also fetches the full
> browser), you can delete the leftover full-browser folder to slim the bundle —
> keep `chromium_headless_shell-*`, remove `chromium-*`:
> ```powershell
> Remove-Item -Recurse -Force browsers\chromium-*
> ```

### 3. Build

Invoke PyInstaller via `python -m`. The `pyinstaller.exe` wrapper hard-codes the
venv path and fails with "Unable to create process" if the project folder was
ever moved; `python -m PyInstaller` avoids that.

```powershell
.venv\Scripts\python.exe -m PyInstaller --onedir --name stream_parser --noconfirm --collect-all playwright stream_parser\main.py
```

### 4. Bundle Chromium into the build

PyInstaller packages the code and the Playwright driver, but not the browser
binary — copy the `browsers\` folder next to the exe:

```powershell
Copy-Item -Recurse -Force browsers dist\stream_parser\browsers
```

### 5. Final distribution structure

The parser runs headless, so the bundled browser is the lightweight
`chromium_headless_shell-*` (the folder name carries the revision number tied to
your Playwright version):

```
dist\stream_parser\
    stream_parser.exe
    browsers\
        chromium_headless_shell-XXXX\
            chrome-win\
                headless_shell.exe
                ...
    _internal\
        ...
```

Copy the entire `dist\stream_parser\` folder to your application directory.

### 6. Verify the build is self-contained

Run the built exe **directly** (not through your venv) to confirm it finds the
bundled browser rather than your profile:

```powershell
dist\stream_parser\stream_parser.exe --url "https://play.tavr.media/radioroks/hardnheavy/" --timeout 60 --debug
```

If you see `[BROWSER] Launching Chromium` followed by stream candidates, the
bundle is self-contained and ready to distribute. A
`[BROWSER] Launch failed: Executable doesn't exist...` means the `browsers\`
folder is missing next to the exe, or its revision does not match the bundled
Playwright version (rebuild with matching versions, step 2 onward).

---

## Integration with C# (RadioApp)

The parser is designed to be called as a subprocess from the C# host application.
See `PythonStreamDiscoveryService.cs` for the integration service.

The service:
- Searches for `python.exe` in `.venv\Scripts\` next to the host `.exe`, then falls back to PATH
- Launches `stream_parser.exe` (or `python -m stream_parser.main`) with the given URL and timeout
- Reads stdout as UTF-8 JSON and deserializes the result
- Returns a `DiscoveredRadioStream` with the best candidate's `finalUrl` as `StreamUrl`
- Appends additional candidates to `Description` as `Also possible stream candidates: ...`

> **Tip:** normalize user-entered URLs before calling the parser — if the input has
> no scheme, prepend `https://`. The browser engine rejects bare hosts like
> `www.example.com/listen` ("Cannot navigate to invalid URL"), and end users
> frequently paste URLs without `http(s)://`.

> **Tip:** some pages (see "catalog-style" stations below) return many validated
> candidates for one page URL. Consider surfacing the candidate list in the UI so
> the user can pick the right sub-stream / bitrate rather than auto-assigning all
> of them to a single station.

---

## Known Limitations
- Sites that serve stream URLs exclusively via dynamically generated JS (e.g. computed
  from an API response at runtime) may not be parseable without site-specific logic.
- Some Icecast/Shoutcast servers respond with `ICY 200 OK` instead of standard HTTP —
  this is handled and treated as a valid stream.
- The browser extractor adds ~5–30 seconds of overhead depending on the site.
  Static extraction completes in under 5 seconds for most stations.
- M3U/PLS playlists are downloaded and parsed automatically, including those served
  with `application/octet-stream` content type.
- **HD/quality-toggle players** (e.g. tavr.media): when a station autoplays a
  standard-definition stream and exposes a separate "play in HD" switch, the
  parser clicks that switch to also capture the HD stream, returning both
  candidates (HD first by quality score). On ad-supported players it first skips
  the pre-roll ad so the (late-rendering) HD control can appear. HD capture is
  still timing-dependent: if a pre-roll is unskippable or outlasts the retry
  window, only the standard-definition stream is returned for that run —
  re-running usually picks up the HD stream.
- **TuneIn-embedded players** (WordPress sites that embed a
  `tunein.com/embed/player/...` iframe — e.g. croma-music themes): the real Play
  button lives inside a cross-origin iframe and only responds to a genuine
  (trusted) click, after which the embedded player resolves the stream via an
  authenticated TuneIn API call. The parser clicks inside the iframe with a real
  gesture and scrapes the resulting stream URLs (often several bitrates) from the
  response body. Ad-heavy embeds can be flaky on a given run; if a station fails,
  see the manual workflow below — the underlying Icecast stream usually accepts
  manual entry.
- **Catalog-style stations** (e.g. RockFM / ATSW / `streamabc.net`-backed sites):
  the page HTML lists every sub-station mount, so the parser may return many
  validated candidates (one per sub-stream and bitrate) for a single page URL.
  These are all genuine streams; pick the one matching the station you want by its
  mount name (e.g. `.../metal/aac-128/...`).
- **Sites gated by a cookie consent overlay (CMP):** stations behind a "Accept cookies"
  popup may not start the player until consent is granted. The parser tries known
  CMP frameworks and a multilingual accept-button fallback, but bespoke CMPs may
  still block playback. Examples include parts of `radio.de` / `radio.net` and
  similar aggregators built on Next.js. If a station from such a source fails to
  parse, see the manual workflow below.
- **SoCast-based stations** (`*.thezone.fm`, other Pattison/SoCast affiliates):
  the stream URL is fetched via an authenticated API call (`/api/v1/music/streamAction`)
  with session-bound parameters. These cannot be discovered statically and are
  treated as a known unsupported case.
- **Sites with dynamic JS-generated Play buttons** (e.g. `jungletrain.net`): the
  Play button is rendered after JS execution and points to a proxy URL that VLC
  may not accept. The underlying direct stream usually works — see the manual
  workflow below.

## Finding a Stream URL Manually

If the parser cannot detect a stream from a page URL, you can usually find it
yourself in a couple of minutes. Note that the **Stream URL is not the same as
the page URL** — the page URL is the HTML page you visit in the browser
(e.g. `https://example.com/listen`), while the Stream URL is the direct
audio endpoint that VLC, foobar2000, or any media player can open
(e.g. `https://stream.example.com/live.mp3` or `https://stream.example.com:8000/;`).

**Step-by-step:**

1. Open the radio station's page in Chrome, Edge, or Firefox.
2. Open DevTools (F12 or Ctrl+Shift+I) and switch to the **Network** tab.
3. Click the **Media** filter (or **All** if Media shows nothing).
4. Tick **Preserve log** so requests survive page reloads.
5. Click the station's Play button.
6. Look for a new entry that is either a long-running request, or one whose
   content type is `audio/mpeg`, `audio/aac`, `audio/aacp`, `application/ogg`,
   or that ends with `.mp3`, `.aac`, `.m3u8`, or has no extension but a
   non-standard port like `:8000`, `:8443`.
7. Right-click that entry → **Copy** → **Copy URL** (or **Copy link address**).
8. Paste the URL into the **Stream URL** field of the Add Radio Station window.
   You can leave **Radio page URL** filled in too — both fields can coexist.

**Tips:**
- If the first request returns HTTP `302 Found`, follow it — the redirect
  target is often the real stream and is more stable.
- Prefer the shortest URL form. Trailing tokens like
  `?session=abc123&token=...` are usually optional and may expire.
- Some Icecast servers require a trailing `/;`, `/stream`, `/live`, or `/1`
  to actually serve audio. If the URL plays in a browser but not in the
  player, try appending one of these suffixes.
