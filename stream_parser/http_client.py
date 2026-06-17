import sys

import requests


class HttpClient:
    def __init__(self, timeout: int = 15, debug: bool = False):
        self.timeout = timeout
        self.debug = debug

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "application/json;q=0.9,text/plain;q=0.8,*/*;q=0.7",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        })

    def log(self, message: str):
        if self.debug:
            print(message, file=sys.stderr)

    def download_text(self, url: str) -> str:
        self.log(f"[HTTP] Downloading text: {url}")

        response = self.session.get(
            url,
            timeout=self.timeout,
            allow_redirects=True
        )

        self.log(
            f"[HTTP] Status={response.status_code}, "
            f"ContentType={response.headers.get('Content-Type', '')}, "
            f"FinalUrl={response.url}"
        )

        response.raise_for_status()

        if not response.encoding:
            response.encoding = response.apparent_encoding or "utf-8"

        return response.text

    def download_text_safe(self, url: str) -> str:
        try:
            return self.download_text(url)
        except Exception as ex:
            self.log(f"[HTTP] Failed: {url} | {type(ex).__name__}: {ex}")
            return ""