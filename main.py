import os
import re
import sys
import json
import time
import asyncio
import tempfile

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    UserAlreadyParticipantError,
    InviteRequestSentError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
)
from telethon.tl.functions.channels import (
    JoinChannelRequest,
    LeaveChannelRequest,
)
from telethon.tl.functions.messages import (
    ImportChatInviteRequest,
    CheckChatInviteRequest,
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


def log(msg: str):
    print(msg, flush=True)


# =========================================================
# CONFIG & SAFE ENVIRONMENT LOADING
# =========================================================

raw_api_id = os.environ.get("API_ID", "").strip()
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "7504234770").strip())

if not raw_api_id or not API_HASH or not BOT_TOKEN:
    log("❌ ERROR: Missing environment variables (API_ID, API_HASH, BOT_TOKEN)!")
    sys.exit(1)

API_ID = int(raw_api_id)

DATA_DIR = "/app/data"
try:
    os.makedirs(DATA_DIR, exist_ok=True)
    test_file = os.path.join(DATA_DIR, ".write_test")
    with open(test_file, "w") as f:
        f.write("ok")
    os.remove(test_file)
except Exception:
    DATA_DIR = os.path.abspath("./data")
    os.makedirs(DATA_DIR, exist_ok=True)
    log(f"⚠️ Notice: /app/data not writable, using fallback: {DATA_DIR}")

SESSION_FILE = os.path.join(DATA_DIR, "telegram")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

# تنظیمات سرعت بالا
WAIT_SECONDS = 55
AFTER_MEDIA_WAIT = 3.5
POLL_INTERVAL = 0.4
PAGE_SIZE = 8
MAX_BOT_HOPS = 4

tg: TelegramClient = None


# =========================================================
# STATE & SETTINGS MANAGEMENT
# =========================================================

default_settings = {
    "destination_id": None,
    "destination_name": None,
    "destination_type": None,
    "button_mode": "prompt",      # "prompt" | "auto"
    "anti_duplicate": True,
    "auto_leave": True,
    "caption_mode": "none",       # "none" | "clean" | "custom"
    "custom_caption": "",
    "sent_media_ids": [],
    "pending_cleanup_ids": [],    # ذخیره آیدی کانال/گروه‌های اسپانسر برای حذف تضمینی
}

settings = default_settings.copy()

destination = None
processing = False
stop_requested = False
waiting_for_custom_caption = False
current_live_step = "آماده به کار"

login_state = {
    "step": None,
    "phone": None,
    "phone_code_hash": None,
}

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
            log(f"⚠️ Error loading config.json: {repr(e)}")


def save_settings():
    try:
        if len(settings["sent_media_ids"]) > 5000:
            settings["sent_media_ids"] = settings["sent_media_ids"][-5000:]
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"⚠️ Error saving config.json: {repr(e)}")


async def restore_destination():
    global destination
    if not tg or not await tg.is_user_authorized():
        return
    dest_id = settings.get("destination_id")
    if not dest_id:
        return
    try:
        destination = await tg.get_entity(dest_id)
        log(f"✅ Restored destination: {settings.get('destination_name')} ({dest_id})")
    except Exception:
        await load_destinations()
        for dtype in ("groups", "channels", "saved"):
            for dialog in destination_cache[dtype]:
                if dialog.entity.id == dest_id:
                    destination = dialog.entity
                    log(f"✅ Restored destination from dialogs: {dialog.name}")
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
# ADVANCED MENUS & KEYBOARDS
# =========================================================

def main_menu():
    dest_label = settings.get("destination_name") or "انتخاب نشده"
    if len(dest_label) > 16:
        dest_label = dest_label[:16] + "..."

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"📍 مقصد: {dest_label}", callback_data="dest_menu"),
            InlineKeyboardButton("📊 وضعیت زنده", callback_data="status"),
        ],
        [
            InlineKeyboardButton("⚙️ تنظیمات هوشمند", callback_data="settings_menu"),
            InlineKeyboardButton("📥 ارسال لینک/پست", callback_data="links"),
        ],
        [
            InlineKeyboardButton("🚪 پاکسازی اسپانسرها", callback_data="force_cleanup"),
            InlineKeyboardButton("🧹 ریست ضدتکرار", callback_data="clear_dups"),
        ],
        [
            InlineKeyboardButton("🛑 توقف و خالی کردن صف", callback_data="stop"),
            InlineKeyboardButton("❓ راهنما", callback_data="help"),
        ],
    ])


def settings_menu():
    btn_mode_text = (
        "🤔 پرسش هنگام چند دکمه"
        if settings["button_mode"] == "prompt"
        else "⚡ دانلود خودکار همه"
    )
    dup_text = "🟢 فعال" if settings["anti_duplicate"] else "🔴 غیرفعال"
    leave_text = "🟢 فعال (فوری)" if settings["auto_leave"] else "🔴 غیرفعال"

    cap_map = {
        "none": "🚫 بدون کپشن",
        "clean": "🧹 پاکسازی تبلیغات",
        "custom": "✍️ کپشن اختصاصی",
    }
    cap_text = cap_map.get(settings["caption_mode"], "🚫 بدون کپشن")

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"🎛 دکمه‌های پست: {btn_mode_text}",
                callback_data="toggle:button_mode",
            )
        ],
        [
            InlineKeyboardButton(
                f"🚪 حذف خودکار کانال/گروه اسپانسر: {leave_text}",
                callback_data="toggle:auto_leave",
            )
        ],
        [
            InlineKeyboardButton(
                f"🛡 سیستم ضد تکرار رسانه: {dup_text}",
                callback_data="toggle:anti_duplicate",
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
                "✏️ تعیین متن کپشن اختصاصی",
                callback_data="set_custom_caption",
            )
        ],
        [
            InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="home"),
        ],
    ])


