"""Discover the group chat ID and operator user IDs for config.

One-time helper, run before M8 exists. It does not write anything; it prints
the values you paste into .env and config/config.yaml.

    1. @BotFather -> /newbot -> copy the token into .env as TELEGRAM_BOT_TOKEN
    2. Create a Telegram group, add Mr K, Mr D and the bot
    3. Make it a supergroup (Group settings -> Chat history -> Visible), because
       a basic group's ID changes on upgrade and delivery breaks silently
    4. Have BOTH operators send any message starting with "/" (e.g. /hello) --
       privacy mode means the bot only receives slash-prefixed messages
    5. python scripts/telegram_setup.py
"""
import json
import os
import sys
import urllib.request

# Windows consoles default to a legacy codepage; operator names are frequently
# non-ASCII and get mangled into the config without this.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API = "https://api.telegram.org/bot{token}/{method}"


def call(token, method):
    with urllib.request.urlopen(API.format(token=token, method=method), timeout=20) as r:
        payload = json.load(r)
    if not payload.get("ok"):
        sys.exit(f"Telegram API error: {payload}")
    return payload["result"]


def load_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token
    try:
        with open(".env", encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.partition("=")
                if key.strip() == "TELEGRAM_BOT_TOKEN" and value.strip():
                    return value.strip()
    except FileNotFoundError:
        pass
    sys.exit("No TELEGRAM_BOT_TOKEN. Copy .env.example to .env and fill it in.")


def main():
    token = load_token()
    me = call(token, "getMe")
    print("Bot: @{} ({})\n".format(me["username"], me["id"]))

    updates = call(token, "getUpdates")
    if not updates:
        sys.exit(
            "No updates. Add the bot to the group, then have both operators\n"
            "send a message starting with '/' and run this again.\n"
            "(If the bot ever ran with a webhook, call deleteWebhook first.)"
        )

    chats, users = {}, {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("type")
        who = msg.get("from")
        if who and not who.get("is_bot"):
            users[who["id"]] = " ".join(
                filter(None, [who.get("first_name"), who.get("last_name")])
            ) or who.get("username", "?")

    print("Chats seen (use the negative group ID, not a personal one):")
    for cid, title in chats.items():
        kind = "GROUP" if cid < 0 else "private"
        print(f"  {cid:<18} {kind:<8} {title}")

    print("\nOperators seen -- paste into config/config.yaml under telegram.operators:")
    print("  operators:")
    for uid, name in users.items():
        print(f'    - {{id: {uid}, name: "{name}"}}')

    if len(users) < 2:
        print(f"\nOnly {len(users)} operator seen. The other one still needs to send a")
        print("slash-message in the group before their ID appears here.")


if __name__ == "__main__":
    main()
