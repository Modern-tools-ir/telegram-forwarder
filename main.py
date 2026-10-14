import os
import re
import json
import asyncio
import tempfile

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    UserAlreadyParticipantError,
    InviteRequestSentError,
)
from telethon.tl.functions.channels import (
    JoinChannelRequest,
    LeaveChannelRequest,
)
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import (
    MessageMediaWebPage,
    MessageMediaEmpty,
    KeyboardButtonUrl,
    KeyboardButtonCallback,
)

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
# CONFIG & PERSISTENCE
# =========================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Telegram user ID صاحب ربات
OWNER_ID = int(os.environ.get("OWNER_ID", 7504234770))

# محل Session و تنظیمات دائمی در Railway Volume
DATA_DIR = "/app/data"
SESSION_FILE = "/app/data/telegram"
CONFIG_FILE = "/app/data/config.json"

os.makedirs(DATA_DIR, exist_ok=True)

WAIT_SECONDS = 65
AFTER_MEDIA_WAIT = 8
PAGE_SIZE = 8
MAX_BOT_HOPS = 3

tg = TelegramClient(
    SESSION_FILE,
    API_ID,
    API_HASH,
)


# =========================================================
# STATE & SETTINGS MANAGEMENT
# =========================================================

default_settings = {
    "destination_id": None,
    "destination_name": None,
    "destination_type": None,
    "button_mode": "prompt",      # "prompt" (پرسش قبل از دانلود) | "auto" (دانلود خودکار همه)
    "anti_duplicate": True,       # جلوگیری از ارسال فایل تکراری
    "auto_leave": True,           # خروج خودکار از کانال‌های جوین اجباری
    "caption_mode": "none",       # "none" | "clean" | "custom"
    "custom_caption": "",
    "sent_media_ids": [],         # لیست شناسه‌های فایل‌های ارسال‌شده
}

settings = default_settings.copy()

destination = None
processing = False
stop_requested = False
waiting_for_custom_caption = False

# صف پردازش لینک‌ها و کش دکمه‌های انتخابی
link_queue = []
pending_batches = {}
batch_counter = 0

destination_cache = {
    "groups": [],
    "channels": [],
    "saved": [],
}


def load_settings():
    global settings
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                settings.update(loaded)
        except Exception as e:
            print("⚠️ Error loading config.json:", repr(e))


def save_settings():
    try:
        # محدود کردن تاریخچه ضد تکرار به ۵۰۰۰ آیتم آخر برای حفظ سرعت
        if len(settings["sent_media_ids"]) > 5000:
            settings["sent_media_ids"] = settings["sent_media_ids"][-5000:]
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("⚠️ Error saving config.json:", repr(e))


async def restore_destination():
    global destination
    dest_id = settings.get("destination_id")
    if not dest_id:
        return
    try:
        destination = await tg.get_entity(dest_id)
        print(f"✅ Restored destination: {settings.get('destination_name')} ({dest_id})")
    except Exception:
        await load_destinations()
        for dtype in ("groups", "channels", "saved"):
            for dialog in destination_cache[dtype]:
                if dialog.entity.id == dest_id:
                    destination = dialog.entity
                    print(f"✅ Restored destination from dialogs: {dialog.name}")
                    return


# =========================================================
# OWNER CHECK
# =========================================================

def is_owner(update: Update):
    return (
        update.effective_user
        and update.effective_user.id == OWNER_ID
    )


# =========================================================
# MENUS & KEYBOARDS
# =========================================================

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📍 انتخاب مقصد", callback_data="dest_menu"),
            InlineKeyboardButton("📊 وضعیت و صف", callback_data="status"),
        ],
        [
            InlineKeyboardButton("⚙️ تنظیمات هوشمند", callback_data="settings_menu"),
            InlineKeyboardButton("📥 ارسال لینک/پست", callback_data="links"),
        ],
        [
            InlineKeyboardButton("🧹 ریست تاریخچه تکراری", callback_data="clear_dups"),
            InlineKeyboardButton("❓ راهنما", callback_data="help"),
        ],
    ])


def settings_menu():
    btn_mode_text = (
        "🤔 پرسش هنگام چند دکمه"
        if settings["button_mode"] == "prompt"
        else "⚡ دانلود خودکار همه دکمه‌ها"
    )
    dup_text = "✅ فعال" if settings["anti_duplicate"] else "❌ غیرفعال"
    leave_text = "✅ فعال" if settings["auto_leave"] else "❌ غیرفعال"

    cap_map = {
        "none": "🚫 بدون کپشن",
        "clean": "🧹 پاکسازی لینک و تبلیغ",
        "custom": "✍️ کپشن اختصاصی",
    }
    cap_text = cap_map.get(settings["caption_mode"], "🚫 بدون کپشن")

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"🎛 حالت دکمه‌ها: {btn_mode_text}",
                callback_data="toggle:button_mode",
            )
        ],
        [
            InlineKeyboardButton(
                f"🛡 ضد تکرار فایل‌ها: {dup_text}",
                callback_data="toggle:anti_duplicate",
            )
        ],
        [
            InlineKeyboardButton(
                f"🚪 لفت خودکار از اسپانسر: {leave_text}",
                callback_data="toggle:auto_leave",
            )
        ],
        [
            InlineKeyboardButton(
                f"📝 حالت کپشن: {cap_text}",
                callback_data="toggle:caption_mode",
            )
        ],
        [
            InlineKeyboardButton(
                "✏️ ویرایش متن کپشن اختصاصی",
                callback_data="set_custom_caption",
            )
        ],
        [
            InlineKeyboardButton("🔙 منوی اصلی", callback_data="home"),
        ],
    ])


def destination_type_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 گروه‌ها", callback_data="type:groups")],
        [InlineKeyboardButton("📢 کانال‌ها", callback_data="type:channels")],
        [InlineKeyboardButton("💾 پیام‌های ذخیره‌شده", callback_data="type:saved")],
        [InlineKeyboardButton("🔙 منوی اصلی", callback_data="home")],
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
        if getattr(entity, "is_self", False):
            destination_cache["saved"].append(dialog)
            continue
        if getattr(entity, "broadcast", False):
            destination_cache["channels"].append(dialog)
            continue
        if getattr(entity, "megagroup", False) or entity.__class__.__name__ == "Chat":
            destination_cache["groups"].append(dialog)
            continue