def destination_type_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 کانال‌ها", callback_data="type:channels"),
            InlineKeyboardButton("👥 گروه‌ها", callback_data="type:groups"),
        ],
        [
            InlineKeyboardButton("💾 پیام‌های ذخیره‌شده (Saved)", callback_data="type:saved"),
        ],
        [
            InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="home"),
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
    state_icon = "⚡ در حال استخراج..." if processing else "🟢 آماده"

    text = (
        "🤖 <b>پنل پیشرفته استخراج و فوروارد هوشمند</b>\n\n"
        f"👤 <b>اکانت متصل:</b> {me.first_name or 'بدون نام'} (<code>{me.id}</code>)\n"
        f"📍 <b>مقصد فعلی:</b> <b>{dest_title}</b>\n"
        f"📡 <b>وضعیت:</b> {state_icon} | <b>صف:</b> {len(link_queue)} لینک\n"
        f"🧭 <b>آخرین مرحله:</b> <i>{current_live_step}</i>\n"
        f"🛡 <b>آرشیو ضدتکرار:</b> {len(settings['sent_media_ids'])} فایل\n\n"
        "💡 <i>هر لینک یا پستی (عکس، ویدیو یا فایل دارای دکمه) را مستقیم به اینجا بفرستید:</i>"
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
        "• <b>دکمه‌های پست:</b> انتخاب کیفیت/دکمه قبل از دانلود یا دانلود خودکار همه.\n"
        "• <b>حذف خودکار اسپانسر:</b> خروج و پاکسازی فوری تمام کانال‌ها و گروه‌های جوین اجباری به محض دریافت فایل.\n"
        "• <b>ضد تکرار:</b> جلوگیری از ارسال فایل‌های تکراری.\n"
        f"• <b>کپشن اختصاصی:</b> <code>{custom_cap}</code>\n\n"
        "برای تغییر هر گزینه روی دکمه آن بزنید:"
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
        "📍 <b>انتخاب مقصد ارسال رسانه‌ها</b>\n\n"
        f"📢 کانال‌ها: <b>{channels}</b>\n"
        f"👥 گروه‌ها: <b>{groups}</b>\n"
        f"💾 پیام‌های ذخیره‌شده: <b>{saved}</b>\n\n"
        "نوع مقصد مورد نظر را انتخاب کنید:"
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
        f"📄 صفحه {page + 1} از {total_pages} (مجموع: {len(items)})\n\n"
        "روی مقصد مورد نظر کلیک کنید:"
    )
    await update.callback_query.edit_message_text(
        text, parse_mode="HTML", reply_markup=keyboard
    )


async def show_status(update: Update):
    dest_name = settings.get("destination_name") or "انتخاب نشده"
    state = "⚡ در حال پردازش فعال" if processing else "🟢 آماده به کار"
    text = (
        "📊 <b>وضعیت زنده سیستم</b>\n\n"
        f"⚙️ <b>حالت فعلی:</b> {state}\n"
        f"🧭 <b>مرحله جاری:</b> <code>{current_live_step}</code>\n"
        f"📍 <b>مقصد:</b> <b>{dest_name}</b> ({settings.get('destination_type') or '-'})\n"
        f"📥 <b>لینک‌های در صف:</b> <b>{len(link_queue)}</b>\n"
        f"🚪 <b>اسپانسرهای در لیست حذف:</b> <b>{len(settings.get('pending_cleanup_ids', []))}</b>\n"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 بروزرسانی وضعیت", callback_data="status"),
            InlineKeyboardButton("🛑 توقف کامل", callback_data="stop"),
        ],
        [
            InlineKeyboardButton("🏠 منوی اصلی", callback_data="home"),
        ],
    ])
    await update.callback_query.edit_message_text(
        text, parse_mode="HTML", reply_markup=keyboard
    )


async def show_help(update: Update):
    text = (
        "❓ <b>راهنمای نسخه فوق سریع و هوشمند</b>\n\n"
        "⚡ <b>ارسال فوری بدون تاخیر:</b> ربات به محض دیدن عکس، ویدیو یا فایل، در کمتر از نیم ثانیه آن را به مقصد می‌فرستد و گول پیام‌های «تا ۲۰ ثانیه دیگر حذف می‌شود» را نمی‌خورد.\n\n"
        "🧭 <b>نمایش زنده مراحل:</b> هنگام پردازش هر لینک، دقیقاً روی پیام وضعیت می‌بینید که ربات در حال عضویت، کلیک روی دکمه، پرش به ربات دوم یا ارسال فایل است.\n\n"
        "🚪 <b>حذف کامل کانال و گروه اسپانسر:</b> بلافاصله پس از گرفتن فایل، تمام کانال‌ها و گروه‌های تبلیغاتی (عمومی و خصوصی) ترک و از لیست چت‌های اکانت شما پاک می‌شوند.\n\n"
        "🔀 <b>پرش هوشمند بین ربات‌ها:</b> اگر ربات اول شما را با دکمه یا پیام ویرایش‌شده به ربات دوم و سوم بفرستد، ربات خودش به ترتیب وارد همه آن‌ها می‌شود تا فایل اصلی را بگیرد."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="home")
    ]])
    await update.callback_query.edit_message_text(
        text, parse_mode="HTML", reply_markup=keyboard
    )


# =========================================================
# IN-BOT INTERACTIVE LOGIN HANDLER
# =========================================================

async def prompt_login(update: Update):
    login_state["step"] = "phone"
    text = (
        "🔐 <b>ورود به اکانت تلگرام (Session)</b>\n\n"
        "اکانت شما هنوز لاگین نشده است.\n"
        "لطفاً <b>شماره موبایل</b> اکانت خود را با پیش‌شماره کشور بفرستید:\n\n"
        "مثال: <code>+989123456789</code>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML")


