import os
import re
import asyncio
from typing import Optional

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

OWNER_ID = 7504234770

DATA_DIR = "/app/data"
SESSION_FILE = "/app/data/telegram"

WAIT_SECONDS = 55
AFTER_MEDIA_WAIT = 7

PAGE_SIZE = 8

os.makedirs(DATA_DIR, exist_ok=True)


# =========================================================
# TELEGRAM USER SESSION
# =========================================================

tg = TelegramClient(
    SESSION_FILE,
    API_ID,
    API_HASH,
)


# =========================================================
# STATE
# =========================================================

destination = None
destination_name = None

processing = False
stop_requested = False

destinations_cache = []


# =========================================================
# SECURITY
# =========================================================

def owner(update: Update):
    return (
        update.effective_user
        and update.effective_user.id == OWNER_ID
    )


# =========================================================
# MAIN MENU
# =========================================================

def main_menu():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📍 انتخاب مقصد",
                callback_data="menu_dest",
            ),
            InlineKeyboardButton(
                "📊 وضعیت",
                callback_data="menu_status",
            ),
        ],
        [
            InlineKeyboardButton(
                "📥 پردازش لینک‌ها",
                callback_data="menu_links",
            ),
            InlineKeyboardButton(
                "🔄 تغییر مقصد",
                callback_data="menu_dest",
            ),
        ],
        [
            InlineKeyboardButton(
                "❓ راهنما",
                callback_data="menu_help",
            ),
        ],
    ])


# =========================================================
# DESTINATION KEYBOARD
# =========================================================

def destination_keyboard(page=0):

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE

    items = destinations_cache[start:end]

    buttons = []

    for i, dialog in enumerate(items, start=start):

        title = dialog.name or "بدون نام"

        if len(title) > 30:
            title = title[:30] + "..."

        buttons.append([
            InlineKeyboardButton(
                f"📁 {title}",
                callback_data=f"choose:{i}",
            )
        ])

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "◀️ قبلی",
                callback_data=f"destpage:{page - 1}",
            )
        )

    if end < len(destinations_cache):
        navigation.append(
            InlineKeyboardButton(
                "بعدی ▶️",
                callback_data=f"destpage:{page + 1}",
            )
        )

    if navigation:
        buttons.append(navigation)

    buttons.append([
        InlineKeyboardButton(
            "🔙 منوی اصلی",
            callback_data="home",
        )
    ])

    total_pages = max(
        1,
        (len(destinations_cache) + PAGE_SIZE - 1)
        // PAGE_SIZE,
    )

    return InlineKeyboardMarkup(buttons), total_pages


# =========================================================
# DESTINATION LIST
# =========================================================

async def load_destinations():

    global destinations_cache

    destinations_cache = []

    async for dialog in tg.iter_dialogs():

        entity = dialog.entity

        # Groups
        if getattr(entity, "megagroup", False):
            destinations_cache.append(dialog)
            continue

        # Channels
        if getattr(entity, "broadcast", False):
            destinations_cache.append(dialog)
            continue

        # Basic groups
        if getattr(entity, "title", None):
            entity_id = getattr(
                entity,
                "id",
                0,
            )

            if entity_id < 0:
                destinations_cache.append(dialog)


# =========================================================
# SHOW DESTINATIONS
# =========================================================

async def show_destinations(
    update: Update,
    page=0,
):

    if not destinations_cache:
        await load_destinations()

    if not destinations_cache:

        text = (
            "❌ هیچ گروه یا کانالی پیدا نشد."
        )

        if update.callback_query:
            await update.callback_query.edit_message_text(
                text,
                reply_markup=main_menu(),
            )
        else:
            await update.message.reply_text(
                text,
                reply_markup=main_menu(),
            )

        return

    keyboard, total_pages = destination_keyboard(page)

    text = (
        "📍 <b>انتخاب مقصد</b>\n\n"
        f"صفحه {page + 1} از {total_pages}\n"
        f"تعداد مقصدها: {len(destinations_cache)}\n\n"
        "گروه یا کانال موردنظر را انتخاب کن:"
    )

    if update.callback_query:

        await update.callback_query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    else:

        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


# =========================================================
# STATUS
# =========================================================

