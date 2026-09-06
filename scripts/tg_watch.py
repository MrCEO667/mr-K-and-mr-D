"""Read new group messages and reply. Used to answer the operators in
Telegram while nobody is at the machine.

Offset is kept in data/.tg_offset so a message is read once and not replayed.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OFFSET_FILE = ROOT / "data" / ".tg_offset"
CHAT_ID = -1003790327906
API = "https://api.telegram.org/bot{token}/{method}"


def token() -> str:
    env = ROOT / ".env"
    if not env.exists():
        sys.exit(f"no {env}; copy .env.example and fill in TELEGRAM_BOT_TOKEN")
    for line in env.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "TELEGRAM_BOT_TOKEN" and value.strip():
            return value.strip()
    sys.exit("no TELEGRAM_BOT_TOKEN")


def call(method: str, params: dict | None = None) -> dict:
    url = API.format(token=token(), method=method)
    data = urllib.parse.urlencode(params).encode() if params else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=40) as r:
        payload = json.load(r)
    if not payload.get("ok"):
        # Telegram answers a rejected call with 200 and ok:false. Reading
        # ["result"] off that raises KeyError and hides what went wrong.
        sys.exit(f"telegram {method} failed: {payload.get('description', payload)}")
    return payload


def read_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def poll() -> list[dict]:
    params = {"timeout": 0}
    offset = read_offset()
    if offset:
        params["offset"] = offset
    result = call("getUpdates", params).get("result", [])
    messages = []
    for update in result:
        msg = update.get("message") or update.get("edited_message") or {}
        if not msg.get("text"):
            continue
        who = msg.get("from") or {}
        messages.append(
            {
                "from": who.get("first_name") or who.get("username") or "?",
                "id": who.get("id"),
                "text": msg["text"],
                "date": msg.get("date"),
            }
        )
    # The offset is committed only once the batch has been turned into
    # messages. Advancing it first meant a crash here skipped those updates
    # permanently, since Telegram will not serve them again.
    if result:
        OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        OFFSET_FILE.write_text(str(result[-1]["update_id"] + 1), encoding="utf-8")
    return messages


def send(text: str) -> int:
    res = call("sendMessage", {"chat_id": CHAT_ID, "text": text,
                               "disable_web_page_preview": True})
    return res["result"]["message_id"]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "send":
        print("sent", send(sys.stdin.read()))
    else:
        for m in poll():
            print(json.dumps(m, ensure_ascii=False))
