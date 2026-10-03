import os
import re
import asyncio

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
destination_type = None

processing = False
stop_requested = False

destination_cache = {
    "groups": [],
    "channels": [],
    "saved": [],
}


# =========================================================
# SECURITY
# =========================================================

def is_owner(update: Update):

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
                callback_data="dest_menu",
            ),
            InlineKeyboardButton(
                "📊 وضعیت",
                callback_data="status",
            ),
        ],
        [
            InlineKeyboardButton(
                "📥 پردازش لینک‌ها",
                callback_data="links",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔄 تغییر مقصد",
                callback_data="dest_menu",
            ),
            InlineKeyboardButton(
                "❓ راهنما",
                callback_data="help",
            ),
        ],
    ])


# =========================================================
# DESTINATION TYPE MENU
# =========================================================

def destination_type_menu():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 گروه‌ها",
                callback_data="type:groups",
            ),
        ],
        [
            InlineKeyboardButton(
                "📢 کانال‌ها",
                callback_data="type:channels",
            ),
        ],
        [
            InlineKeyboardButton(
                "💾 پیام‌های ذخیره‌شده",
                callback_data="type:saved",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔙 منوی اصلی",
                callback_data="home",
            ),
        ],
    ])


# =========================================================
# LOAD DESTINATIONS
# =========================================================

async def load_destinations():

    destination_cache["groups"] = []
    destination_cache["channels"] = []
    destination_cache["saved"] = []

    async for dialog in tg.iter_dialogs():

        entity = dialog.entity

        # -----------------------------------------------
        # Saved Messages
        # -----------------------------------------------

        if getattr(entity, "is_self", False):

            destination_cache["saved"].append(dialog)

            continue

        # -----------------------------------------------
        # Channel
        # -----------------------------------------------

        if getattr(entity, "broadcast", False):

            destination_cache["channels"].append(dialog)

            continue

        # -----------------------------------------------
        # Supergroup
        # -----------------------------------------------

        if getattr(entity, "megagroup", False):

            destination_cache["groups"].append(dialog)

            continue

        # -----------------------------------------------
        # Normal Group
        # -----------------------------------------------

        if entity.__class__.__name__ == "Chat":

            destination_cache["groups"].append(dialog)

            continue

        # -----------------------------------------------
        # Everything else ignored:
        # Private chats
        # Bots
        # Users
        # -----------------------------------------------


# =========================================================
# DESTINATION LIST KEYBOARD
# =========================================================

def destination_list_keyboard(
    destination_type,
    page,
):

    items = destination_cache[
        destination_type
    ]

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE

    current = items[start:end]

    buttons = []

    for index, dialog in enumerate(
        current,
        start=start,
    ):

        title = dialog.name or "بدون نام"

        if len(title) > 32:
            title = title[:32] + "..."

        buttons.append([
            InlineKeyboardButton(
                f"📁 {title}",
                callback_data=(
                    f"select:"
                    f"{destination_type}:"
                    f"{index}"
                ),
            )
        ])

    total_pages = max(
        1,
        (
            len(items)
            + PAGE_SIZE
            - 1
        )
        // PAGE_SIZE,
    )

    navigation = []

    if page > 0:

        navigation.append(
            InlineKeyboardButton(
                "◀️ قبلی",
                callback_data=(
                    f"page:"
                    f"{destination_type}:"
                    f"{page - 1}"
                ),
            )
        )

    if end < len(items):

        navigation.append(
            InlineKeyboardButton(
                "بعدی ▶️",
                callback_data=(
                    f"page:"
                    f"{destination_type}:"
                    f"{page + 1}"
                ),
            )
        )

    if navigation:

        buttons.append(navigation)

    buttons.append([
        InlineKeyboardButton(
            "🔙 انواع مقصد",
            callback_data="dest_menu",
        )
    ])

    return (
        InlineKeyboardMarkup(buttons),
        total_pages,
    )


# =========================================================
# SHOW DESTINATION TYPES
# =========================================================

async def show_destination_types(
    update: Update,
):

    if not destination_cache["groups"] and \
       not destination_cache["channels"] and \
       not destination_cache["saved"]:

        await load_destinations()

    groups = len(
        destination_cache["groups"]
    )

    channels = len(
        destination_cache["channels"]
    )

    saved = len(
        destination_cache["saved"]
    )

    text = (
        "📍 <b>انتخاب مقصد</b>\n\n"
        f"👥 گروه‌ها: {groups}\n"
        f"📢 کانال‌ها: {channels}\n"
        f"💾 پیام‌های ذخیره‌شده: {saved}\n\n"
        "نوع مقصد را انتخاب کن:"
    )

    keyboard = destination_type_menu()

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
# SHOW DESTINATION LIST
# =========================================================