async def show_status(
    update: Update,
):

    if destination:

        status = (
            "🟢 در حال پردازش"
            if processing
            else "🟢 آماده"
        )

        text = (
            "📊 <b>وضعیت سیستم</b>\n\n"
            f"⚙️ وضعیت: {status}\n"
            f"📍 مقصد: {destination_name}\n"
            f"🛑 توقف درخواست‌شده: "
            f"{'بله' if stop_requested else 'خیر'}"
        )

    else:

        text = (
            "📊 <b>وضعیت سیستم</b>\n\n"
            "🟡 مقصدی انتخاب نشده.\n\n"
            "ابتدا یک مقصد انتخاب کن."
        )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📍 انتخاب مقصد",
                callback_data="menu_dest",
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 منوی اصلی",
                callback_data="home",
            )
        ],
    ])

    if update.callback_query:

        await update.callback_query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    else:

        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


# =========================================================
# HELP
# =========================================================

async def show_help(update: Update):

    text = (
        "❓ <b>راهنما</b>\n\n"

        "📍 <b>انتخاب مقصد</b>\n"
        "گروه یا کانالی را که فایل‌ها باید به آن "
        "ارسال شوند انتخاب می‌کند.\n\n"

        "📥 <b>پردازش لینک‌ها</b>\n"
        "چند لینک Telegram را یکجا بفرست.\n"
        "لینک‌ها یکی‌یکی پردازش می‌شوند.\n\n"

        "📦 فقط پیام‌های دارای Media منتقل می‌شوند؛ "
        "متن‌های تبلیغاتی به‌عنوان فایل ارسال نمی‌شوند.\n\n"

        "🛑 <b>توقف</b>\n"
        "پردازش صف فعلی را متوقف می‌کند.\n\n"

        "⚠️ ربات‌هایی که برای دریافت فایل نیاز به "
        "مراحل یا دکمه‌های اختصاصی دارند ممکن است "
        "به پردازش اختصاصی نیاز داشته باشند."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔙 منوی اصلی",
                callback_data="home",
            )
        ]
    ])

    await update.callback_query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# =========================================================
# HOME
# =========================================================

async def show_home(update: Update):

    me = await tg.get_me()

    text = (
        "🎛 <b>پنل مدیریت</b>\n\n"
        f"👤 اکانت: {me.first_name or 'بدون نام'}\n"
        f"🆔 ID: {me.id}\n\n"
    )

    if destination:
        text += (
            f"📍 مقصد فعلی: "
            f"<b>{destination_name}</b>\n"
        )
    else:
        text += "📍 مقصد: <b>انتخاب نشده</b>\n"

    text += (
        "\nیکی از گزینه‌های زیر را انتخاب کن:"
    )

    if update.callback_query:

        await update.callback_query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=main_menu(),
        )

    else:

        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=main_menu(),
        )


