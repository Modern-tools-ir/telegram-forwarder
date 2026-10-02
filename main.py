import os
import asyncio
from telethon import TelegramClient
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# SETTINGS
# =========================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

OWNER_ID = 7504234770

DATA_DIR = "/app/data"
SESSION_FILE = "/app/data/telegram"

os.makedirs(DATA_DIR, exist_ok=True)

# =========================
# TELEGRAM USER SESSION
# =========================

client = TelegramClient(
    SESSION_FILE,
    API_ID,
    API_HASH,
)

# =========================
# OWNER CHECK
# =========================

def is_owner(update: Update):
    return (
        update.effective_user
        and update.effective_user.id == OWNER_ID
    )

# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_owner(update):
        await update.message.reply_text(
            "⛔ دسترسی ندارید."
        )
        return

    if not await client.is_user_authorized():
        await update.message.reply_text(
            "❌ Session اکانت تلگرام پیدا نشد یا معتبر نیست."
        )
        return

    me = await client.get_me()

    name = me.first_name or "بدون نام"

    await update.message.reply_text(
        f"✅ اکانت متصل است.\n\n"
        f"👤 نام: {name}\n"
        f"🆔 ID: {me.id}\n\n"
        f"📥 حالا لینک‌های Telegram را بفرست."
    )

# =========================
# HANDLE LINKS
# =========================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_owner(update):
        return

    text = (update.message.text or "").strip()

    if not text:
        return

    if not await client.is_user_authorized():
        await update.message.reply_text(
            "❌ Session معتبر نیست."
        )
        return

    # تشخیص لینک تلگرام
    if (
        "https://t.me/" in text
        or "http://t.me/" in text
        or "t.me/" in text
    ):

        await update.message.reply_text(
            "🔗 لینک دریافت شد.\n\n"
            "⏳ در حال بررسی لینک..."
        )

        # فعلاً فقط تست Session
        try:

            me = await client.get_me()

            await update.message.reply_text(
                "✅ Session فعال است.\n"
                f"👤 {me.first_name}\n\n"
                "📌 پردازش فایل را در مرحله بعد اضافه می‌کنیم."
            )

        except Exception as e:

            print("TELEGRAM ERROR:", repr(e))

            await update.message.reply_text(
                "❌ خطا هنگام دسترسی به اکانت تلگرام."
            )

        return

    await update.message.reply_text(
        "ℹ️ لطفاً یک لینک Telegram بفرست."
    )

# =========================
# MAIN
# =========================

async def main():

    print("Connecting Telegram session...")

    await client.connect()

    if await client.is_user_authorized():

        me = await client.get_me()

        print(
            f"✅ Telegram connected: "
            f"{me.first_name} ({me.id})"
        )

    else:

        print(
            "❌ Telegram session is not authorized."
        )

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    print("🤖 Bot started.")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
