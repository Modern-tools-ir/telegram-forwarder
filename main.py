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

# Telegram user ID صاحب ربات
OWNER_ID = 7504234770

# محل Session
DATA_DIR = "/app/data"
SESSION_FILE = "/app/data/telegram"

os.makedirs(DATA_DIR, exist_ok=True)

# زمان انتظار برای دریافت جواب ربات مقصد
WAIT_SECONDS = 55

# بعد از آخرین فایل، کمی صبر
AFTER_MEDIA_WAIT = 7

# تعداد مقصد در هر صفحه
PAGE_SIZE = 8


# =========================================================
# TELEGRAM USER SESSION
# =========================================================

tg = TelegramClient(
    SESSION_FILE,
    API_ID,
    API_HASH,
)


# =========================================================
# GLOBAL STATE
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
# OWNER CHECK
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
                "📥 دریافت لینک‌ها",
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

        # -------------------------------------------------
        # Saved Messages
        # -------------------------------------------------

        if getattr(entity, "is_self", False):

            destination_cache["saved"].append(dialog)

            continue

        # -------------------------------------------------
        # Channel
        # -------------------------------------------------

        if getattr(entity, "broadcast", False):

            destination_cache["channels"].append(dialog)

            continue

        # -------------------------------------------------
        # Supergroup
        # -------------------------------------------------

        if getattr(entity, "megagroup", False):

            destination_cache["groups"].append(dialog)

            continue

        # -------------------------------------------------
        # Normal Telegram Group
        # -------------------------------------------------

        if entity.__class__.__name__ == "Chat":

            destination_cache["groups"].append(dialog)

            continue

        # Users/private chats/bots are ignored.


# =========================================================
# DESTINATION KEYBOARD
# =========================================================

def destination_list_keyboard(
    dtype,
    page,
):

    items = destination_cache.get(
        dtype,
        [],
    )

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE

    current = items[start:end]

    buttons = []

    for index, dialog in enumerate(
        current,
        start=start,
    ):

        title = (
            dialog.name
            or "بدون نام"
        )

        if len(title) > 32:
            title = title[:32] + "..."

        buttons.append([
            InlineKeyboardButton(
                f"📁 {title}",
                callback_data=(
                    f"select:{dtype}:{index}"
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
                    f"page:{dtype}:{page - 1}"
                ),
            )
        )

    if end < len(items):

        navigation.append(
            InlineKeyboardButton(
                "بعدی ▶️",
                callback_data=(
                    f"page:{dtype}:{page + 1}"
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
# SHOW DESTINATION MENU
# =========================================================

async def show_destination_types(
    update: Update,
):

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

    await update.callback_query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# =========================================================
# SHOW DESTINATION LIST
# =========================================================

async def show_destination_list(
    update: Update,
    dtype,
    page=0,
):

    items = destination_cache.get(
        dtype,
        [],
    )

    names = {
        "groups": "👥 گروه‌ها",
        "channels": "📢 کانال‌ها",
        "saved": "💾 پیام‌های ذخیره‌شده",
    }

    title = names.get(
        dtype,
        "مقصدها",
    )

    if not items:

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔙 بازگشت",
                    callback_data="dest_menu",
                )
            ]
        ])

        await update.callback_query.edit_message_text(
            f"{title}\n\n❌ موردی پیدا نشد.",
            reply_markup=keyboard,
        )

        return

    keyboard, total_pages = (
        destination_list_keyboard(
            dtype,
            page,
        )
    )

    text = (
        f"{title}\n\n"
        f"📄 صفحه {page + 1} از {total_pages}\n"
        f"📊 تعداد: {len(items)}\n\n"
        "مقصد را انتخاب کن:"
    )

    await update.callback_query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# =========================================================
# STATUS
# =========================================================

