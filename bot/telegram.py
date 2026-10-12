"""Direct Telegram Bot API calls (no python-telegram-bot)."""
from __future__ import annotations

import html
import time

import requests

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # Telegram limit is 4096


def esc(v) -> str:
    return html.escape(str(v), quote=False)


class Telegram:
    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None, dry_run: bool = False):
        self.token = token
        self.chat_id = str(chat_id)
        self.s = session or requests.Session()
        self.dry_run = dry_run
        self.sent: list[str] = []  # for tests / dry runs

    def _post(self, method: str, http_timeout: float = 30, **payload):
        if self.dry_run:
            return {"ok": True, "result": {}}
        files = payload.pop("files", None)
        last = "failed"
        for attempt in range(3):
            try:
                r = self.s.post(API.format(token=self.token, method=method),
                                data=payload, files=files, timeout=http_timeout)
                try:
                    j = r.json()
                except ValueError:                      # HTML from a proxy / 5xx page
                    raise requests.RequestException(f"HTTP {r.status_code}, не JSON")
                if j.get("ok"):
                    return j
                if r.status_code == 429:
                    time.sleep(int(j.get("parameters", {}).get("retry_after", 2)))
                    continue
                if r.status_code >= 500:
                    raise requests.RequestException(f"HTTP {r.status_code}: {j.get('description')}")
                raise RuntimeError(f"Telegram {method}: {str(j.get('description')).replace(self.token, '<token>')}")
            except requests.RequestException as e:
                last = str(e).replace(self.token, "<token>")
                if files is not None and hasattr(files.get("document", (None, None))[1], "seek"):
                    files["document"][1].seek(0)
                time.sleep(2)
        raise RuntimeError(f"Telegram {method}: {last}")

    def send(self, text: str, chat_id: str | None = None, silent: bool = False) -> None:
        """Send HTML-formatted text, splitting on newlines when too long."""
        self.sent.append(text)
        chunks, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > MAX_LEN:
                chunks.append(cur)
                cur = line
            else:
                cur = f"{cur}\n{line}" if cur else line
        if cur:
            chunks.append(cur)
        for c in chunks:
            self._post("sendMessage", chat_id=chat_id or self.chat_id, text=c,
                       parse_mode="HTML", disable_web_page_preview="true",
                       disable_notification="true" if silent else "false")

    def send_document(self, path: str, caption: str = "", filename: str | None = None) -> None:
        self.sent.append(f"[document {path}] {caption}")
        if self.dry_run:
            return
        with open(path, "rb") as fh:
            self._post("sendDocument", http_timeout=60, chat_id=self.chat_id, caption=caption[:1000],
                       parse_mode="HTML", files={"document": (filename or path.split("/")[-1], fh)})

    def get_updates(self, offset: int, timeout: int = 0) -> list[dict]:
        if self.dry_run:
            return []
        j = self._post("getUpdates", http_timeout=timeout + 15, offset=offset, timeout=timeout,
                       allowed_updates='["message"]')
        return j.get("result", [])

    def get_me(self) -> dict:
        return self._post("getMe").get("result", {})