# =========================================================
# CALLBACKS
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not owner(update):
        return

    query = update.callback_query

    await query.answer()

    data = query.data

    # -----------------------------------------------------
    # HOME
    # -----------------------------------------------------

    if data == "home":
        await show_home(update)
        return

    # -----------------------------------------------------
    # DESTINATION
    # -----------------------------------------------------

    if data == "menu_dest":
        await show_destinations(update, 0)
        return

    if data.startswith("destpage:"):

        page = int(
            data.split(":")[1]
        )

        await show_destinations(
            update,
            page,
        )

        return

    # -----------------------------------------------------
    # CHOOSE DESTINATION
    # -----------------------------------------------------

    if data.startswith("choose:"):

        global destination
        global destination_name

        index = int(
            data.split(":")[1]
        )

        if index >= len(destinations_cache):

            await query.edit_message_text(
                "❌ مقصد دیگر وجود ندارد.",
                reply_markup=main_menu(),
            )

            return

        dialog = destinations_cache[index]

        destination = dialog.entity
        destination_name = (
            dialog.name or "بدون نام"
        )

        await query.edit_message_text(
            "✅ <b>مقصد انتخاب شد</b>\n\n"
            f"📍 {destination_name}\n\n"
            "حالا می‌توانی لینک‌ها را بفرستی.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📥 پردازش لینک‌ها",
                        callback_data="menu_links",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔄 تغییر مقصد",
                        callback_data="menu_dest",
                    )
                    ,
                    InlineKeyboardButton(
                        "🔙 منوی اصلی",
                        callback_data="home",
                    ),
                ],
            ]),
        )

        return

    # -----------------------------------------------------
    # STATUS
    # -----------------------------------------------------

    if data == "menu_status":

        await show_status(update)

        return

    # -----------------------------------------------------
    # LINKS
    # -----------------------------------------------------

    if data == "menu_links":

        if not destination:

            await query.edit_message_text(
                "❌ ابتدا مقصد را انتخاب کن.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "📍 انتخاب مقصد",
                            callback_data="menu_dest",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🔙 بازگشت",
                            callback_data="home",
                        )
                    ],
                ]),
            )

            return

        await query.edit_message_text(
            "📥 <b>آماده دریافت لینک‌ها</b>\n\n"
            f"📍 مقصد: {destination_name}\n\n"
            "حالا لینک‌ها را بفرست.\n"
            "می‌توانی چند لینک را یکجا بفرستی.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🛑 توقف",
                        callback_data="stop",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 منوی اصلی",
                        callback_data="home",
                    )
                ],
            ]),
        )

        return

    # -----------------------------------------------------
    # STOP
    # -----------------------------------------------------

    if data == "stop":

        global stop_requested

        stop_requested = True

        await query.edit_message_text(
            "🛑 <b>درخواست توقف ثبت شد.</b>\n\n"
            "پس از پایان لینک فعلی، صف متوقف می‌شود.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )

        return


# =========================================================
# PARSE LINKS
# =========================================================

def extract_links(text):

    pattern = (
        r'https?://(?:www\.)?'
        r'(?:t\.me|telegram\.me)/'
        r'[^\s]+'
    )

    found = re.findall(
        pattern,
        text,
        re.IGNORECASE,
    )

    result = []

    for link in found:

        link = link.rstrip(
            ".,!?،؛)]}>"
        )

        if link not in result:
            result.append(link)

    return result


# =========================================================
# PARSE TELEGRAM BOT LINK
# =========================================================

def parse_link(link):

    pattern = re.compile(
        r'(?:https?://)?'
        r'(?:www\.)?'
        r'(?:t\.me|telegram\.me)/'
        r'([A-Za-z0-9_]+)'
        r'(?:\?start=([^&\s]+))?',
        re.IGNORECASE,
    )

    match = pattern.search(link)

    if not match:
        return None

    username = match.group(1)
    payload = match.group(2)

    return username, payload


# =========================================================
# MEDIA CHECK
# =========================================================

def has_media(message):

    return bool(
        message
        and message.media
    )


# =========================================================
# SEND MEDIA WITHOUT TEXT
# =========================================================

async def send_media(
    message,
):

    try:

        await tg.send_file(
            destination,
            message.media,
            caption=None,
        )

        return True

    except FloodWaitError as e:

        print(
            f"FloodWait: {e.seconds}s"
        )

        await asyncio.sleep(
            e.seconds
        )

        try:

            await tg.send_file(
                destination,
                message.media,
                caption=None,
            )

            return True

        except Exception as e2:

            print(
                "SEND RETRY ERROR:",
                repr(e2),
            )

            return False

    except Exception as e:

        print(
            "SEND ERROR:",
            repr(e),
        )

        return False


# =========================================================
# PROCESS ONE LINK
# =========================================================

async def process_one_link(
    link,
):

    parsed = parse_link(link)

    if not parsed:

        return {
            "success": False,
            "count": 0,
            "reason": "لینک معتبر نیست.",
        }

    username, payload = parsed

    try:

        bot = await tg.get_entity(
            username
        )

    except Exception as e:

        print(
            "ENTITY ERROR:",
            repr(e),
        )

        return {
            "success": False,
            "count": 0,
            "reason": (
                f"ربات @{username} پیدا نشد."
            ),
        }

    # -----------------------------------------------------
    # START
    # -----------------------------------------------------

    try:

        if payload:
            command = f"/start {payload}"
        else:
            command = "/start"

        request_message = await tg.send_message(
            bot,
            command,
        )

    except Exception as e:

        print(
            "START ERROR:",
            repr(e),
        )

        return {
            "success": False,
            "count": 0,
            "reason": (
                f"ارسال درخواست به @{username} "
                "ناموفق بود."
            ),
        }

    # -----------------------------------------------------
    # WAIT FOR MEDIA
    # -----------------------------------------------------

    start_time = asyncio.get_running_loop().time()

    last_message_id = request_message.id

    media_count = 0

    last_media_at = None

    while True:

        if stop_requested:
            break

        now = asyncio.get_running_loop().time()

        if now - start_time >= WAIT_SECONDS:
            break

        try:

            messages = await tg.get_messages(
                bot,
                limit=50,
                min_id=last_message_id,
            )

        except Exception as e:

            print(
                "READ ERROR:",
                repr(e),
            )

            await asyncio.sleep(1)

            continue

        messages = list(
            reversed(messages)
        )

        for message in messages:

            if message.id <= last_message_id:
                continue

            last_message_id = max(
                last_message_id,
                message.id,
            )

            # فقط Media
            if not has_media(message):
                continue

            success = await send_media(
                message
            )

            if success:

                media_count += 1

                last_media_at = (
                    asyncio.get_running_loop().time()
                )

                print(
                    f"MEDIA SENT: "
                    f"@{username} "
                    f"{message.id}"
                )

        # اگر فایل پیدا شده و چند ثانیه فایل جدید نیامد
        if (
            media_count > 0
            and last_media_at is not None
            and (
                asyncio.get_running_loop().time()
                - last_media_at
            ) >= AFTER_MEDIA_WAIT
        ):
            break

        await asyncio.sleep(1)

    if media_count:

        return {
            "success": True,
            "count": media_count,
            "reason": None,
        }

    return {
        "success": False,
        "count": 0,
        "reason": (
            f"در {WAIT_SECONDS} ثانیه "
            f"رسانه‌ای از @{username} دریافت نشد."
        ),
    }


# =========================================================
# PROCESS ALL
# =========================================================

async def process_all(
    update: Update,
    links,
):

    global processing
    global stop_requested

    processing = True
    stop_requested = False

    total = len(links)

    success = 0
    failed = 0
    total_media = 0

    try:

        status_message = await update.message.reply_text(
            "🚀 <b>پردازش شروع شد</b>\n\n"
            f"🔗 تعداد لینک‌ها: {total}\n"
            f"📍 مقصد: {destination_name}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🛑 توقف",
                        callback_data="stop",
                    )
                ]
            ]),
        )

        for number, link in enumerate(
            links,
            start=1,
        ):

            if stop_requested:
                break

            try:

                await status_message.edit_text(
                    "⏳ <b>در حال پردازش</b>\n\n"
                    f"🔗 لینک: {number}/{total}\n"
                    f"📦 رسانه ارسال‌شده: {total_media}\n\n"
                    f"<code>{link}</code>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "🛑 توقف",
                                callback_data="stop",
                            )
                        ]
                    ]),
                )

            except Exception:
                pass

            result = await process_one_link(
                link
            )

            if result["success"]:

                success += 1

                total_media += result["count"]

            else:

                failed += 1

            await asyncio.sleep(1)

        # -------------------------------------------------
        # FINAL
        # -------------------------------------------------

        if stop_requested:

            title = "🛑 پردازش متوقف شد"

        else:

            title = "🏁 پردازش کامل شد"

        await status_message.edit_text(
            f"<b>{title}</b>\n\n"
            f"🔗 کل لینک‌ها: {total}\n"
            f"✅ موفق: {success}\n"
            f"❌ ناموفق: {failed}\n"
            f"📦 کل رسانه ارسال‌شده: {total_media}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📥 دوباره پردازش",
                        callback_data="menu_links",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔄 تغییر مقصد",
                        callback_data="menu_dest",
                    ),
                    InlineKeyboardButton(
                        "🏠 منوی اصلی",
                        callback_data="home",
                    ),
                ],
            ]),
        )

    finally:

        processing = False
        stop_requested = False