async def handle_login_step(update: Update):
    msg = update.message
    text = (msg.text or "").strip()
    step = login_state.get("step")

    if step == "phone":
        phone = re.sub(r"\s+", "", text)
        if not phone.startswith("+"):
            phone = "+" + phone
        try:
            sent = await tg.send_code_request(phone)
            login_state["phone"] = phone
            login_state["phone_code_hash"] = sent.phone_code_hash
            login_state["step"] = "code"

            await msg.reply_text(
                "📩 <b>کد تایید به تلگرام شما ارسال شد!</b>\n\n"
                "⚠️ <b>مهم:</b> حتماً <b>بین ارقام کد فاصله بگذارید</b> تا تلگرام کد را باطل نکند!\n\n"
                "مثال: <code>1 2 3 4 5</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            await msg.reply_text(
                f"❌ خطا در ارسال کد:\n<code>{e}</code>\n\nدوباره شماره را بفرستید:",
                parse_mode="HTML",
            )
        return

    if step == "code":
        code = re.sub(r"[^0-9]", "", text)
        if not code:
            await msg.reply_text(
                "❌ کد نامعتبر است. کد را با فاصله (مثل <code>1 2 3 4 5</code>) بفرستید:",
                parse_mode="HTML",
            )
            return

        try:
            await tg.sign_in(
                phone=login_state["phone"],
                code=code,
                phone_code_hash=login_state["phone_code_hash"],
            )
            login_state["step"] = None
            await restore_destination()
            await msg.reply_text("✅ <b>ورود با موفقیت انجام شد!</b>", parse_mode="HTML")
            await show_home(update)

        except SessionPasswordNeededError:
            login_state["step"] = "2fa"
            await msg.reply_text(
                "🔑 <b>لطفاً رمز تایید دو مرحله‌ای (Two-Step Verification) خود را بفرستید:</b>",
                parse_mode="HTML",
            )
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
            login_state["step"] = "phone"
            await msg.reply_text(
                f"❌ کد اشتباه یا منقضی شده ({type(e).__name__}). دوباره شماره موبایل را بفرستید:",
                parse_mode="HTML",
            )
        except Exception as e:
            await msg.reply_text(f"❌ خطا در ورود:\n<code>{e}</code>", parse_mode="HTML")
        return

    if step == "2fa":
        try:
            await tg.sign_in(password=text)
            login_state["step"] = None
            await restore_destination()
            await msg.reply_text("✅ <b>ورود دو مرحله‌ای با موفقیت انجام شد!</b>", parse_mode="HTML")
            await show_home(update)
        except Exception as e:
            await msg.reply_text(
                f"❌ رمز اشتباه است:\n<code>{e}</code>\n\nدوباره رمز را ارسال کنید:",
                parse_mode="HTML",
            )
        return


# =========================================================
# LINK & BUTTON EXTRACTION (SUPPORTS tg://resolve & HYPERLINKS)
# =========================================================

def normalize_tg_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip().rstrip(".,!?،؛)]}>\"'")
    # تبدیل لینک‌های tg://resolve?domain=X&start=Y به لینک استاندارد https://t.me/
    if url.lower().startswith("tg://resolve"):
        m_dom = re.search(r"domain=([A-Za-z0-9_]+)", url, re.IGNORECASE)
        m_start = re.search(r"(?:start|startgroup)=([^&\s]+)", url, re.IGNORECASE)
        if m_dom:
            base = f"https://t.me/{m_dom.group(1)}"
            if m_start:
                base += f"?start={m_start.group(1)}"
            return base
    if url.lower().startswith("tg://join"):
        m_inv = re.search(r"invite=([A-Za-z0-9_-]+)", url, re.IGNORECASE)
        if m_inv:
            return f"https://t.me/+{m_inv.group(1)}"
    if not url.startswith("http") and ("t.me/" in url.lower() or "telegram.me/" in url.lower()):
        url = "https://" + url.lstrip("/")
    return url


def is_valid_tg_link(url: str) -> bool:
    return bool(
        re.search(
            r"https?://(?:www\.)?(?:t\.me|telegram\.me)/[^\s]+",
            url,
            re.IGNORECASE,
        )
    )


def extract_items_from_message(msg) -> list:
    items = []
    seen_urls = set()

    def add_item(title, raw_url):
        url = normalize_tg_url(raw_url)
        if is_valid_tg_link(url) and url not in seen_urls:
            seen_urls.add(url)
            items.append({"title": (title or "لینک").strip(), "url": url})

    # ۱. دکمه‌های شیشه‌ای زیر پست
    if msg.reply_markup and getattr(msg.reply_markup, "inline_keyboard", None):
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if getattr(btn, "url", None):
                    add_item(btn.text, btn.url)

    # ۲. لینک‌های مخفی داخل متن و کپشن
    text_content = msg.text or msg.caption or ""
    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    for ent in entities:
        if ent.type == "text_link" and getattr(ent, "url", None):
            label = text_content[ent.offset : ent.offset + ent.length]
            add_item(label, ent.url)

    # ۳. لینک‌های آشکار در متن و کپشن
    pattern = r"(?:https?://(?:www\.)?(?:t\.me|telegram\.me)/[^\s]+|tg://(?:resolve|join)\?[^\s]+)"
    for match in re.finditer(pattern, text_content, re.IGNORECASE):
        add_item("لینک متنی", match.group(0))

    return items


# =========================================================
# PARSE TELEGRAM LINKS
# =========================================================

def parse_tg_link(raw_link: str):
    link = normalize_tg_url(raw_link)
    if not link:
        return None

    m_priv = re.search(
        r"(?:t\.me|telegram\.me)/c/(\d+)/(\d+)", link, re.IGNORECASE
    )
    if m_priv:
        return {
            "type": "private_post",
            "chat_id": int("-100" + m_priv.group(1)),
            "msg_id": int(m_priv.group(2)),
            "url": link,
        }

    m_inv = re.search(
        r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)",
        link,
        re.IGNORECASE,
    )
    if m_inv:
        return {
            "type": "invite",
            "hash": m_inv.group(1),
            "url": link,
        }

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
            "url": link,
        }

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
            "url": link,
        }

    return None


# =========================================================
# MEDIA VALIDATION, ANTI-DUPLICATE & CAPTIONING
# =========================================================

def is_real_media(message) -> bool:
    if not message or not getattr(message, "media", None):
        return False
    media_cls_name = message.media.__class__.__name__
    if media_cls_name in ("MessageMediaWebPage", "MessageMediaEmpty", "MessageMediaUnsupported"):
        return False
    return True


