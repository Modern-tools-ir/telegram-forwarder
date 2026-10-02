import os
import re
import asyncio

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

OWNER_ID = 7504234770

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

SESSION = os.environ.get("SESSION", "")

user_client = TelegramClient(
    StringSession(SESSION),
    API_ID,
    API_HASH,
)

app = Application.builder().token(BOT_TOKEN).build()

processing = False
destination = None


def extract_links(text):
    pattern = r"https?://t\.me/[A-Za-z0-9_]+(?:\?start=[^\s]+)?"
    return re.findall(pattern, text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    await update.message.reply_text(
        "سلام 👋\n\n"
        "لینک‌های تلگرام را بفرست.\n"
        "هر تعداد لینک که خواستی می‌توانی در یک پیام قرار بدهی."
    )


async def receive_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global destination

    if update.effective_user.id != OWNER_ID:
        return

    text = update.message.text or ""
    links = extract_links(text)

    if not links:
        await update.message.reply_text(
            "لینک معتبر t.me پیدا نشد."
        )
        return

    await update.message.reply_text(
        f"✅ {len(links)} لینک دریافت شد.\n\n"
        "فعلاً مقصد را باید در تنظیمات برنامه مشخص کنیم."
    )


async def main():
    await user_client.start()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_links,
        )
    )

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    print("Bot is running...")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