async def show_destination_list(
    update: Update,
    destination_type,
    page=0,
):

    items = destination_cache[
        destination_type
    ]

    names = {
        "groups": "👥 گروه‌ها",
        "channels": "📢 کانال‌ها",
        "saved": "💾 پیام‌های ذخیره‌شده",
    }

    title = names.get(
        destination_type,
        "مقصدها",
    )

    if not items:

        text = (
            f"{title}\n\n"
            "❌ موردی پیدا نشد."
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔙 بازگشت",
                    callback_data="dest_menu",
                )
            ]
        ])

    else:

        keyboard, total_pages = (
            destination_list_keyboard(
                destination_type,
                page,
            )
        )

        text = (
            f"{title}\n\n"
            f"صفحه {page + 1} از "
            f"{total_pages}\n"
            f"تعداد: {len(items)}\n\n"
            "مقصد موردنظر را انتخاب کن:"
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

        state = (
            "⏳ در حال پردازش"
            if processing
            else "🟢 آماده"
        )

        text = (
            "📊 <b>وضعیت</b>\n\n"
            f"⚙️ وضعیت: {state}\n"
            f"📍 مقصد: "
            f"<b>{destination_name}</b>\n"
            f"📂 نوع: "
            f"<b>{destination_type}</b>\n"
            f"🛑 توقف: "
            f"{'فعال' if stop_requested else 'خیر'}"
        )

    else:

        text = (
            "📊 <b>وضعیت</b>\n\n"
            "🟡 هنوز مقصد انتخاب نشده."
        )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📍 انتخاب مقصد",
                callback_data="dest_menu",
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 منوی اصلی",
                callback_data="home",
            )
        ],
    ])

    await update.callback_query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# =========================================================
# HELP
# =========================================================