# =========================================================
# TEXT HANDLER
# =========================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not owner(update):
        return

    if not destination:

        await update.message.reply_text(
            "❌ ابتدا مقصد را انتخاب کن.",
            reply_markup=main_menu(),
        )

        return

    if processing:

        await update.message.reply_text(
            "⏳ یک صف در حال پردازش است.\n"
            "لطفاً تا پایان آن صبر کن."
        )

        return

    links = extract_links(
        update.message.text or ""
    )

    if not links:

        await update.message.reply_text(
            "❌ در پیام هیچ لینک Telegram پیدا نشد."
        )

        return

    await process_all(
        update,
        links,
    )


# =========================================================
# START
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not owner(update):

        await update.message.reply_text(
            "⛔ دسترسی ندارید."
        )

        return

    if not await tg.is_user_authorized():

        await update.message.reply_text(
            "❌ Session معتبر نیست."
        )

        return

    await show_home(update)


# =========================================================
# MAIN
# =========================================================

async def main():

    print(
        "================================"
    )

    print(
        "Connecting Telegram Session..."
    )

    await tg.connect()

    if not await tg.is_user_authorized():

        print(
            "❌ Telegram Session is not authorized."
        )

        return

    me = await tg.get_me()

    print(
        f"✅ Telegram connected: "
        f"{me.first_name} ({me.id})"
    )

    print(
        "================================"
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
            start_command,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    await app.initialize()

    await app.start()

    await app.updater.start_polling()

    print(
        "🤖 Management bot started."
    )

    await asyncio.Event().wait()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        print("Stopped.")

    except Exception as e:

        print(
            "FATAL ERROR:",
            repr(e),
        )