def get_media_unique_id(message):
    if not message or not getattr(message, "media", None):
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
        cleaned = re.sub(r"https?://[^\s]+", "", original_text)
        cleaned = re.sub(r"(?:t\.me|telegram\.me)/[^\s]+", "", cleaned)
        cleaned = re.sub(r"@[A-Za-z0-9_]+", "", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned or None
    return None


# =========================================================
# ULTRA-FAST MEDIA SENDER (ZERO DELAY)
# =========================================================

async def send_media_group_or_single(messages_list):
    """ارسال آنی عکس، ویدیو، فایل، ویس یا آلبوم در کمتر از نیم ثانیه"""
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
            log(f"🛡 Duplicate skipped: msg {msg.id}")
            skipped_dups += 1
            continue
        valid_messages.append(msg)

    if not valid_messages:
        return {
            "success": skipped_dups > 0,
            "count": 0,
            "skipped": skipped_dups,
            "error": "تکراری" if skipped_dups > 0 else "بدون مدیا",
        }

    captions = [build_caption(m.message or "") for m in valid_messages]
    media_files = [m.media for m in valid_messages]

    try:
        # ارسال مستقیم File Reference روی سرور تلگرام (بدون دانلود، در ۰.۲ ثانیه)
        if len(valid_messages) == 1:
            sent = await tg.send_file(
                entity=destination,
                file=media_files[0],
                caption=captions[0],
                force_document=False,
            )
        else:
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
            log(f"⚡ Instant send success: {count} item(s)")
            return {"success": True, "count": count, "skipped": skipped_dups, "error": None}

    except FloodWaitError as e:
        log(f"⏳ FloodWait during send: {e.seconds}s")
        await asyncio.sleep(e.seconds + 0.5)
        return await send_media_group_or_single(valid_messages)

    except Exception as e:
        err_str = str(e).lower()
        log(f"⚠️ Direct send error ({type(e).__name__}): {e}")

        # فقط اگر چت قفل فوروارد (Restricted) داشت، دانلود و آپلود کن
        if "restricted" in err_str or "forward" in err_str:
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
                    log(f"❌ Restricted fallback failed: {repr(dl_err)}")
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass

            if sent_count > 0:
                return {"success": True, "count": sent_count, "skipped": skipped_dups, "error": None}

        return {"success": False, "count": 0, "skipped": skipped_dups, "error": str(e)}

    return {"success": False, "count": 0, "skipped": skipped_dups, "error": "خطای نامشخص"}


# =========================================================
# COMPLETE SPONSOR JOIN & GUARANTEED LEAVE/DELETE ENGINE
# =========================================================

VERIFY_KEYWORDS = [
    "تایید", "تأیید", "بررسی", "عضو شدم", "جوین شدم", "دریافت",
    "دانلود", "ادامه", "بعدی", "بخش", "قسمت", "کیفیت", "فایل",
    "پارت", "مشاهده", "کلیک", "باز کردن", "ورود",
    "check", "verify", "joined", "continue", "download", "get",
    "start", "try again", "unlock", "✅", "🔄", "📥", "📂", "🎬",
]

# کلماتی که نشان می‌دهند عددِ داخل متن، هشدارِ حذفِ فایل است نه تایمر انتظار!
SELF_DESTRUCT_WORDS = [
    "حذف", "پاک", "ذخیره", "سیو", "فوروارد", "منقضی", "انقضا",
    "پیام های ذخیره", "سریع", "ثانیه دیگر", "ثانيه ديگر",
    "delete", "remove", "save", "forward", "expire", "destruct",
]


def detect_wait_timer(text: str) -> int:
    """
    تشخیص هوشمند تایمر انتظار واقعی از هشدار حذف ۲۰ ثانیه‌ای!
    اگر متن درباره حذف/ذخیره شدن فایل باشد، ۰ برمی‌گرداند تا ربات اصلاً معطل نکند.
    """
    if not text:
        return 0
    low = text.lower()
    if any(w in low for w in SELF_DESTRUCT_WORDS):
        return 0

    if "صبر" in low or "منتظر" in low or "wait" in low:
        m = re.search(
            r"(\d+)\s*(?:ثانیه|ثانيه|second|seconds|sec|s)\b",
            low,
        )
        if m:
            sec = int(m.group(1))
            if 1 <= sec <= 15:
                return sec
    return 0


def register_sponsor_for_cleanup(entity):
    """ثبت آیدی کانال/گروه اسپانسر در لیست دائمی برای اطمینان از حذف صددرصدی"""
    if not entity:
        return
    cid = getattr(entity, "id", None)
    if not cid:
        return
    # تبدیل آیدی به فرمت استاندارد تلگرام برای کانال‌ها و سوپرگروه‌ها
    if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
        full_id = int(f"-100{cid}") if not str(cid).startswith("-100") else cid
    elif entity.__class__.__name__ == "Chat":
        full_id = -abs(cid)
    else:
        full_id = cid

    if full_id not in settings["pending_cleanup_ids"]:
        settings["pending_cleanup_ids"].append(full_id)
        save_settings()


async def join_sponsor_link(url: str, joined_entities: dict, step_cb=None) -> bool:
    parsed = parse_tg_link(url)
    if not parsed:
        return False

    try:
        if parsed["type"] == "invite":
            inv_hash = parsed["hash"]
            if step_cb:
                await step_cb(f"🔗 عضویت در لینک خصوصی اسپانسر...")

            # ابتدا بررسی می‌کنیم آیا قبلاً عضو بوده‌ایم یا مشخصات چت چیست
            try:
                chk = await tg(CheckChatInviteRequest(inv_hash))
                if hasattr(chk, "chat") and chk.chat:
                    joined_entities[chk.chat.id] = chk.chat
                    register_sponsor_for_cleanup(chk.chat)
                    return True
            except Exception:
                pass

            updates = await tg(ImportChatInviteRequest(inv_hash))
            if hasattr(updates, "chats") and updates.chats:
                for ch in updates.chats:
                    joined_entities[ch.id] = ch
                    register_sponsor_for_cleanup(ch)
            log(f"✅ Joined private sponsor: {inv_hash}")
            await asyncio.sleep(0.6)
            return True

        if parsed["type"] in ("bot_or_entity", "public_post"):
            username = parsed["username"]
            if username.lower().endswith("bot"):
                return False
            entity = await tg.get_entity(username)
            if getattr(entity, "bot", False):
                return False

            if step_cb:
                await step_cb(f"🔗 عضویت در اسپانسر @{username}...")

            await tg(JoinChannelRequest(entity))
            joined_entities[entity.id] = entity
            register_sponsor_for_cleanup(entity)
            log(f"✅ Joined public sponsor: @{username}")
            await asyncio.sleep(0.6)
            return True

    except UserAlreadyParticipantError:
        # حتی اگر از قبل عضو شده بود، در لیست حذف قرار بده تا پاک شود
        try:
            if parsed["type"] == "invite":
                chk = await tg(CheckChatInviteRequest(parsed["hash"]))
                if hasattr(chk, "chat") and chk.chat:
                    joined_entities[chk.chat.id] = chk.chat
                    register_sponsor_for_cleanup(chk.chat)
            elif parsed.get("username"):
                ent = await tg.get_entity(parsed["username"])
                joined_entities[ent.id] = ent
                register_sponsor_for_cleanup(ent)
        except Exception:
            pass
        return True

    except InviteRequestSentError:
        log(f"📨 Join request sent for private sponsor: {url}")
        await asyncio.sleep(0.5)
        return True

    except FloodWaitError as e:
        log(f"⏳ FloodWait on Join: {e.seconds}s")
        await asyncio.sleep(e.seconds + 0.5)

    except Exception as e:
        log(f"⚠️ Could not join {url}: {repr(e)}")

    return False


async def cleanup_joined_channels(joined_entities: dict, step_cb=None) -> int:
    """
    خروج کامل و حذف گفتگو از تمام کانال‌ها، سوپرگروه‌ها و گروه‌های معمولی اسپانسر
    """
    if not settings.get("auto_leave", True):
        return 0

    # ترکیب موجودیت‌های فعلی و آیدی‌های ذخیره‌شده
    targets = list(joined_entities.values())
    seen_raw_ids = {getattr(e, "id", None) for e in targets if getattr(e, "id", None)}

    for saved_id in list(settings.get("pending_cleanup_ids", [])):
        raw_id = int(str(saved_id).replace("-100", "").lstrip("-"))
        if raw_id not in seen_raw_ids:
            targets.append(saved_id)

    if not targets:
        return 0

    if step_cb:
        await step_cb(f"🧹 در حال خروج و حذف {len(targets)} کانال/گروه تبلیغاتی...")

    left_count = 0
    for item in targets:
        try:
            # delete_dialog هم از کانال/گروه لفت می‌دهد و هم چت را کامل حذف می‌کند
            await tg.delete_dialog(item)
            left_count += 1
            log(f"🚪 Deleted & left sponsor dialog: {getattr(item, 'title', item)}")
        except Exception:
            try:
                await tg(LeaveChannelRequest(item))
                left_count += 1
                log(f"🚪 Left sponsor channel (fallback): {item}")
            except Exception as e2:
                log(f"⚠️ Cleanup skip for {item}: {repr(e2)}")
        await asyncio.sleep(0.3)

    settings["pending_cleanup_ids"] = []
    save_settings()
    joined_entities.clear()
    return left_count


# =========================================================
# SMART BOT INTERACTION & MULTI-HOP REDIRECT DETECTOR
# =========================================================

async def inspect_and_interact_with_bot_message(
    msg,
    current_bot_username: str,
    current_payload: str,
    joined_entities: dict,
    clicked_buttons: set,
    step_cb=None,
):
    text = msg.message or ""

    # ۱. بررسی تایمر واقعی (بدون گول خوردن از هشدار ۲۰ ثانیه حذف)
    wait_sec = detect_wait_timer(text)
    if wait_sec > 0:
        if step_cb:
            await step_cb(f"⏱ انتظار برای تایمر اجباری ربات ({wait_sec} ثانیه)...")
        await asyncio.sleep(wait_sec + 0.5)

    next_bot_links = []
    joined_any = False
    re_start_url = None

    # جمع‌آوری تمام لینک‌های داخل دکمه‌های شیشه‌ای و هایپرلینک‌های متن
    button_urls = []
    if getattr(msg, "buttons", None):
        for row in msg.buttons:
            for btn in row:
                b_url = getattr(btn, "url", None)
                if b_url:
                    button_urls.append((getattr(btn, "text", "") or "", normalize_tg_url(b_url)))

    # بررسی هایپرلینک‌های داخل متن پیام ربات
    for ent in getattr(msg, "entities", None) or []:
        ent_url = getattr(ent, "url", None)
        if ent_url:
            button_urls.append(("لینک متن", normalize_tg_url(ent_url)))

    # بررسی لینک‌های آشکار داخل متن پیام ربات
    for match in re.finditer(
        r"(?:https?://(?:www\.)?(?:t\.me|telegram\.me)/[^\s]+|tg://(?:resolve|join)\?[^\s]+)",
        text,
        re.IGNORECASE,
    ):
        u = normalize_tg_url(match.group(0))
        if all(u != existing_url for _, existing_url in button_urls):
            button_urls.append(("لینک متنی", u))

    # ۲. تفکیک لینک‌های اسپانسر از لینک‌های پرش به ربات بعدی
    for btn_label, b_url in button_urls:
        parsed = parse_tg_link(b_url)
        if not parsed:
            continue

        # الف) اگر لینک دعوت خصوصی است -> قطعاً کانال/گروه اسپانسر است
        if parsed["type"] == "invite":
            if await join_sponsor_link(b_url, joined_entities, step_cb):
                joined_any = True
            continue

        # ب) اگر لینک به خودِ همین ربات است
        if (
            parsed["type"] == "bot_or_entity"
            and parsed["username"].lower() == current_bot_username.lower()
        ):
            # اگر پارامتر استارت جدیدی دارد، هم می‌تواند دکمه تایید باشد و هم مرحله بعد
            re_start_url = b_url
            if parsed.get("payload") and parsed["payload"] != current_payload:
                if b_url not in next_bot_links:
                    next_bot_links.append(b_url)
            continue

        # ج) اگر لینک دارای ?start= به ربات دیگری است -> پرش به ربات دوم!
        if parsed["type"] == "bot_or_entity" and parsed.get("payload"):
            if b_url not in next_bot_links:
                next_bot_links.append(b_url)
            continue

        # د) اگر یوزرنیم بدون پارامتر است، چک کن ربات است یا کانال/گروه اسپانسر
        if parsed["type"] == "bot_or_entity":
            uname = parsed["username"]
            try:
                ent = await tg.get_entity(uname)
                if getattr(ent, "bot", False):
                    # اگر ربات دیگری است ولی در دکمه کلمات دانلود/دریافت دارد، به عنوان ربات بعدی ثبت کن
                    if any(k in btn_label.lower() for k in VERIFY_KEYWORDS):
                        if b_url not in next_bot_links:
                            next_bot_links.append(b_url)
                else:
                    # کانال یا گروه اسپانسر است
                    if await join_sponsor_link(b_url, joined_entities, step_cb):
                        joined_any = True
            except Exception:
                pass

    # ۳. کلیک هوشمند روی دکمه تایید عضویت یا دکمه‌های مرحله‌ای (Callback)
    if getattr(msg, "buttons", None):
        target_callback = None
        fallback_callback = None

        # امضای پیام با احتساب تاریخ ویرایش (تا اگر پیام بعد از کلیک ویرایش شد، دکمه‌های جدیدش هم کلیک شوند!)
        edit_stamp = str(getattr(msg, "edit_date", "") or "")

        for r_idx, row in enumerate(msg.buttons):
            for b_idx, btn in enumerate(row):
                if getattr(btn, "url", None):
                    continue

                btn_title = (getattr(btn, "text", "") or "").strip()
                btn_key = (msg.id, edit_stamp, r_idx, b_idx, btn_title)
                if btn_key in clicked_buttons:
                    continue

                btn_low = btn_title.lower()
                if any(kw in btn_low for kw in VERIFY_KEYWORDS):
                    target_callback = (r_idx, b_idx, btn_title, btn_key)
                    break
                fallback_callback = (r_idx, b_idx, btn_title, btn_key)
            if target_callback:
                break

        chosen = target_callback or (fallback_callback if joined_any else None)
        if chosen:
            r_idx, b_idx, b_title, btn_key = chosen
            clicked_buttons.add(btn_key)
            try:
                if step_cb:
                    await step_cb(f"🔘 کلیک خودکار روی دکمه «{b_title}»...")
                log(f"🔘 Clicking button [{b_title}] on msg {msg.id}...")
                click_res = await msg.click(r_idx, b_idx)

                # اگر دکمه کلیکی در پاسخ یک URL مخفی برگرداند (Callback URL Redirect)
                res_url = getattr(click_res, "url", None)
                if res_url:
                    norm_res_url = normalize_tg_url(res_url)
                    if norm_res_url and norm_res_url not in next_bot_links:
                        log(f"🔀 Callback returned redirect URL: {norm_res_url}")
                        next_bot_links.insert(0, norm_res_url)

                await asyncio.sleep(0.8)
            except Exception as e:
                log(f"⚠️ Button click error: {repr(e)}")

        elif joined_any and re_start_url:
            btn_key = (msg.id, "restart_url", re_start_url)
            if btn_key not in clicked_buttons:
                clicked_buttons.add(btn_key)
                if re_start_url not in next_bot_links:
                    next_bot_links.insert(0, re_start_url)

    return next_bot_links


# =========================================================
# PROCESS DIRECT CHANNEL POSTS & BOT LINKS
# =========================================================

async def process_channel_post(parsed_info, step_cb=None):
    try:
        if step_cb:
            await step_cb("📥 در حال دریافت مستقیم پست از کانال...")

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

        if getattr(msg, "grouped_id", None):
            surrounding = await tg.get_messages(
                entity,
                ids=list(range(max(1, msg_id - 10), msg_id + 11)),
            )
            album = [
                m for m in surrounding
                if m and getattr(m, "grouped_id", None) == msg.grouped_id
            ]
            if step_cb:
                await step_cb(f"⚡ ارسال فوری آلبوم ({len(album)} رسانه) به مقصد...")
            res = await send_media_group_or_single(album)
        else:
            if step_cb:
                await step_cb("⚡ ارسال فوری رسانه به مقصد...")
            res = await send_media_group_or_single([msg])

        return {
            "success": res["success"],
            "count": res["count"],
            "skipped": res.get("skipped", 0),
            "reason": res["error"],
        }
    except Exception as e:
        return {"success": False, "count": 0, "skipped": 0, "reason": str(e)}


async def process_one_link(
    link: str,
    hop_depth: int = 0,
    joined_entities: dict = None,
    visited_links: set = None,
    step_cb=None,
):
    if joined_entities is None:
        joined_entities = {}
    if visited_links is None:
        visited_links = set()

    norm_link = normalize_tg_url(link)
    if norm_link in visited_links:
        return {"success": False, "count": 0, "skipped": 0, "reason": "لینک تکراری در چرخه"}
    visited_links.add(norm_link)

    if hop_depth >= MAX_BOT_HOPS:
        return {
            "success": False,
            "count": 0,
            "skipped": 0,
            "reason": "تعداد پرش بین ربات‌ها از حد مجاز گذشت.",
        }

    parsed = parse_tg_link(norm_link)
    if not parsed:
        return {
            "success": False,
            "count": 0,
            "skipped": 0,
            "reason": "لینک تلگرام معتبر نیست.",
        }

    if parsed["type"] in ("private_post", "public_post"):
        return await process_channel_post(parsed, step_cb=step_cb)

    if parsed["type"] != "bot_or_entity":
        return {
            "success": False,
            "count": 0,
            "skipped": 0,
            "reason": "نوع لینک پشتیبانی نمی‌شود.",
        }

    username = parsed["username"]
    payload = parsed.get("payload")

    try:
        if step_cb:
            await step_cb(f"📡 اتصال به @{username} (مرحله {hop_depth + 1})...")
        bot = await tg.get_entity(username)
    except Exception as e:
        return {"success": False, "count": 0, "skipped": 0, "reason": str(e)}

    # اگر لینک به یک کانال عمومی یا گروه اشاره داشت (نه ربات)
    if not getattr(bot, "bot", False):
        return {
            "success": False,
            "count": 0,
            "skipped": 0,
            "reason": f"@{username} یک ربات نیست.",
        }

    try:
        command = f"/start {payload}" if payload else "/start"
        if step_cb:
            await step_cb(f"📨 ارسال دستور شروع به @{username}...")
        request = await tg.send_message(bot, command)
        log(f"📨 [Hop {hop_depth + 1}] Sent to @{username}: {command}")
    except Exception as e:
        return {"success": False, "count": 0, "skipped": 0, "reason": str(e)}

    start_time = asyncio.get_running_loop().time()
    min_id = max(0, request.id - 1)
    processed_media_msg_ids = set()
    inspected_text_signatures = set()
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
                messages = await tg.get_messages(bot, limit=30, min_id=min_id)
            except Exception as e:
                log(f"⚠️ Read error: {repr(e)}")
                await asyncio.sleep(0.5)
                continue

            messages = [
                m for m in reversed(messages)
                if m and m.id > request.id and not m.out
            ]

            # =====================================================
            # اولویت ۱: شکار و ارسال فوق سریع رسانه‌ها (قبل از هر کاری!)
            # =====================================================
            media_batches = []
            current_group_id = None
            current_group_list = []

            for message in messages:
                if is_real_media(message) and message.id not in processed_media_msg_ids:
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

            if current_group_list:
                await asyncio.sleep(0.6)
                media_batches.append(current_group_list)

            for batch in media_batches:
                if step_cb:
                    await step_cb(
                        f"⚡ دریافت رسانه از @{username}! در حال ارسال فوری به مقصد..."
                    )
                res = await send_media_group_or_single(batch)
                if res["success"]:
                    media_count += res["count"]
                    skipped_dups += res.get("skipped", 0)
                    last_activity_time = asyncio.get_running_loop().time()
                else:
                    skipped_dups += res.get("skipped", 0)
                    log(f"❌ Media batch error: {res['error']}")

            # =====================================================
            # اولویت ۲: بررسی پیام‌های متنی، دکمه‌ها و پیام‌های ویرایش‌شده
            # =====================================================
            for message in messages:
                # حتی اگر پیام مدیا دارد ولی زیرش دکمه مرحله بعد یا لینک ربات دوم است، دکمه‌هایش را هم بررسی کن
                btn_count = sum(len(r) for r in (getattr(message, "buttons", None) or []))
                sig = (
                    message.id,
                    str(getattr(message, "edit_date", "") or ""),
                    btn_count,
                    len(message.message or ""),
                )
                if sig in inspected_text_signatures:
                    continue

                if not is_real_media(message) or btn_count > 0:
                    inspected_text_signatures.add(sig)
                    hops = await inspect_and_interact_with_bot_message(
                        message,
                        username,
                        payload,
                        joined_entities,
                        clicked_buttons,
                        step_cb=step_cb,
                    )
                    for h in hops:
                        if h not in discovered_bot_hops and normalize_tg_url(h) not in visited_links:
                            discovered_bot_hops.append(h)
                            last_activity_time = asyncio.get_running_loop().time()

            # =====================================================
            # اولویت ۳: پرش هوشمند به ربات دوم/سوم در صورت وجود لینک هدایت
            # =====================================================
            if media_count == 0 and discovered_bot_hops:
                next_link = discovered_bot_hops.pop(0)
                parsed_next = parse_tg_link(next_link)
                next_name = parsed_next.get("username", "ربات بعدی") if parsed_next else "ربات بعدی"

                if step_cb:
                    await step_cb(f"🔀 پرش هوشمند به @{next_name} برای دریافت فایل...")
                log(f"🔀 Hopping to next bot link: {next_link}")

                hop_res = await process_one_link(
                    next_link,
                    hop_depth=hop_depth + 1,
                    joined_entities=joined_entities,
                    visited_links=visited_links,
                    step_cb=step_cb,
                )
                media_count += hop_res["count"]
                skipped_dups += hop_res.get("skipped", 0)
                if hop_res["success"]:
                    break

            # خروج سریع به محض اینکه همه فایل‌های ربات دریافت و ارسال شدند
            if (
                (media_count > 0 or skipped_dups > 0)
                and (asyncio.get_running_loop().time() - last_activity_time)
                >= AFTER_MEDIA_WAIT
            ):
                break

            await asyncio.sleep(POLL_INTERVAL)

    finally:
        # به محض تمام شدن کار لینک در سطح اول، فوری از تمام کانال‌ها و گروه‌های اسپانسر خارج شو و پاکشان کن
        if hop_depth == 0:
            await cleanup_joined_channels(joined_entities, step_cb=step_cb)

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
        "reason": "فایلی دریافت نشد.",
    }