def destination_list_keyboard(dtype, page):
    items = destination_cache.get(dtype, [])
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    current = items[start:end]

    buttons = []
    for index, dialog in enumerate(current, start=start):
        title = dialog.name or "بدون نام"
        if len(title) > 32:
            title = title[:32] + "..."
        buttons.append([
            InlineKeyboardButton(
                f"📁 {title}",
                callback_data=f"select:{dtype}:{index}",
            )
        ])

    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton("◀️ قبلی", callback_data=f"page:{dtype}:{page - 1}")
        )
    if end < len(items):
        navigation.append(
            InlineKeyboardButton("بعدی ▶️", callback_data=f"page:{dtype}:{page + 1}")
        )
    if navigation:
        buttons.append(navigation)

    buttons.append([InlineKeyboardButton("🔙 انواع مقصد", callback_data="dest_menu")])
    return InlineKeyboardMarkup(buttons), total_pages


# =========================================================
# UI VIEWS
# =========================================================

async def show_home(update: Update):
    me = await tg.get_me()
    dest_title = settings.get("destination_name") or "انتخاب نشده"

    text = (
        "🎛 <b>پنل مدیریت هوشمند</b>\n\n"
        f"👤 اکانت: {me.first_name or 'بدون نام'} (<code>{me.id}</code>)\n"
        f"📍 مقصد فعلی: <b>{dest_title}</b>\n"
        f"📥 لینک‌های در صف: <b>{len(link_queue)}</b>\n"
        f"🛡 فایل‌های ثبت‌شده در ضدتکرار: <b>{len(settings['sent_media_ids'])}</b>\n\n"
        "می‌توانی مستقیم لینک بفرستی یا هر پستی (عکس/ویدیو/فایل دارای دکمه) را به اینجا فوروارد کنی:"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML", reply_markup=main_menu()
        )
    else:
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=main_menu()
        )


async def show_settings(update: Update):
    custom_cap = settings.get("custom_caption") or "تنظیم نشده"
    text = (
        "⚙️ <b>تنظیمات هوشمند ربات</b>\n\n"
        "• <b>حالت دکمه‌ها:</b> اگر پستی فوروارد کنید که چند دکمه دانلود (مثل کیفیت‌های مختلف) داشته باشد، از شما بپرسد یا همه را خودکار دانلود کند.\n"
        "• <b>ضد تکرار:</b> جلوگیری از ارسال مجدد عکس/ویدیو/فایل‌های تکراری.\n"
        "• <b>لفت خودکار:</b> خروج از کانال‌های اسپانسر بلافاصله پس از دریافت فایل.\n"
        f"• <b>کپشن اختصاصی فعلی:</b> <code>{custom_cap}</code>\n\n"
        "برای تغییر هر مورد روی آن کلیک کنید:"
    )
    await update.callback_query.edit_message_text(
        text, parse_mode="HTML", reply_markup=settings_menu()
    )


async def show_destination_types(update: Update):
    await load_destinations()
    groups = len(destination_cache["groups"])
    channels = len(destination_cache["channels"])
    saved = len(destination_cache["saved"])

    text = (
        "📍 <b>انتخاب مقصد</b>\n\n"
        f"👥 گروه‌ها: {groups}\n"
        f"📢 کانال‌ها: {channels}\n"
        f"💾 پیام‌های ذخیره‌شده: {saved}\n\n"
        "نوع مقصد را انتخاب کن:"
    )
    await update.callback_query.edit_message_text(
        text, parse_mode="HTML", reply_markup=destination_type_menu()
    )


async def show_destination_list(update: Update, dtype, page=0):
    items = destination_cache.get(dtype, [])
    names = {
        "groups": "👥 گروه‌ها",
        "channels": "📢 کانال‌ها",
        "saved": "💾 پیام‌های ذخیره‌شده",
    }
    title = names.get(dtype, "مقصدها")

    if not items:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 بازگشت", callback_data="dest_menu")
        ]])
        await update.callback_query.edit_message_text(
            f"{title}\n\n❌ موردی پیدا نشد.", reply_markup=keyboard
        )
        return

    keyboard, total_pages = destination_list_keyboard(dtype, page)
    text = (
        f"{title}\n\n"
        f"📄 صفحه {page + 1} از {total_pages}\n"
        f"📊 تعداد: {len(items)}\n\n"
        "مقصد را انتخاب کن:"
    )
    await update.callback_query.edit_message_text(
        text, parse_mode="HTML", reply_markup=keyboard
    )


async def show_status(update: Update):
    dest_name = settings.get("destination_name")
    if dest_name:
        state = "⏳ در حال پردازش" if processing else "🟢 آماده"
        text = (
            "📊 <b>وضعیت سیستم</b>\n\n"
            f"⚙️ وضعیت: {state}\n"
            f"📍 مقصد: <b>{dest_name}</b>\n"
            f"📂 نوع: <b>{settings.get('destination_type')}</b>\n"
            f"📥 لینک‌های موجود در صف انتظار: <b>{len(link_queue)}</b>\n"
        )
    else:
        text = "📊 <b>وضعیت</b>\n\n🟡 مقصدی انتخاب نشده است."

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 توقف و خالی کردن صف", callback_data="stop")],
        [InlineKeyboardButton("📍 تغییر مقصد", callback_data="dest_menu")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
    ])
    await update.callback_query.edit_message_text(
        text, parse_mode="HTML", reply_markup=keyboard
    )


