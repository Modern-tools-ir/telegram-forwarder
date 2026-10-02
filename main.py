import os
import asyncio
import re

from telethon import TelegramClient
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

DATA_DIR = "/app/data"
SESSION_FILE = os.path.join(DATA_DIR, "telegram.session")

os.makedirs(DATA_DIR, exist_ok=True)


def load_session():
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()

    return ""


def save_session(session_string):
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        f.write(session_string)


SESSION = load_session()

client = TelegramClient(
    StringSession(SESSION),
    API_ID,
    API_HASH,
)


def owner_only(update):
    return (
        update.effective_user is not None
        and update.effective_user.id == OWNER_ID
    )


def extract_links(text):
    pattern = r"https?://t\.me/[A-Za-z0-9_]+(?:\?start=[^\s]+)?"
    return re.findall(pattern, text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not owner_only(update):
        return

    if await client.is_user_authorized():

        await update.message.reply_text(
            "✅ اکانت تلگرام متصل است.\n\n"
            "حالا می‌توانی لینک‌های t.me را بفرستی."
        )

    else:

        await update.message.reply_text(
            "❌ هنوز اکانت تلگرام متصل نشده است.\n\n"
            "ابتدا Session اکانت را ایجاد و در /app/data/telegram.session "
            "قرار بده."
        )


async def handle_links(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not owner_only(update):
        return

    text = update.message.text or ""

    links = extract_links(text)

    if not links:
        await update.message.reply_text(
            "❌ لینک t.me پیدا نشد."
        )
        return

    await update.message.reply_text(
        f"📥 {len(links)} لینک دریافت شد.\n\n"
        "مرحله دریافت و ارسال فایل‌ها بعد از تست اتصال اضافه می‌شود."
    )


async def main():

    await client.connect()

    if await client.is_user_authorized():

        print("TELEGRAM_SESSION_OK")

        # اگر Session معتبر است، دوباره ذخیره‌اش می‌کنیم.
        save_session(client.session.save())

    else:

        print("TELEGRAM_SESSION_NOT_AUTHORIZED")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_links,
        )
    )

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    print("MANAGEMENT_BOT_RUNNING")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
