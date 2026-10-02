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

client = TelegramClient(
    StringSession(),
    API_ID,
    API_HASH,
)

login_step = None
login_phone = None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    await update.message.reply_text(
        "سلام 👋\n\n"
        "برای اتصال اکانت تلگرام، شماره تلفنت را با فرمت زیر بفرست:\n\n"
        "+989xxxxxxxxx"
    )


async def messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global login_step, login_phone

    if update.effective_user.id != OWNER_ID:
        return

    text = update.message.text.strip()

    try:
        if login_step is None:
            login_phone = text

            await client.connect()

            result = await client.send_code_request(login_phone)

            login_step = "code"

            await update.message.reply_text(
                "📩 کد ورود تلگرام برایت ارسال شد.\n\n"
                "کد را همینجا بفرست."
            )

        elif login_step == "code":
            code = text.replace(" ", "")

            try:
                await client.sign_in(
                    phone=login_phone,
                    code=code,
                )

                login_step = None

                session = client.session.save()

                await update.message.reply_text(
                    "✅ اکانت با موفقیت متصل شد.\n\n"
                    "حالا آماده ساختن بخش دریافت لینک‌ها هستیم.\n\n"
                    "Session ساخته شد؛ آن را داخل پیام یا لاگ نمایش نمی‌دهم."
                )

                print("TELEGRAM_ACCOUNT_CONNECTED")
                print("SESSION_CREATED")

            except Exception as e:
                if "password" in str(e).lower():
                    login_step = "password"

                    await update.message.reply_text(
                        "🔐 اکانتت رمز دومرحله‌ای دارد.\n"
                        "رمز 2FA را بفرست."
                    )
                else:
                    await update.message.reply_text(
                        "❌ کد واردشده قبول نشد. دوباره کد را بفرست."
                    )

        elif login_step == "password":
            password = text

            try:
                await client.sign_in(password=password)

                login_step = None

                await update.message.reply_text(
                    "✅ اکانت با موفقیت متصل شد."
                )

                print("TELEGRAM_ACCOUNT_CONNECTED")

            except Exception:
                await update.message.reply_text(
                    "❌ رمز دومرحله‌ای اشتباه است."
                )

    except Exception as e:
        print("ERROR:", repr(e))

        await update.message.reply_text(
            "❌ در اتصال مشکلی پیش آمد. Logs را بررسی می‌کنیم."
        )


async def main():
    await client.connect()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            messages,
        )
    )

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    print("MANAGEMENT_BOT_RUNNING")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