async def show_help(update: Update):
    text = (
        "❓ <b>راهنمای قابلیت‌های پیشرفته</b>\n\n"
        "1️⃣ <b>استخراج خودکار از پست‌ها:</b> کافیست هر پستی (عکس، ویدیو، فایل یا متن) که دارای دکمه شیشه‌ای استارت یا لینک مخفی است را به ربات فوروارد کنید.\n\n"
        "2️⃣ <b>انتخابگر هوشمند کیفیت‌ها:</b> اگر پست دارای چند دکمه باشد، ربات دکمه‌ها را به شما نمایش می‌دهد تا انتخاب کنید کدام‌یک دانلود شود.\n\n"
        "3️⃣ <b>گذر خودکار از جوین اجباری و تایمر:</b> ربات خودش در کانال‌های اسپانسر عضو می‌شود، دکمه تایید/ادامه را می‌زند، تایمرها را صبر می‌کند، به ربات‌های بعدی می‌پرد و در پایان از کانال‌های تبلیغاتی لفت می‌دهد.\n\n"
        "4️⃣ <b>پشتیبانی از همه رسانه‌ها و آلبوم:</b> عکس، ویدیو، فایل، ویس، موزیک و آلبوم‌های چندتایی با فرمت اصلی ارسال می‌شوند.\n\n"
        "5️⃣ <b>ضد تکرار:</b> از ارسال فایل‌های تکراری به مقصد جلوگیری می‌کند."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 منوی اصلی", callback_data="home")
    ]])
    await update.callback_query.edit_message_text(
        text, parse_mode="HTML", reply_markup=keyboard
    )


# =========================================================
# LINK & BUTTON EXTRACTION FROM USER MESSAGES
# =========================================================

def clean_url(url: str) -> str:
    return url.strip().rstrip(".,!?،؛)]}>\"'")


def is_valid_tg_link(url: str) -> bool:
    return bool(
        re.search(
            r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/[^\s]+",
            url,
            re.IGNORECASE,
        )
    )


def extract_items_from_message(msg) -> list:
    """
    استخراج تمام لینک‌های تلگرام از:
    ۱. دکمه‌های شیشه‌ای زیر پست (Inline Keyboard)
    ۲. لینک‌های مخفی داخل متن و کپشن (Entities / Hyperlinks)
    ۳. متن ساده و کپشن عکس/ویدیو/فایل
    خروجی: لیستی از دیکشنری‌ها [{"title": "...", "url": "..."}]
    """
    items = []
    seen_urls = set()

    def add_item(title, url):
        url = clean_url(url)
        if not url.startswith("http"):
            url = "https://" + url
        if is_valid_tg_link(url) and url not in seen_urls:
            seen_urls.add(url)
            items.append({"title": title or "لینک استخراج‌شده", "url": url})

    # ۱. بررسی دکمه‌های شیشه‌ای (Inline Keyboard)
    if msg.reply_markup and getattr(msg.reply_markup, "inline_keyboard", None):
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if getattr(btn, "url", None):
                    add_item(btn.text, btn.url)

    # ۲. بررسی Entities در متن یا کپشن (Hyperlinks)
    text_content = msg.text or msg.caption or ""
    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    for ent in entities:
        if ent.type == "text_link" and getattr(ent, "url", None):
            label = text_content[ent.offset : ent.offset + ent.length]
            add_item(label, ent.url)

    # ۳. بررسی لینک‌های متنی آشکار در متن و کپشن
    pattern = r"https?://(?:www\.)?(?:t\.me|telegram\.me)/[^\s]+"
    for match in re.finditer(pattern, text_content, re.IGNORECASE):
        add_item("لینک متنی", match.group(0))

    return items


# =========================================================
# PARSE TELEGRAM LINKS
# =========================================================

def parse_tg_link(link: str):
    """
    تشخیص نوع لینک تلگرام:
    - bot_start: لینک ربات دارای پارامتر یا بدون پارامتر
    - private_post: لینک پست کانال خصوصی (t.me/c/123456/789)
    - public_post: لینک پست کانال عمومی (t.me/channel/123)
    - invite: لینک عضویت خصوصی (t.me/+xxx یا t.me/joinchat/xxx)
    """
    # لینک پست کانال خصوصی
    m_priv = re.search(
        r"(?:t\.me|telegram\.me)/c/(\d+)/(\d+)", link, re.IGNORECASE
    )
    if m_priv:
        return {
            "type": "private_post",
            "chat_id": int("-100" + m_priv.group(1)),
            "msg_id": int(m_priv.group(2)),
        }

    # لینک دعوت خصوصی
    m_inv = re.search(
        r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)",
        link,
        re.IGNORECASE,
    )
    if m_inv:
        return {
            "type": "invite",
            "hash": m_inv.group(1),
        }

    # لینک پست کانال عمومی
    m_pub = re.search(
        r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})/(\d+)",
        link,
        re.IGNORECASE,
    )
    if m_pub and "?start=" not in link.lower():
        return {
            "type": "public_post",
            "username": m_pub.group(1),
            "msg_id": int(m_pub.group(2)),
        }

    # لینک ربات یا یوزرنیم
    m_bot = re.search(
        r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)(?:\?(?:start|startgroup)=([^&\s]+))?",
        link,
        re.IGNORECASE,
    )
    if m_bot:
        return {
            "type": "bot_or_entity",
            "username": m_bot.group(1),
            "payload": m_bot.group(2),
        }

    return None


# =========================================================
# MEDIA VALIDATION, ANTI-DUPLICATE & CAPTIONING
# =========================================================

def is_real_media(message) -> bool:
    """تشخیص رسانه واقعی (عکس، ویدیو، فایل، ویس، موزیک، گیف) و نادیده گرفتن پیش‌نمایش وب"""
    if not message or not message.media:
        return False
    if isinstance(message.media, (MessageMediaWebPage, MessageMediaEmpty)):
        return False
    return True


def get_media_unique_id(message):
    """استخراج شناسه یکتای رسانه برای سیستم ضد تکرار"""
    if not message or not message.media:
        return None
    doc = getattr(message.media, "document", None)
    if doc and getattr(doc, "id", None):
        return f"doc_{doc.id}"
    photo = getattr(message.media, "photo", None)
    if photo and getattr(photo, "id", None):
        return f"photo_{photo.id}"
    return None


