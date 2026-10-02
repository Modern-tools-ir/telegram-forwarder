import os
from telethon import TelegramClient

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

SESSION_PATH = "/app/data/telegram"

client = TelegramClient(
    SESSION_PATH,
    API_ID,
    API_HASH
)

async def main():
    print("=== Telegram Session Generator ===")
    print("Session will be saved to:")
    print("/app/data/telegram.session")
    print()

    await client.start()

    me = await client.get_me()

    print()
    print("✅ LOGIN SUCCESSFUL")
    print("Account:", me.first_name)
    print("User ID:", me.id)
    print()
    print("Session saved successfully.")

with client:
    client.loop.run_until_complete(main())
