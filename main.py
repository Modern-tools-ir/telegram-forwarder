import os
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
)

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
    if not os.path.exists(SESSION_FILE):
        return ""

    with open(SESSION_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def save_session():
    session_string = client.session.save()

    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        f.write(session_string)


SESSION = load_session()

client = TelegramClient(
    StringSession(SESSION),
    API_ID,
    API_HASH,
)

state = {
    "step": None,
    "phone": None,
    "phone_code_hash": None,
}


def is_owner(update: Update):
    return (
        update.effective_user is not None
        and update.effective_user.id == OWNER_ID
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_owner(update):
        return

    if await client.is_user_authorized():

        await update.message.reply_text(
            "✅ اکانت تلگرام قبلاً متصل شده.\n\n"
            "حالا لینک‌های t.me را بفرست."
        )
        return

    state["step"] = "phone"
    state["phone"] = None
    state["phone_code_hash"] = None

    await update.message.reply_text(
        "📱 شماره تلگرام را با کد کشور بفرست.\n\n"
        "مثال:\n"
        "+1234567890"
    )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_owner(update):
        return

    text = (update.message.text or "").strip()

    if not text:
        return

    try:

        # -------------------------
        # شماره تلفن
        # -------------------------
        if state["step"] == "phone":

            phone = text.replace(" ", "")

            if not phone.startswith("+"):
                await update.message.reply_text(
                    "❌ شماره باید با + و کد کشور شروع شود."
                )
                return

            await client.connect()

            sent = await client.send_code_request(phone)

            state["phone"] = phone
            state["phone_code_hash"] = sent.phone_code_hash
            state["step"] = "code"

            await update.message.reply_text(
                "📩 کد ورود ارسال شد.\n\n"
                "کد جدیدی که تلگرام الان فرستاده را بفرست."
            )

            return

        # -------------------------
        # کد ورود
        # -------------------------
        if state["step"] == "code":

            code = text.replace(" ", "").replace("-", "")

            try:

                await client.sign_in(
                    phone=state["phone"],
                    code=code,
                    phone_code_hash=state["phone_code_hash"],
                )

                save_session()

                state["step"] = None
                state["phone"] = None
                state["phone_code_hash"] = None

                await update.message.reply_text(
                    "✅ اکانت تلگرام با موفقیت متصل شد.\n\n"
                    "Session روی Volume ذخیره شد.\n\n"
                    "حالا آماده دریافت لینک‌ها هستیم."
                )

            except SessionPasswordNeededError:

                state["step"] = "password"

                await update.message.reply_text(
                    "🔐 روی این اکانت رمز دومرحله‌ای فعال است.\n\n"
                    "رمز 2FA را وارد کن."
                )

            except PhoneCodeInvalidError:

                await update.message.reply_text(
                    "❌ کد اشتباه است.\n\n"
                    "کد جدید همان درخواست را وارد کن."
                )

            except PhoneCodeExpiredError:

                state["step"] = "phone"

                await update.message.reply_text(
                    "⌛ کد منقضی شده.\n\n"
                    "دوباره شماره را بفرست تا کد جدید بگیری."
                )

            except Exception as e:

                print("LOGIN ERROR:", repr(e))

                await update.message.reply_text(
                    "❌ هنگام ورود خطایی رخ داد."
                )

            return

        # -------------------------
        # رمز دومرحله‌ای
        # -------------------------
        if state["step"] == "password":

            password = text

            try:

                await client.sign_in(
                    password=password
                )

                save_session()

                state["step"] = None
                state["phone"] = None
                state["phone_code_hash"] = None

                await update.message.reply_text(
                    "✅ ورود کامل شد.\n\n"
                    "Session ذخیره شد.\n"
                    "اکانت تلگرام آماده است."
                )

            except Exception as e:

                print("2FA ERROR:", repr(e))

                await update.message.reply_text(
                    "❌ رمز دومرحله‌ای اشتباه است."
                )

            return

        # -------------------------
        # اگر وارد شده‌ایم
        # -------------------------
        if await client.is_user_authorized():

            await update.message.reply_text(
                "📥 لینک دریافت شد.\n\n"
                "بخش پردازش لینک‌ها را در مرحله بعد اضافه می‌کنیم."
            )

    except Exception as e:

        print("GENERAL ERROR:", repr(e))

        await update.message.reply_text(
            "❌ خطایی رخ داد. Logs را بررسی می‌کنیم."
        )


async def main():

    await client.connect()

    if await client.is_user_authorized():

        print("TELEGRAM_SESSION_OK")

    else:

        print("TELEGRAM_SESSION_NOT_AUTHORIZED")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    print("MANAGEMENT_BOT_RUNNING")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