def is_duplicate_media(message) -> bool:
    if not settings.get("anti_duplicate", True):
        return False
    uid = get_media_unique_id(message)
    if not uid:
        return False
    return uid in settings["sent_media_ids"]


def mark_media_sent(message):
    uid = get_media_unique_id(message)
    if uid and uid not in settings["sent_media_ids"]:
        settings["sent_media_ids"].append(uid)
        save_settings()


def build_caption(original_text: str):
    mode = settings.get("caption_mode", "none")
    if mode == "none":
        return None
    if mode == "custom":
        return settings.get("custom_caption") or None
    if mode == "clean":
        if not original_text:
            return None
        # حذف لینک‌ها و آیدی‌های تبلیغاتی از کپشن اصلی
        cleaned = re.sub(r"https?://[^\s]+", "", original_text)
        cleaned = re.sub(r"(?:t\.me|telegram\.me)/[^\s]+", "", cleaned)
        cleaned = re.sub(r"@[A-Za-z0-9_]+", "", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned or None
    return None


# =========================================================
# SEND MEDIA & ALBUMS TO DESTINATION
# =========================================================

async def send_media_group_or_single(messages_list):
    """
    ارسال تکی یا آلبومی (Media Group) انواع عکس، ویدیو و فایل
    به همراه پشتیبانی از کانال‌ها و ربات‌های دارای قفل فوروارد (Restricted Content)
    """
    global destination
    if destination is None:
        await restore_destination()
    if destination is None:
        return {"success": False, "count": 0, "skipped": 0, "error": "مقصد انتخاب نشده."}

    valid_messages = []
    skipped_dups = 0

    for msg in messages_list:
        if not is_real_media(msg):
            continue
        if is_duplicate_media(msg):
            print(f"🛡 Duplicate media skipped: message {msg.id}")
            skipped_dups += 1
            continue
        valid_messages.append(msg)

    if not valid_messages:
        return {
            "success": skipped_dups > 0,
            "count": 0,
            "skipped": skipped_dups,
            "error": "همه فایل‌ها تکراری بودند." if skipped_dups > 0 else "مدیای معتبری یافت نشد.",
        }

    # آماده‌سازی کپشن
    captions = [build_caption(m.message or "") for m in valid_messages]
    media_files = [m.media for m in valid_messages]

    try:
        if len(valid_messages) == 1:
            sent = await tg.send_file(
                entity=destination,
                file=media_files[0],
                caption=captions[0],
                force_document=False,
            )
        else:
            # ارسال به صورت آلبوم یکپارچه
            first_cap = next((c for c in captions if c), None)
            sent = await tg.send_file(
                entity=destination,
                file=media_files,
                caption=first_cap,
                force_document=False,
            )

        if sent:
            for m in valid_messages:
                mark_media_sent(m)
            count = len(sent) if isinstance(sent, list) else 1
            return {"success": True, "count": count, "skipped": skipped_dups, "error": None}

    except FloodWaitError as e:
        print(f"⏳ FloodWait during send: {e.seconds}s")
        await asyncio.sleep(e.seconds + 1)
        return await send_media_group_or_single(valid_messages)

    except Exception as e:
        # در صورتی که فایل قفل فوروارد داشته باشد (ChatForwardsRestrictedError)، دانلود و آپلود مجدد می‌شود
        print(f"⚠️ Direct send failed ({type(e).__name__}), trying download & re-upload...")
        sent_count = 0
        for idx, m in enumerate(valid_messages):
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, dir="/tmp") as tmp:
                    tmp_path = tmp.name
                file_path = await tg.download_media(m, file=tmp_path)
                if file_path:
                    await tg.send_file(
                        entity=destination,
                        file=file_path,
                        caption=captions[idx],
                        force_document=False,
                    )
                    mark_media_sent(m)
                    sent_count += 1
            except Exception as dl_err:
                print("❌ Re-upload failed:", repr(dl_err))
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        if sent_count > 0:
            return {"success": True, "count": sent_count, "skipped": skipped_dups, "error": None}
        return {"success": False, "count": 0, "skipped": skipped_dups, "error": str(e)}

    return {"success": False, "count": 0, "skipped": skipped_dups, "error": "ارسال ناموفق بود."}


# =========================================================
# AUTO-JOIN, TIMER & STEP-BUTTON ENGINE (مورد د)
# =========================================================

VERIFY_KEYWORDS = [
    "تایید", "تأیید", "بررسی", "عضو شدم", "جوین شدم", "دریافت",
    "دانلود", "ادامه", "بعدی", "بخش", "قسمت", "کیفیت", "فایل",
    "check", "verify", "joined", "continue", "download", "get",
    "start", "try again", "✅", "🔄", "📥", "📂",
]


def detect_wait_timer(text: str) -> int:
    """تشخیص تایمر در متن پیام ربات مبدا (مثلاً: لطفاً ۱۵ ثانیه صبر کنید)"""
    if not text:
        return 0
    m = re.search(
        r"(\d+)\s*(?:ثانیه|ثانيه|second|seconds|sec|s)\b",
        text,
        re.IGNORECASE,
    )
    if m:
        sec = int(m.group(1))
        if 1 <= sec <= 60:
            return sec
    return 0


async def join_sponsor_link(url: str, joined_channels: list) -> bool:
    """عضویت خودکار در کانال یا گروه اسپانسر (عمومی یا خصوصی)"""
    parsed = parse_tg_link(url)
    if not parsed:
        return False

    try:
        if parsed["type"] == "invite":
            updates = await tg(ImportChatInviteRequest(parsed["hash"]))
            if hasattr(updates, "chats") and updates.chats:
                joined_channels.append(updates.chats[0])
            print(f"✅ Joined private sponsor invite: {parsed['hash']}")
            await asyncio.sleep(1.5)
            return True

        if parsed["type"] in ("bot_or_entity", "public_post"):
            username = parsed["username"]
            # اگر یوزرنیم به bot ختم شود، کانال نیست بلکه ربات است
            if username.lower().endswith("bot"):
                return False
            entity = await tg.get_entity(username)
            if getattr(entity, "bot", False):
                return False
            await tg(JoinChannelRequest(entity))
            joined_channels.append(entity)
            print(f"✅ Joined public sponsor channel: @{username}")
            await asyncio.sleep(1.5)
            return True

    except (UserAlreadyParticipantError, InviteRequestSentError):
        return True
    except FloodWaitError as e:
        print(f"⏳ FloodWait on JoinChannel: {e.seconds}s")
        await asyncio.sleep(e.seconds + 1)
    except Exception as e:
        print(f"⚠️ Could not join {url}: {repr(e)}")

    return False