async def show_status(update: Update):

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
        )

    else:

        text = (
            "📊 <b>وضعیت</b>\n\n"
            "🟡 مقصدی انتخاب نشده."
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
                "🏠 منوی اصلی",
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

async def show_help(update: Update):

    text = (
        "❓ <b>راهنما</b>\n\n"

        "1️⃣ ابتدا مقصد را انتخاب کن.\n\n"

        "2️⃣ می‌توانی چند لینک Telegram را "
        "در یک پیام بفرستی.\n\n"

        "3️⃣ سیستم لینک‌ها را یکی‌یکی پردازش می‌کند.\n\n"

        "4️⃣ فقط پیام‌هایی که Media دارند "
        "به مقصد ارسال می‌شوند.\n\n"

        "5️⃣ متن تبلیغاتی همراه Media منتقل نمی‌شود.\n\n"

        "6️⃣ نتیجه واقعی ارسال هر فایل در لاگ "
        "ثبت می‌شود.\n\n"

        "⚠️ اکانت Telegram که Session آن استفاده "
        "می‌شود باید خودش در مقصد دسترسی ارسال داشته باشد."
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

    global destination
    global destination_name
    global destination_type
    global stop_requested

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
    # TYPE
    # -----------------------------------------------------

    if data.startswith("type:"):

        dtype = data.split(
            ":",
            1,
        )[1]

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
    # SELECT
    # -----------------------------------------------------

    if data.startswith("select:"):

        parts = data.split(":")

        dtype = parts[1]
        index = int(parts[2])

        items = destination_cache.get(
            dtype,
            [],
        )

        if index < 0 or index >= len(items):

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

        # ---------------------------------------------
        # تست دسترسی مقصد
        # ---------------------------------------------

        try:

            permissions = (
                await tg.get_permissions(
                    destination,
                    await tg.get_me(),
                )
            )

            print(
                "================================"
            )

            print(
                "📍 DESTINATION SELECTED"
            )

            print(
                f"Name: {destination_name}"
            )

            print(
                f"ID: {destination.id}"
            )

            print(
                f"Type: {dtype}"
            )

            print(
                f"Send permission: "
                f"{getattr(permissions, 'send_messages', None)}"
            )

            print(
                "================================"
            )

        except Exception as e:

            print(
                "Permission check error:",
                repr(e),
            )

        await query.edit_message_text(
            "✅ <b>مقصد انتخاب شد</b>\n\n"
            f"📍 {destination_name}\n"
            f"📂 {dtype}\n\n"
            "حالا لینک‌ها را بفرست.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📥 دریافت لینک‌ها",
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
                    ]
                ]),
            )

            return

        await query.edit_message_text(
            "📥 <b>آماده دریافت لینک‌ها</b>\n\n"
            f"📍 مقصد: {destination_name}\n\n"
            "حالا یک یا چند لینک Telegram "
            "را ارسال کن.",
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

        stop_requested = True

        await query.edit_message_text(
            "🛑 <b>درخواست توقف ثبت شد.</b>\n\n"
            "بعد از پایان عملیات فعلی متوقف می‌شود.",
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
# SEND MEDIA
# =========================================================

async def send_media(message):

    if destination is None:

        print(
            "❌ DESTINATION IS NONE"
        )

        return {
            "success": False,
            "count": 0,
            "error": "مقصد انتخاب نشده.",
        }

    try:

        print(
            "================================"
        )

        print(
            "📥 MEDIA RECEIVED"
        )

        print(
            f"📍 Destination: "
            f"{destination_name}"
        )

        print(
            f"🆔 Destination ID: "
            f"{destination.id}"
        )

        print(
            f"📦 Source Message ID: "
            f"{message.id}"
        )

        print(
            "📤 SENDING..."
        )

        # -------------------------------------------------
        # ارسال فایل بدون متن تبلیغاتی
        # -------------------------------------------------

        sent = await tg.send_file(
            entity=destination,
            file=message.media,
            caption=None,
            force_document=False,
        )

        # -------------------------------------------------
        # بررسی نتیجه واقعی
        # -------------------------------------------------

        if sent is None:

            print(
                "❌ TELEGRAM RETURNED NONE"
            )

            return {
                "success": False,
                "count": 0,
                "error": (
                    "Telegram نتیجه ارسال "
                    "برنگرداند."
                ),
            }

        if isinstance(sent, list):

            count = len(sent)

        else:

            count = 1

        if count <= 0:

            print(
                "❌ ZERO MESSAGES SENT"
            )

            return {
                "success": False,
                "count": 0,
                "error": (
                    "هیچ پیامی ارسال نشد."
                ),
            }

        print(
            f"✅ SEND SUCCESS: "
            f"{count} message(s)"
        )

        print(
            "================================"
        )

        return {
            "success": True,
            "count": count,
            "error": None,
        }

    except FloodWaitError as e:

        print(
            f"⏳ FLOOD WAIT: "
            f"{e.seconds}s"
        )

        await asyncio.sleep(
            e.seconds
        )

        try:

            sent = await tg.send_file(
                entity=destination,
                file=message.media,
                caption=None,
                force_document=False,
            )

            if sent is None:

                return {
                    "success": False,
                    "count": 0,
                    "error": (
                        "Retry نتیجه‌ای نداشت."
                    ),
                }

            if isinstance(sent, list):
                count = len(sent)
            else:
                count = 1

            print(
                f"✅ RETRY SUCCESS: "
                f"{count}"
            )

            return {
                "success": True,
                "count": count,
                "error": None,
            }

        except Exception as retry_error:

            print(
                "❌ RETRY FAILED:"
            )

            print(
                type(retry_error).__name__
            )

            print(
                repr(retry_error)
            )

            return {
                "success": False,
                "count": 0,
                "error": str(
                    retry_error
                ),
            }

    except Exception as e:

        print(
            "❌ SEND MEDIA ERROR"
        )

        print(
            "Exception:",
            type(e).__name__,
        )

        print(
            "Details:",
            repr(e),
        )

        print(
            "================================"
        )

        return {
            "success": False,
            "count": 0,
            "error": (
                f"{type(e).__name__}: {e}"
            ),
        }


# =========================================================
# PROCESS ONE LINK
# =========================================================

async def process_one_link(link):

    parsed = parse_link(link)

    if not parsed:

        return {
            "success": False,
            "count": 0,
            "reason": (
                "لینک Telegram معتبر نیست."
            ),
        }

    username, payload = parsed

    print(
        f"🔗 Processing: @{username}"
    )

    # -----------------------------------------------------
    # GET BOT
    # -----------------------------------------------------

    try:

        bot = await tg.get_entity(
            username
        )

    except Exception as e:

        print(
            "❌ GET ENTITY ERROR:",
            repr(e),
        )

        return {
            "success": False,
            "count": 0,
            "reason": str(e),
        }

    # -----------------------------------------------------
    # START BOT
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
            f"📨 Sent to @{username}: "
            f"{command}"
        )

    except Exception as e:

        print(
            "❌ START ERROR:",
            repr(e),
        )

        return {
            "success": False,
            "count": 0,
            "reason": str(e),
        }

    # -----------------------------------------------------
    # WAIT FOR MEDIA
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
                "❌ READ ERROR:",
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

            print(
                f"📦 Media found: "
                f"message {message.id}"
            )

            result = await send_media(
                message
            )

            if result["success"]:

                media_count += (
                    result["count"]
                )

                last_media_time = (
                    asyncio.get_running_loop().time()
                )

            else:

                print(
                    "❌ FILE NOT SENT:"
                )

                print(
                    result["error"]
                )

        # -------------------------------------------------
        # اگر فایل دریافت شده و مدتی فایل جدیدی نیامد
        # -------------------------------------------------

        if (
            media_count > 0
            and last_media_time is not None
            and (
                asyncio.get_running_loop().time()
                - last_media_time
            ) >= AFTER_MEDIA_WAIT
        ):

            break

        await asyncio.sleep(1)

    # -----------------------------------------------------
    # RESULT
    # -----------------------------------------------------

    if media_count > 0:

        return {
            "success": True,
            "count": media_count,
            "reason": None,
        }

    return {
        "success": False,
        "count": 0,
        "reason": (
            "فایلی با موفقیت به مقصد ارسال نشد."
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
                f"🔗 لینک‌ها: {total}\n"
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
                    f"🔗 لینک: "
                    f"{number}/{total}\n"
                    f"📦 ارسال موفق: "
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

                print(
                    "❌ LINK FAILED:"
                )

                print(
                    result["reason"]
                )

            await asyncio.sleep(1)

        # -------------------------------------------------
        # FINAL RESULT
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
            f"📦 فایل‌های واقعاً ارسال‌شده: "
            f"{media_total}",
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

    if processing:

        await update.message.reply_text(
            "⏳ پردازش قبلی هنوز در حال اجراست."
        )

        return

    if not destination:

        await update.message.reply_text(
            "❌ ابتدا مقصد را انتخاب کن.",
            reply_markup=main_menu(),
        )

        return

    text = (
        update.message.text
        or ""
    )

    links = extract_links(text)

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
# START COMMAND
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
            "❌ Session اکانت Telegram معتبر نیست."
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
        "🔌 Connecting Telegram..."
    )

    await tg.connect()

    if not await tg.is_user_authorized():

        print(
            "❌ Telegram Session is NOT authorized."
        )

        return

    me = await tg.get_me()

    print(
        "✅ Telegram Session connected"
    )

    print(
        f"👤 Name: "
        f"{me.first_name or ''}"
    )

    print(
        f"🆔 ID: {me.id}"
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
        "🤖 Bot is running."
    )

    await asyncio.Event().wait()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        print(
            "🛑 Stopped."
        )

    except Exception as e:

        print(
            "🔥 FATAL ERROR:"
        )

        print(
            type(e).__name__
        )

        print(
            repr(e)
        )
