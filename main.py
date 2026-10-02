import os
import asyncio

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

# Session ذخیره‌شده، اگر وجود داشته باشد
SESSION = os.environ.get("SESSION", "")

client = TelegramClient(
    StringSession(SESSION),
    API_ID,
    API_HASH,
)

login_state = {}
phone_code_hash = {}


def is_owner(update: Update):
    return (
        update.effective_user
        and update.effective_user.id == OWNER_ID
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    login_state.clear()

    await update.message.reply_text(
        "سلام 👋\n\n"
        "برای اتصال اکانت تلگرام:\n"
        "شماره تلفن را با فرمت بین‌المللی بفرست.\n\n"
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
        # دریافت شماره
        # -------------------------
        if "step" not in login_state:

            phone = text

            if not phone.startswith("+"):
                await update.message.reply_text(
                    "❌ شماره باید با + و کد کشور شروع شود.\n"
                    "مثال: +1234567890"
                )
                return

            await client.connect()

            sent = await client.send_code_request(phone)

            login_state["step"] = "code"
            login_state["phone"] = phone

            phone_code_hash["value"] = sent.phone_code_hash

            await update.message.reply_text(
                "📩 کد ورود ارسال شد.\n\n"
                "کد جدیدی که تلگرام همین الان فرستاده را "
                "وارد کن."
            )

            return

        # -------------------------
        # دریافت کد
        # -------------------------
        if login_state["step"] == "code":

            code = text.replace(" ", "").replace("-", "")

            phone = login_state["phone"]
            code_hash = phone_code_hash["value"]

            try:

                await client.sign_in(
                    phone=phone,
                    code=code,
                    phone_code_hash=code_hash,
                )

                await successful_login(update)

            except Exception as e:

                error = str(e).lower()

                if "password" in error or "two-step" in error:

                    login_state["step"] = "password"

                    await update.message.reply_text(
                        "🔐 اکانتت رمز دومرحله‌ای دارد.\n\n"
                        "رمز 2FA را وارد کن."
                    )

                elif "phone_code_expired" in error:

                    login_state.clear()
                    phone_code_hash.clear()

                    await update.message.reply_text(
                        "⌛ کد منقضی شده.\n\n"
                        "دوباره /start بزن و یک کد جدید بگیر."
                    )

                elif "phone_code_invalid" in error:

                    await update.message.reply_text(
                        "❌ این کد اشتباه است.\n\n"
                        "کد جدیدی که تلگرام همین الان "
                        "فرستاده را وارد کن."
                    )

                else:

                    print("LOGIN ERROR:", repr(e))

                    await update.message.reply_text(
                        "❌ ورود انجام نشد.\n"
                        "دوباره /start بزن."
                    )

            return

        # -------------------------
        # دریافت رمز 2FA
        # -------------------------
        if login_state["step"] == "password":

            password = text

            try:

                await client.sign_in(
                    password=password
                )

                await successful_login(update)

            except Exception as e:

                print("2FA ERROR:", repr(e))

                await update.message.reply_text(
                    "❌ رمز دومرحله‌ای صحیح نیست."
                )

            return

    except Exception as e:

        print("GENERAL ERROR:", repr(e))

        await update.message.reply_text(
            "❌ یک خطا رخ داد.\n"
            "Logs مربوط به Railway را بررسی کن."
        )


async def successful_login(update):

    # ساخت Session
    session_string = client.session.save()

    # نمایش ندادن Session
    print("TELEGRAM_ACCOUNT_CONNECTED")
    print("SESSION_CREATED")

    login_state.clear()
    phone_code_hash.clear()

    await update.message.reply_text(
        "✅ اکانت تلگرام با موفقیت متصل شد.\n\n"
        "حالا مرحله بعدی:\n"
        "📥 دریافت لینک‌ها\n"
        "🎯 انتخاب گروه/کانال\n"
        "📤 ارسال خودکار فایل‌ها"
    )

    # مهم:
    # Session باید در Railway به صورت Variable ذخیره شود.
    # این برنامه آن را در Logs چاپ نمی‌کند.
    #
    # فعلاً Session را خودکار در Variable نمی‌نویسیم،
    # چون Railway Variables از داخل برنامه قابل تغییر نیستند.


async def main():

    await client.connect()

    if await client.is_user_authorized():

        print("TELEGRAM_SESSION_ALREADY_AUTHORIZED")

    else:

        print("TELEGRAM_ACCOUNT_NOT_AUTHORIZED")

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
