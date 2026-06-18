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
   loads and plays. Used as a fallback when the static extractor finds nothing.
   Works for JS-heavy players (MyTuner, TAVR, radio.net, etc.).

3. **Stream validator** — validates each candidate URL via HEAD/GET requests,
   handles ICY/Shoutcast responses, follows redirects, and parses M3U/PLS playlists.

Results are returned as JSON on stdout. Debug logs go to stderr.

---

## Requirements

- Python 3.9+
- A virtual environment with dependencies installed (see [Setup](#setup))
- Chromium browser installed for Playwright (see [Setup](#setup))

---

## Setup

```powershell
# Create virtual environment
python -m venv .venv

# Install dependencies
.\.venv\Scripts\pip install -r requirements.txt

# Install Chromium for Playwright
.\.venv\Scripts\playwright install chromium
```

### Dependencies (`requirements.txt`)

| Package | Purpose |
|---|---|
| `requests` | HTTP requests for static extraction and stream validation |
| `beautifulsoup4` | HTML parsing |
| `lxml` | Fast HTML/XML parser backend for BeautifulSoup |
| `playwright` | Headless Chromium browser automation |
| `greenlet` | Required by Playwright's sync API |

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
| `--url` | string | **required** | Radio station page URL or direct stream/playlist URL |
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

---

## Building the Executable

The parser can be packaged into a standalone `.exe` using PyInstaller.
The result is a folder (`dist/stream_parser/`) containing the executable
and all required DLLs.

### 1. Clean Python cache (recommended before each build)

```powershell
Get-ChildItem -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force
Get-ChildItem -Recurse -Filter "*.pyc" | Remove-Item -Force
```

### 2. Build

```powershell
.\.venv\Scripts\pyinstaller --onedir --name stream_parser --noconfirm --collect-all playwright stream_parser\main.py
```

### 3. Bundle Chromium

Playwright's Chromium browser must be copied next to the executable so it works
without a Python installation on the target machine:

```powershell
# Find installed Chromium version
Get-ChildItem "$env:LOCALAPPDATA\ms-playwright" | Select-Object Name

# Copy it into the dist folder (replace chromium-XXXX with your actual folder name)
Copy-Item "$env:LOCALAPPDATA\ms-playwright\chromium-1148" -Destination "dist\stream_parser\ms-playwright\chromium-1148" -Recurse
```

### 4. Final distribution structure

```
dist\stream_parser\
    stream_parser.exe
    ms-playwright\
        chromium-1148\
            chrome-win\
                chrome.exe
                ...
    _internal\
        ...
```

Copy the entire `dist\stream_parser\` folder to your application directory.

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