async def cleanup_joined_channels(joined_channels: list):
    """خروج خودکار از کانال‌های تبلیغاتی پس از اتمام دریافت فایل‌ها"""
    if not settings.get("auto_leave", True) or not joined_channels:
        return
    for entity in joined_channels:
        try:
            await tg(LeaveChannelRequest(entity))
            print(f"🚪 Left sponsor channel: {getattr(entity, 'title', entity)}")
            await asyncio.sleep(1)
        except Exception as e:
            print("⚠️ Error leaving sponsor channel:", repr(e))


async def inspect_and_interact_with_bot_message(
    msg,
    current_bot_username: str,
    joined_channels: list,
    clicked_buttons: set,
):
    """
    بررسی هوشمند پیام‌های ربات مبدا:
    ۱. تشخیص تایمر و صبر کردن خودکار
    ۲. پیدا کردن لینک کانال‌های جوین اجباری (در دکمه‌ها و متن) و عضویت در آن‌ها
    ۳. تشخیص لینک پرش به ربات بعدی (Multi-Hop Bot)
    ۴. کلیک خودکار روی دکمه‌های تایید عضویت یا دکمه‌های مرحله‌ای (دریافت فایل / ادامه)
    """
    text = msg.message or ""

    # ۱. بررسی تایمر
    wait_sec = detect_wait_timer(text)
    if wait_sec > 0:
        print(f"⏱ Timer detected in bot message! Waiting {wait_sec}s...")
        await asyncio.sleep(wait_sec + 1)

    next_bot_links = []
    joined_any = False
    re_start_url = None

    # ۲. اسکن دکمه‌های شیشه‌ای پیام ربات
    if msg.reply_markup and hasattr(msg.reply_markup, "rows"):
        for r_idx, row in enumerate(msg.reply_markup.rows):
            for b_idx, btn in enumerate(row.buttons):
                if isinstance(btn, KeyboardButtonUrl) and btn.url:
                    parsed = parse_tg_link(btn.url)
                    if not parsed:
                        continue

                    # بررسی اینکه آیا دکمه به خودِ همین ربات اشاره دارد (دکمه تایید از نوع URL)
                    if (
                        parsed["type"] == "bot_or_entity"
                        and parsed["username"].lower() == current_bot_username.lower()
                    ):
                        re_start_url = btn.url
                        continue

                    # بررسی اینکه آیا دکمه به یک ربات دیگر (ربات دوم) اشاره دارد
                    if parsed["type"] == "bot_or_entity" and (
                        parsed["payload"] or parsed["username"].lower().endswith("bot")
                    ):
                        try:
                            ent = await tg.get_entity(parsed["username"])
                            if getattr(ent, "bot", False):
                                next_bot_links.append(btn.url)
                                continue
                        except Exception:
                            pass

                    # در غیر این صورت، کانال یا گروه اسپانسر است -> عضویت خودکار
                    did_join = await join_sponsor_link(btn.url, joined_channels)
                    if did_join:
                        joined_any = True

    # ۳. اسکن لینک‌های داخل متن پیام (اگر کانال اسپانسر را در متن گذاشته باشند)
    for match in re.finditer(
        r"https?://(?:www\.)?(?:t\.me|telegram\.me)/[^\s]+", text, re.IGNORECASE
    ):
        url = clean_url(match.group(0))
        parsed = parse_tg_link(url)
        if parsed and parsed["type"] in ("invite", "bot_or_entity"):
            if (
                parsed["type"] == "bot_or_entity"
                and parsed["username"].lower() == current_bot_username.lower()
            ):
                continue
            did_join = await join_sponsor_link(url, joined_channels)
            if did_join:
                joined_any = True

    # ۴. کلیک خودکار روی دکمه تایید عضویت یا دکمه‌های مرحله‌ای (مورد د)
    if msg.reply_markup and hasattr(msg.reply_markup, "rows"):
        target_callback = None
        fallback_callback = None

        for r_idx, row in enumerate(msg.reply_markup.rows):
            for b_idx, btn in enumerate(row.buttons):
                btn_key = (msg.id, r_idx, b_idx)
                if btn_key in clicked_buttons:
                    continue

                if isinstance(btn, KeyboardButtonCallback):
                    btn_text = (btn.text or "").lower()
                    if any(kw in btn_text for kw in VERIFY_KEYWORDS):
                        target_callback = (r_idx, b_idx, btn.text)
                        break
                    fallback_callback = (r_idx, b_idx, btn.text)
            if target_callback:
                break

        chosen = target_callback or (fallback_callback if joined_any else None)
        if chosen:
            r_idx, b_idx, b_title = chosen
            clicked_buttons.add((msg.id, r_idx, b_idx))
            try:
                print(f"🔘 Clicking inline button [{b_title}] on message {msg.id}...")
                await asyncio.sleep(1.5)
                await msg.click(r_idx, b_idx)
                await asyncio.sleep(2)
            except Exception as e:
                print(f"⚠️ Button click error: {repr(e)}")

        elif joined_any and re_start_url:
            btn_key = (msg.id, "restart_url")
            if btn_key not in clicked_buttons:
                clicked_buttons.add(btn_key)
                next_bot_links.insert(0, re_start_url)

    return next_bot_links


# =========================================================
# PROCESS DIRECT CHANNEL POSTS & BOT LINKS
# =========================================================