async def show_help(
    update: Update,
):

    text = (
        "❓ <b>راهنما</b>\n\n"

        "📍 <b>انتخاب مقصد</b>\n"
        "ابتدا گروه، کانال یا Saved Messages "
        "را انتخاب کن.\n\n"

        "📥 <b>پردازش لینک‌ها</b>\n"
        "می‌توانی چند لینک Telegram را "
        "یکجا ارسال کنی.\n\n"

        "🔄 لینک‌ها یکی‌یکی پردازش می‌شوند.\n\n"

        "📦 فقط پیام‌های دارای Media منتقل "
        "می‌شوند.\n\n"

        "📝 متن تبلیغاتی همراه فایل منتقل "
        "نمی‌شود.\n\n"

        "🛑 در هر زمان می‌توانی صف را متوقف کنی.\n\n"

        "⚠️ اگر یک ربات برای دریافت فایل نیاز "
        "به دکمه یا مراحل اختصاصی داشته باشد، "
        "ممکن است نیاز به پردازش اختصاصی داشته باشد."
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

async def show_home(
    update: Update,
):

    me = await tg.get_me()

    text = (
        "🎛 <b>پنل مدیریت</b>\n\n"
        f"👤 اکانت: "
        f"{me.first_name or 'بدون نام'}\n"
        f"🆔 ID: {me.id}\n\n"
    )

    if destination:

        text += (
            f"📍 مقصد فعلی: "
            f"<b>{destination_name}</b>\n"
        )

    else:

        text += (
            "📍 مقصد فعلی: "
            "<b>انتخاب نشده</b>\n"
        )

    text += (
        "\nیکی از گزینه‌ها را انتخاب کن:"
    )

    keyboard = main_menu()

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
# CALLBACK HANDLER
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_owner(update):
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
    # DESTINATION MENU
    # -----------------------------------------------------

    if data == "dest_menu":

        await show_destination_types(
            update
        )

        return

    # -----------------------------------------------------
    # DESTINATION TYPE
    # -----------------------------------------------------

    if data.startswith("type:"):

        dtype = data.split(
            ":",
            1,
        )[1]

        if dtype not in destination_cache:

            return

        await show_destination_list(
            update,
            dtype,
            0,
        )

        return

    # -----------------------------------------------------
    # PAGE
    # -----------------------------------------------------

    if data.startswith("page:"):

        parts = data.split(":")

        dtype = parts[1]
        page = int(parts[2])

        await show_destination_list(
            update,
            dtype,
            page,
        )

        return

    # -----------------------------------------------------
    # SELECT DESTINATION
    # -----------------------------------------------------

    if data.startswith("select:"):

        global destination
        global destination_name
        global destination_type

        parts = data.split(":")

        dtype = parts[1]
        index = int(parts[2])

        items = destination_cache.get(
            dtype,
            [],
        )

        if index >= len(items):

            await query.edit_message_text(
                "❌ مقصد پیدا نشد.",
                reply_markup=main_menu(),
            )

            return

        dialog = items[index]

        destination = dialog.entity

        destination_name = (
            dialog.name
            or "بدون نام"
        )

        destination_type = dtype

        await query.edit_message_text(
            "✅ <b>مقصد انتخاب شد</b>\n\n"
            f"📍 {destination_name}\n"
            f"📂 {dtype}\n\n"
            "حالا می‌توانی لینک‌ها را بفرستی.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📥 پردازش لینک‌ها",
                        callback_data="links",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔄 تغییر مقصد",
                        callback_data="dest_menu",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 منوی اصلی",
                        callback_data="home",
                    )
                ],
            ]),
        )

        return

    # -----------------------------------------------------
    # STATUS
    # -----------------------------------------------------

    if data == "status":

        await show_status(update)

        return

    # -----------------------------------------------------
    # LINKS
    # -----------------------------------------------------

    if data == "links":

        if not destination:

            await query.edit_message_text(
                "❌ ابتدا مقصد را انتخاب کن.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "📍 انتخاب مقصد",
                            callback_data="dest_menu",
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
            "می‌توانی چند لینک را در یک پیام "
            "یکجا بفرستی.",
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
                        "🏠 منوی اصلی",
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
            "بعد از پایان لینک فعلی، پردازش متوقف می‌شود.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )

        return

    # -----------------------------------------------------
    # HELP
    # -----------------------------------------------------

    if data == "help":

        await show_help(update)

        return


# =========================================================
# EXTRACT LINKS
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
# PARSE LINK
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

    return (
        match.group(1),
        match.group(2),
    )


# =========================================================
# SEND MEDIA ONLY
# =========================================================

async def send_media(message):

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

        except Exception as retry_error:

            print(
                "RETRY SEND ERROR:",
                repr(retry_error),
            )

            return False

    except Exception as e:

        print(
            "SEND MEDIA ERROR:",
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
            "reason": "لینک Telegram معتبر نیست.",
        }

    username, payload = parsed

    print(
        f"Processing @{username}"
    )

    try:

        bot = await tg.get_entity(
            username
        )

    except Exception as e:

        print(
            "GET ENTITY ERROR:",
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

            command = (
                f"/start {payload}"
            )

        else:

            command = "/start"

        request = await tg.send_message(
            bot,
            command,
        )

        print(
            f"Sent: {command}"
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
                "ارسال درخواست به ربات "
                "ناموفق بود."
            ),
        }

    # -----------------------------------------------------
    # WAIT
    # -----------------------------------------------------

    start_time = (
        asyncio.get_running_loop().time()
    )

    last_id = request.id

    media_count = 0

    last_media_time = None

    while True:

        if stop_requested:
            break

        now = (
            asyncio.get_running_loop().time()
        )

        if (
            now - start_time
            >= WAIT_SECONDS
        ):
            break

        try:

            messages = await tg.get_messages(
                bot,
                limit=50,
                min_id=last_id,
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

            if message.id <= last_id:
                continue

            last_id = max(
                last_id,
                message.id,
            )

            # فقط Media
            if not message.media:
                continue

            sent = await send_media(
                message
            )

            if sent:

                media_count += 1

                last_media_time = (
                    asyncio.get_running_loop().time()
                )

        # بعد از دریافت فایل، برای فایل‌های بعدی
        # کمی صبر می‌کنیم
        if (
            media_count > 0
            and last_media_time
            and (
                asyncio.get_running_loop().time()
                - last_media_time
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
            "رسانه‌ای دریافت نشد."
        ),
    }


# =========================================================
# PROCESS ALL LINKS
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
    media_total = 0

    try:

        status_message = (
            await update.message.reply_text(
                "🚀 <b>پردازش شروع شد</b>\n\n"
                f"🔗 تعداد: {total}\n"
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
                    f"📦 رسانه ارسال‌شده: "
                    f"{media_total}\n\n"
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

                media_total += (
                    result["count"]
                )

            else:

                failed += 1

            await asyncio.sleep(1)

        if stop_requested:

            title = "🛑 پردازش متوقف شد"

        else:

            title = "🏁 پردازش کامل شد"

        await status_message.edit_text(
            f"<b>{title}</b>\n\n"
            f"🔗 کل لینک‌ها: {total}\n"
            f"✅ موفق: {success}\n"
            f"❌ ناموفق: {failed}\n"
            f"📦 رسانه‌ها: {media_total}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📥 پردازش جدید",
                        callback_data="links",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔄 تغییر مقصد",
                        callback_data="dest_menu",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 منوی اصلی",
                        callback_data="home",
                    )
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

    if not is_owner(update):
        return

    if not destination:

        await update.message.reply_text(
            "❌ ابتدا مقصد را انتخاب کن.",
            reply_markup=main_menu(),
        )

        return

    if processing:

        await update.message.reply_text(
            "⏳ پردازش قبلی هنوز تمام نشده."
        )

        return

    links = extract_links(
        update.message.text or ""
    )

    if not links:

        await update.message.reply_text(
            "❌ هیچ لینک Telegram پیدا نشد."
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

    if not is_owner(update):

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
            "❌ Session is not authorized."
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
        "🤖 Bot started."
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