# =========================================================
# LIVE QUEUE RUNNER WITH REAL-TIME TELEGRAM STATUS
# =========================================================

async def run_queue(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    global processing, stop_requested, current_live_step
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
            "🚀 <b>موتور هوشمند استخراج آغاز به کار کرد</b>\n\n"
            f"📥 لینک‌های در صف: <b>{len(link_queue)}</b>\n"
            f"📍 مقصد: <b>{settings.get('destination_name')}</b>"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🛑 توقف فوری", callback_data="stop")
        ]]),
    )

    last_edit_time = 0.0

    try:
        while link_queue and not stop_requested:
            item = link_queue.pop(0)
            total_processed += 1
            link = item["url"]
            title = item.get("title") or "لینک"

            async def update_live_step(step_text: str, force: bool = False):
                global current_live_step
                nonlocal last_edit_time
                current_live_step = step_text
                now = time.monotonic()
                if not force and (now - last_edit_time) < 0.85:
                    return
                last_edit_time = now
                try:
                    await status_message.edit_text(
                        "⚡ <b>در حال استخراج و ارسال هوشمند...</b>\n\n"
                        f"🏷 <b>عنوان:</b> {title}\n"
                        f"🧭 <b>مرحله فعلی:</b> <b>{step_text}</b>\n\n"
                        f"📊 <b>پیشرفت:</b> لینک {total_processed} (صف انتظار: {len(link_queue)})\n"
                        f"📦 <b>ارسال‌شده به مقصد:</b> <b>{media_total}</b> رسانه\n"
                        f"🛡 <b>تکراریِ ردشده:</b> {skipped_total}\n\n"
                        f"🔗 <code>{link}</code>",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🛑 توقف فوری", callback_data="stop")
                        ]]),
                    )
                except Exception:
                    pass

            await update_live_step("🔍 شروع آنالیز لینک...", force=True)
            result = await process_one_link(link, step_cb=update_live_step)

            if result["success"]:
                success_links += 1
                media_total += result["count"]
                skipped_total += result.get("skipped", 0)
                await update_live_step("✅ رسانه با موفقیت به مقصد ارسال شد!", force=True)
            else:
                failed_links += 1
                log(f"❌ Link failed ({link}): {result['reason']}")
                await update_live_step(f"❌ ناموفق: {result['reason']}", force=True)

            await asyncio.sleep(0.4)

        current_live_step = "آماده به کار"
        header = "🛑 عملیات متوقف شد" if stop_requested else "🏁 عملیات با موفقیت کامل شد"
        if stop_requested:
            link_queue.clear()

        await status_message.edit_text(
            f"<b>{header}</b>\n\n"
            f"🔗 <b>کل لینک‌های پردازش‌شده:</b> {total_processed}\n"
            f"✅ <b>موفق:</b> {success_links} | ❌ <b>ناموفق:</b> {failed_links}\n"
            f"📦 <b>مجموع رسانه‌های ارسال‌شده:</b> <b>{media_total}</b>\n"
            f"🛡 <b>فایل‌های تکراری ردشده:</b> {skipped_total}\n"
            f"🚪 <b>وضعیت اسپانسرها:</b> پاکسازی کامل انجام شد",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
    finally:
        processing = False
        stop_requested = False
        current_live_step = "آماده به کار"


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global destination, stop_requested, waiting_for_custom_caption

    if not is_owner(update):
        return

    query = update.callback_query
    await query.answer()

    if not await tg.is_user_authorized():
        await prompt_login(update)
        return

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
            "متن یا آیدی کانال خود را بفرستید تا زیر تمام فایل‌های ارسالی قرار بگیرد:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 انصراف", callback_data="settings_menu")
            ]]),
        )
        return

    if data == "force_cleanup":
        await query.edit_message_text("🧹 <b>در حال بررسی و خروج از کانال/گروه‌های اسپانسر...</b>", parse_mode="HTML")
        left_num = await cleanup_joined_channels({})
        await query.edit_message_text(
            f"✅ <b>پاکسازی انجام شد!</b>\nتعداد <b>{left_num}</b> کانال/گروه اسپانسر ترک و حذف شدند.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if data == "clear_dups":
        settings["sent_media_ids"] = []
        save_settings()
        await query.edit_message_text(
            "🧹 <b>حافظه ضد تکرار با موفقیت صفر شد.</b>",
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
            "✅ <b>مقصد با موفقیت انتخاب و قفل شد</b>\n\n"
            f"📍 <b>{settings['destination_name']}</b>\n"
            f"📂 نوع: {dtype}\n\n"
            "حالا لینک بفرستید یا هر پستی که دکمه دانلود دارد را فوروارد کنید:",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

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
            f"⚡ تعداد <b>{len(selected)}</b> مورد به صف پردازش فوری اضافه شد.",
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
                "❌ ابتدا مقصد را انتخاب کنید.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📍 انتخاب مقصد", callback_data="dest_menu")
                ]]),
            )
            return

        await query.edit_message_text(
            "📥 <b>آماده دریافت لینک یا پست</b>\n\n"
            f"📍 مقصد فعلی: <b>{settings.get('destination_name')}</b>\n\n"
            "کافیست لینک بفرستید یا پستی که زیرش دکمه شیشه‌ای دارد را به اینجا فوروارد کنید.",
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
            "🛑 <b>توقف فوری ثبت شد و صف انتظار خالی گردید.</b>",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if data == "help":
        await show_help(update)
        return


# =========================================================
# UNIVERSAL MESSAGE HANDLER
# =========================================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_custom_caption, batch_counter

    if not is_owner(update) or not update.message:
        return

    if not await tg.is_user_authorized():
        if login_state.get("step"):
            await handle_login_step(update)
        else:
            await prompt_login(update)
        return

    msg = update.message

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
            "❌ ابتدا مقصد را از منوی زیر انتخاب کنید:",
            reply_markup=main_menu(),
        )
        return

    extracted_items = extract_items_from_message(msg)

    if not extracted_items:
        await msg.reply_text(
            "❌ هیچ لینک یا دکمه استارتی در این پیام پیدا نشد."
        )
        return

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
                f"🚀 دانلود خودکار همه ({len(extracted_items)} مورد)",
                callback_data=f"batch:{b_id}:all",
            )
        ])
        buttons.append([
            InlineKeyboardButton("❌ لغو", callback_data=f"batch:{b_id}:cancel")
        ])

        await msg.reply_text(
            f"🔍 در این پست <b>{len(extracted_items)} دکمه/لینک</b> شناسایی شد.\n"
            "کدام مورد استخراج و به مقصد ارسال شود؟",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    link_queue.extend(extracted_items)
    if processing:
        await msg.reply_text(
            f"➕ تعداد <b>{len(extracted_items)}</b> لینک به صف اضافه شد (در صف: {len(link_queue)}).",
            parse_mode="HTML",
        )
    else:
        asyncio.create_task(run_queue(msg.chat_id, context))


# =========================================================
# START COMMAND & MAIN
# =========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if not is_owner(update):
        await update.message.reply_text(
            f"⛔ دسترسی ندارید.\nآیدی عددی شما: <code>{user_id}</code>",
            parse_mode="HTML",
        )
        return

    if not await tg.is_user_authorized():
        await prompt_login(update)
        return

    login_state["step"] = None
    await show_home(update)


async def main():
    global tg
    log("================================")
    log("🔌 Starting Ultra-Fast Bot & Connecting Telegram...")

    load_settings()

    tg = TelegramClient(
        SESSION_FILE,
        API_ID,
        API_HASH,
    )
    await tg.connect()

    if await tg.is_user_authorized():
        me = await tg.get_me()
        log(f"✅ Telegram Session connected: {me.first_name or ''} ({me.id})")
        await restore_destination()
    else:
        log("⚠️ Telegram Session is NOT authorized yet. Send /start to the bot in Telegram to log in!")

    log("================================")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, message_handler)
    )

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    log("🤖 Ultra-Fast Bot is running!")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("🛑 Stopped.")
    except Exception as e:
        log(f"🔥 FATAL ERROR: {type(e).__name__}: {repr(e)}")