async def process_channel_post(parsed_info):
    """کپی مستقیم پست یا آلبوم از کانال‌های عمومی و خصوصی"""
    try:
        target = (
            parsed_info["chat_id"]
            if parsed_info["type"] == "private_post"
            else parsed_info["username"]
        )
        entity = await tg.get_entity(target)
        msg_id = parsed_info["msg_id"]
        msg = await tg.get_messages(entity, ids=msg_id)

        if not msg:
            return {"success": False, "count": 0, "skipped": 0, "reason": "پست یافت نشد."}

        # بررسی اینکه آیا پست بخشی از یک آلبوم (Media Group) است
        if getattr(msg, "grouped_id", None):
            surrounding = await tg.get_messages(
                entity,
                ids=list(range(max(1, msg_id - 10), msg_id + 11)),
            )
            album = [
                m for m in surrounding
                if m and getattr(m, "grouped_id", None) == msg.grouped_id
            ]
            res = await send_media_group_or_single(album)
        else:
            res = await send_media_group_or_single([msg])

        return {
            "success": res["success"],
            "count": res["count"],
            "skipped": res.get("skipped", 0),
            "reason": res["error"],
        }
    except Exception as e:
        return {"success": False, "count": 0, "skipped": 0, "reason": str(e)}


async def process_one_link(link: str, hop_depth: int = 0, joined_channels=None):
    """
    موتور اصلی پردازش لینک با پشتیبانی از:
    - پست کانال‌ها
    - ربات‌های آپلودر، جوین اجباری، تایمر، دکمه‌های مرحله‌ای و پرش به ربات‌های بعدی
    - تجمیع آلبوم‌ها و ارسال انواع رسانه
    """
    if joined_channels is None:
        joined_channels = []

    if hop_depth >= MAX_BOT_HOPS:
        return {
            "success": False,
            "count": 0,
            "skipped": 0,
            "reason": "تعداد پرش بین ربات‌ها از حد مجاز گذشت.",
        }

    parsed = parse_tg_link(link)
    if not parsed:
        return {
            "success": False,
            "count": 0,
            "skipped": 0,
            "reason": "لینک تلگرام معتبر نیست.",
        }

    # اگر لینک مستقیم پست کانال باشد
    if parsed["type"] in ("private_post", "public_post"):
        return await process_channel_post(parsed)

    if parsed["type"] != "bot_or_entity":
        return {
            "success": False,
            "count": 0,
            "skipped": 0,
            "reason": "نوع لینک پشتیبانی نمی‌شود.",
        }

    username = parsed["username"]
    payload = parsed["payload"]

    try:
        bot = await tg.get_entity(username)
    except Exception as e:
        return {"success": False, "count": 0, "skipped": 0, "reason": str(e)}

    # ارسال دستور /start به ربات مبدا
    try:
        command = f"/start {payload}" if payload else "/start"
        request = await tg.send_message(bot, command)
        print(f"📨 [Hop {hop_depth + 1}] Sent to @{username}: {command}")
    except Exception as e:
        return {"success": False, "count": 0, "skipped": 0, "reason": str(e)}

    start_time = asyncio.get_running_loop().time()
    min_id = request.id
    processed_media_msg_ids = set()
    clicked_buttons = set()
    discovered_bot_hops = []

    media_count = 0
    skipped_dups = 0
    last_activity_time = start_time

    try:
        while not stop_requested:
            now = asyncio.get_running_loop().time()
            if now - start_time >= WAIT_SECONDS:
                break

            try:
                messages = await tg.get_messages(bot, limit=35, min_id=min_id)
            except Exception as e:
                print("⚠️ Read messages error:", repr(e))
                await asyncio.sleep(1.5)
                continue

            messages = list(reversed(messages))

            # دسته‌بندی رسانه‌ها برای پشتیبانی از آلبوم (grouped_id)
            media_batches = []
            current_group_id = None
            current_group_list = []

            for message in messages:
                if message.id <= request.id or message.out:
                    continue

                if is_real_media(message):
                    if message.id in processed_media_msg_ids:
                        continue
                    processed_media_msg_ids.add(message.id)
                    last_activity_time = asyncio.get_running_loop().time()

                    gid = getattr(message, "grouped_id", None)
                    if gid:
                        if current_group_id == gid:
                            current_group_list.append(message)
                        else:
                            if current_group_list:
                                media_batches.append(current_group_list)
                            current_group_id = gid
                            current_group_list = [message]
                    else:
                        if current_group_list:
                            media_batches.append(current_group_list)
                            current_group_list = []
                            current_group_id = None
                        media_batches.append([message])
                else:
                    # پیام متنی یا مرحله‌ای است -> بررسی جوین اجباری، تایمر و دکمه‌ها
                    hops = await inspect_and_interact_with_bot_message(
                        message,
                        username,
                        joined_channels,
                        clicked_buttons,
                    )
                    for h in hops:
                        if h not in discovered_bot_hops:
                            discovered_bot_hops.append(h)
                            last_activity_time = asyncio.get_running_loop().time()

            if current_group_list:
                # کمی صبر برای تکمیل شدن پیام‌های آلبوم
                await asyncio.sleep(1.5)
                media_batches.append(current_group_list)

            # ارسال دسته‌های رسانه به مقصد
            for batch in media_batches:
                res = await send_media_group_or_single(batch)
                if res["success"]:
                    media_count += res["count"]
                    skipped_dups += res.get("skipped", 0)
                    last_activity_time = asyncio.get_running_loop().time()
                else:
                    skipped_dups += res.get("skipped", 0)
                    print("❌ Media batch error:", res["error"])

            # اگر هیچ فایلی نیامد اما لینک پرش به ربات بعدی پیدا شد، بلافاصله به ربات بعدی بپر
            if media_count == 0 and discovered_bot_hops:
                next_link = discovered_bot_hops.pop(0)
                print(f"🔀 Hopping to next bot link: {next_link}")
                hop_res = await process_one_link(
                    next_link,
                    hop_depth=hop_depth + 1,
                    joined_channels=joined_channels,
                )
                media_count += hop_res["count"]
                skipped_dups += hop_res.get("skipped", 0)
                if hop_res["success"]:
                    break

            # خروج هوشمند در صورت اتمام ارسال فایل‌ها
            if (
                (media_count > 0 or skipped_dups > 0)
                and (asyncio.get_running_loop().time() - last_activity_time)
                >= AFTER_MEDIA_WAIT
            ):
                break

            await asyncio.sleep(1.5)

    finally:
        # در سطح اول، پس از اتمام کار از کانال‌های اسپانسر خارج شو
        if hop_depth == 0 and joined_channels:
            await cleanup_joined_channels(joined_channels)

    if media_count > 0 or skipped_dups > 0:
        return {
            "success": True,
            "count": media_count,
            "skipped": skipped_dups,
            "reason": None,
        }

    return {
        "success": False,
        "count": 0,
        "skipped": 0,
        "reason": "فایلی دریافت نشد (ممکن است ربات مبدا خاموش باشد یا کپچای تصویری داشته باشد).",
    }


# =========================================================
# QUEUE RUNNER
# =========================================================

async def run_queue(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    global processing, stop_requested
    if processing:
        return

    processing = True
    stop_requested = False

    total_processed = 0
    success_links = 0
    failed_links = 0
    media_total = 0
    skipped_total = 0

    status_message = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🚀 <b>پردازش صف هوشمند آغاز شد</b>\n\n"
            f"📥 لینک‌های در صف: {len(link_queue)}\n"
            f"📍 مقصد: <b>{settings.get('destination_name')}</b>"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🛑 توقف کامل", callback_data="stop")
        ]]),
    )

    try:
        while link_queue and not stop_requested:
            item = link_queue.pop(0)
            total_processed += 1
            link = item["url"]
            title = item.get("title") or "لینک"

            try:
                await status_message.edit_text(
                    "⏳ <b>در حال پردازش هوشمند...</b>\n\n"
                    f"🏷 عنوان: <b>{title}</b>\n"
                    f"🔗 انجام‌شده: {total_processed} (باقی‌مانده در صف: {len(link_queue)})\n"
                    f"📦 فایل‌های ارسال‌شده: <b>{media_total}</b>\n"
                    f"🛡 تکراریِ ردشده: <b>{skipped_total}</b>\n\n"
                    f"<code>{link}</code>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🛑 توقف کامل", callback_data="stop")
                    ]]),
                )
            except Exception:
                pass

            result = await process_one_link(link)

            if result["success"]:
                success_links += 1
                media_total += result["count"]
                skipped_total += result.get("skipped", 0)
            else:
                failed_links += 1
                print(f"❌ Link failed ({link}): {result['reason']}")

            await asyncio.sleep(1)

        header = "🛑 پردازش متوقف شد" if stop_requested else "🏁 پردازش صف کامل شد"
        if stop_requested:
            link_queue.clear()

        await status_message.edit_text(
            f"<b>{header}</b>\n\n"
            f"🔗 کل لینک‌های بررسی‌شده: {total_processed}\n"
            f"✅ لینک‌های موفق: {success_links}\n"
            f"❌ لینک‌های ناموفق: {failed_links}\n"
            f"📦 فایل‌های ارسال‌شده به مقصد: <b>{media_total}</b>\n"
            f"🛡 فایل‌های تکراری ردشده: <b>{skipped_total}</b>",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
    finally:
        processing = False
        stop_requested = False


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global destination, stop_requested, waiting_for_custom_caption

    if not is_owner(update):
        return

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "home":
        waiting_for_custom_caption = False
        await show_home(update)
        return

    if data == "settings_menu":
        waiting_for_custom_caption = False
        await show_settings(update)
        return

    if data.startswith("toggle:"):
        key = data.split(":", 1)[1]
        if key == "button_mode":
            settings["button_mode"] = (
                "auto" if settings["button_mode"] == "prompt" else "prompt"
            )
        elif key == "anti_duplicate":
            settings["anti_duplicate"] = not settings["anti_duplicate"]
        elif key == "auto_leave":
            settings["auto_leave"] = not settings["auto_leave"]
        elif key == "caption_mode":
            modes = ["none", "clean", "custom"]
            curr = settings.get("caption_mode", "none")
            nxt = modes[(modes.index(curr) + 1) % len(modes)]
            settings["caption_mode"] = nxt
        save_settings()
        await show_settings(update)
        return

    if data == "set_custom_caption":
        waiting_for_custom_caption = True
        await query.edit_message_text(
            "✍️ <b>تنظیم کپشن اختصاصی</b>\n\n"
            "لطفاً متن یا آیدی دلخواه خود را ارسال کنید تا زیر تمام فایل‌های ارسالی قرار بگیرد:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 انصراف", callback_data="settings_menu")
            ]]),
        )
        return

    if data == "clear_dups":
        settings["sent_media_ids"] = []
        save_settings()
        await query.edit_message_text(
            "🧹 <b>حافظه ضد تکرار با موفقیت پاکسازی شد.</b>",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if data == "dest_menu":
        await show_destination_types(update)
        return

    if data.startswith("type:"):
        dtype = data.split(":", 1)[1]
        await show_destination_list(update, dtype, 0)
        return

    if data.startswith("page:"):
        _, dtype, page_str = data.split(":")
        await show_destination_list(update, dtype, int(page_str))
        return

    if data.startswith("select:"):
        _, dtype, idx_str = data.split(":")
        index = int(idx_str)
        items = destination_cache.get(dtype, [])
        if index < 0 or index >= len(items):
            await query.edit_message_text("❌ مقصد پیدا نشد.", reply_markup=main_menu())
            return

        dialog = items[index]
        destination = dialog.entity
        settings["destination_id"] = destination.id
        settings["destination_name"] = dialog.name or "بدون نام"
        settings["destination_type"] = dtype
        save_settings()

        await query.edit_message_text(
            "✅ <b>مقصد انتخاب و ذخیره شد</b>\n\n"
            f"📍 {settings['destination_name']}\n"
            f"📂 نوع: {dtype}\n\n"
            "حالا لینک بفرست یا پستی که دکمه دانلود دارد را فوروارد کن.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    # -----------------------------------------------------
    # انتخابگر هوشمند دکمه‌ها (مورد الف)
    # -----------------------------------------------------
    if data.startswith("batch:"):
        _, batch_id_str, action = data.split(":")
        batch_id = int(batch_id_str)
        items = pending_batches.get(batch_id)

        if not items:
            await query.edit_message_text("❌ این لیست منقضی شده است.", reply_markup=main_menu())
            return

        if action == "cancel":
            pending_batches.pop(batch_id, None)
            await query.edit_message_text("❌ عملیات لغو شد.", reply_markup=main_menu())
            return

        if action == "all":
            selected = items
            pending_batches.pop(batch_id, None)
        else:
            idx = int(action)
            if 0 <= idx < len(items):
                selected = [items[idx]]
            else:
                return

        link_queue.extend(selected)
        await query.edit_message_text(
            f"✅ تعداد <b>{len(selected)}</b> لینک به صف پردازش اضافه شد.",
            parse_mode="HTML",
        )
        asyncio.create_task(run_queue(query.message.chat_id, context))
        return

    if data == "status":
        await show_status(update)
        return

    if data == "links":
        if not destination and not settings.get("destination_id"):
            await query.edit_message_text(
                "❌ ابتدا مقصد را انتخاب کن.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📍 انتخاب مقصد", callback_data="dest_menu")
                ]]),
            )
            return

        await query.edit_message_text(
            "📥 <b>آماده دریافت لینک یا پست</b>\n\n"
            f"📍 مقصد: <b>{settings.get('destination_name')}</b>\n\n"
            "می‌توانی لینک بفرستی یا هر پستی (عکس/ویدیو/فایل دارای دکمه شیشه‌ای) را به اینجا فوروارد کنی.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")
            ]]),
        )
        return

    if data == "stop":
        stop_requested = True
        link_queue.clear()
        await query.edit_message_text(
            "🛑 <b>درخواست توقف ثبت شد و صف خالی گردید.</b>",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if data == "help":
        await show_help(update)
        return


# =========================================================
# UNIVERSAL MESSAGE HANDLER (TEXT, PHOTO, VIDEO, FILE, BUTTONS)
# =========================================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_custom_caption, batch_counter

    if not is_owner(update) or not update.message:
        return

    msg = update.message

    # اگر در حالت دریافت متن کپشن اختصاصی هستیم
    if waiting_for_custom_caption and msg.text:
        waiting_for_custom_caption = False
        settings["custom_caption"] = msg.text.strip()
        settings["caption_mode"] = "custom"
        save_settings()
        await msg.reply_text(
            "✅ <b>کپشن اختصاصی ذخیره و فعال شد:</b>\n\n"
            f"<code>{settings['custom_caption']}</code>",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if not destination:
        await restore_destination()
    if not destination:
        await msg.reply_text(
            "❌ ابتدا مقصد را از منو انتخاب کن.",
            reply_markup=main_menu(),
        )
        return

    # استخراج تمام لینک‌ها از دکمه‌های شیشه‌ای، هایپرلینک‌ها و متن/کپشن پست
    extracted_items = extract_items_from_message(msg)

    if not extracted_items:
        await msg.reply_text(
            "❌ هیچ لینک یا دکمه استارتی در این پیام پیدا نشد."
        )
        return

    # مورد الف: اگر بیش از ۱ لینک/دکمه در پست وجود داشت و حالت پرسش فعال بود
    if len(extracted_items) > 1 and settings.get("button_mode") == "prompt":
        batch_counter += 1
        b_id = batch_counter
        pending_batches[b_id] = extracted_items

        buttons = []
        for i, item in enumerate(extracted_items):
            btn_label = item["title"]
            if len(btn_label) > 35:
                btn_label = btn_label[:35] + "..."
            buttons.append([
                InlineKeyboardButton(
                    f"📥 {btn_label}",
                    callback_data=f"batch:{b_id}:{i}",
                )
            ])

        buttons.append([
            InlineKeyboardButton(
                f"🚀 دانلود همه موارد ({len(extracted_items)} لینک)",
                callback_data=f"batch:{b_id}:all",
            )
        ])
        buttons.append([
            InlineKeyboardButton("❌ لغو", callback_data=f"batch:{b_id}:cancel")
        ])

        await msg.reply_text(
            f"🔍 در این پست <b>{len(extracted_items)} دکمه/لینک</b> پیدا شد.\n"
            "کدام مورد را می‌خواهی استخراج و به مقصد ارسال کنی؟",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # اگر ۱ لینک بود یا حالت دانلود خودکار همه فعال بود -> افزودن مستقیم به صف
    link_queue.extend(extracted_items)
    if processing:
        await msg.reply_text(
            f"➕ تعداد <b>{len(extracted_items)}</b> لینک به صف انتظار اضافه شد (مجموع صف: {len(link_queue)}).",
            parse_mode="HTML",
        )
    else:
        asyncio.create_task(run_queue(msg.chat_id, context))


# =========================================================
# START COMMAND & MAIN
# =========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("⛔ دسترسی ندارید.")
        return
    if not await tg.is_user_authorized():
        await update.message.reply_text("❌ Session اکانت Telegram معتبر نیست.")
        return
    await show_home(update)


async def main():
    print("================================")
    print("🔌 Connecting Telegram...")

    load_settings()
    await tg.connect()

    if not await tg.is_user_authorized():
        print("❌ Telegram Session is NOT authorized.")
        return

    me = await tg.get_me()
    print(f"✅ Telegram Session connected: {me.first_name or ''} ({me.id})")

    await restore_destination()
    print("================================")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, message_handler)
    )

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    print("🤖 Advanced Bot is running.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Stopped.")
    except Exception as e:
        print("🔥 FATAL ERROR:", type(e).__name__, repr(e))
