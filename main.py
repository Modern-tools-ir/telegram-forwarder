import os
import re
import sys
import json
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
from telethon.tl.functions.messages import ImportChatInviteRequest

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
    log("❌ ERROR: Missing environment variables!")
    log(f"API_ID set: {bool(raw_api_id)} | API_HASH set: {bool(API_HASH)} | BOT_TOKEN set: {bool(BOT_TOKEN)}")
    log("Please set API_ID, API_HASH, and BOT_TOKEN in Railway -> Variables tab.")
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
    log(f"⚠️ Notice: /app/data not writable, using fallback directory: {DATA_DIR}")

SESSION_FILE = os.path.join(DATA_DIR, "telegram")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

WAIT_SECONDS = 65
AFTER_MEDIA_WAIT = 8
PAGE_SIZE = 8
MAX_BOT_HOPS = 3

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
}

settings = default_settings.copy()

destination = None
processing = False
stop_requested = False
waiting_for_custom_caption = False

login_state = {
    "step": None,          # None | "phone" | "code" | "2fa"
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
        "• <b>حالت دکمه‌ها:</b> اگر پستی فوروارد کنید که چند دکمه دانلود داشته باشد، از شما بپرسد یا همه را خودکار دانلود کند.\n"
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
# IN-BOT INTERACTIVE LOGIN HANDLER
# =========================================================

async def prompt_login(update: Update):
    login_state["step"] = "phone"
    text = (
        "🔐 <b>ورود به اکانت تلگرام (Session)</b>\n\n"
        "اکانت شما هنوز لاگین نشده است.\n"
        "لطفاً <b>شماره موبایل</b> اکانت خود را به همراه پیش‌شماره کشور ارسال کنید:\n\n"
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
                "⚠️ <b>بسیار مهم:</b> برای اینکه تلگرام کد را به خاطر ارسال در چت باطل نکند، "
                "حتماً <b>بین ارقام کد فاصله یا خط تیره بگذارید!</b>\n\n"
                "مثال: <code>1 2 3 4 5</code> یا <code>1-2-3-4-5</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            await msg.reply_text(
                f"❌ خطا در ارسال کد:\n<code>{e}</code>\n\nدوباره شماره را با فرمت <code>+98912...</code> بفرستید:",
                parse_mode="HTML",
            )
        return

    if step == "code":
        code = re.sub(r"[^0-9]", "", text)
        if not code:
            await msg.reply_text(
                "❌ کد نامعتبر است. لطفاً کد را با فاصله (مثل <code>1 2 3 4 5</code>) بفرستید:",
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
            await msg.reply_text(
                "✅ <b>ورود با موفقیت انجام شد و Session ذخیره گردید!</b>",
                parse_mode="HTML",
            )
            await show_home(update)

        except SessionPasswordNeededError:
            login_state["step"] = "2fa"
            await msg.reply_text(
                "🔑 <b>اکانت شما دارای رمز تایید دو مرحله‌ای (Two-Step Verification) است.</b>\n\n"
                "لطفاً رمز دو مرحله‌ای خود را ارسال کنید:",
                parse_mode="HTML",
            )
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
            login_state["step"] = "phone"
            await msg.reply_text(
                f"❌ کد وارد شده اشتباه یا منقضی شده است ({type(e).__name__}).\n"
                "لطفاً دوباره شماره موبایل خود را بفرستید تا کد جدید ارسال شود:",
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
            await msg.reply_text(
                "✅ <b>ورود دو مرحله‌ای با موفقیت انجام شد و Session ذخیره گردید!</b>",
                parse_mode="HTML",
            )
            await show_home(update)
        except Exception as e:
            await msg.reply_text(
                f"❌ رمز دو مرحله‌ای اشتباه است یا خطایی رخ داد:\n<code>{e}</code>\n\nدوباره رمز را ارسال کنید:",
                parse_mode="HTML",
            )
        return


# =========================================================
# LINK & BUTTON EXTRACTION
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
    items = []
    seen_urls = set()

    def add_item(title, url):
        url = clean_url(url)
        if not url.startswith("http"):
            url = "https://" + url
        if is_valid_tg_link(url) and url not in seen_urls:
            seen_urls.add(url)
            items.append({"title": title or "لینک استخراج‌شده", "url": url})

    if msg.reply_markup and getattr(msg.reply_markup, "inline_keyboard", None):
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if getattr(btn, "url", None):
                    add_item(btn.text, btn.url)

    text_content = msg.text or msg.caption or ""
    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    for ent in entities:
        if ent.type == "text_link" and getattr(ent, "url", None):
            label = text_content[ent.offset : ent.offset + ent.length]
            add_item(label, ent.url)

    pattern = r"https?://(?:www\.)?(?:t\.me|telegram\.me)/[^\s]+"
    for match in re.finditer(pattern, text_content, re.IGNORECASE):
        add_item("لینک متنی", match.group(0))

    return items


# =========================================================
# PARSE TELEGRAM LINKS
# =========================================================

def parse_tg_link(link: str):
    m_priv = re.search(
        r"(?:t\.me|telegram\.me)/c/(\d+)/(\d+)", link, re.IGNORECASE
    )
    if m_priv:
        return {
            "type": "private_post",
            "chat_id": int("-100" + m_priv.group(1)),
            "msg_id": int(m_priv.group(2)),
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
        }

    return None


# =========================================================
# MEDIA VALIDATION, ANTI-DUPLICATE & CAPTIONING
# =========================================================

def is_real_media(message) -> bool:
    """تشخیص رسانه واقعی بدون نیاز به ایمپورت کلاس‌های خاص"""
    if not message or not getattr(message, "media", None):
        return False
    media_cls_name = message.media.__class__.__name__
    if media_cls_name in ("MessageMediaWebPage", "MessageMediaEmpty"):
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
# SEND MEDIA & ALBUMS TO DESTINATION
# =========================================================

async def send_media_group_or_single(messages_list):
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
            log(f"🛡 Duplicate media skipped: message {msg.id}")
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
        log(f"⏳ FloodWait during send: {e.seconds}s")
        await asyncio.sleep(e.seconds + 1)
        return await send_media_group_or_single(valid_messages)

    except Exception as e:
        log(f"⚠️ Direct send failed ({type(e).__name__}), trying download & re-upload...")
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
                log(f"❌ Re-upload failed: {repr(dl_err)}")
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
# AUTO-JOIN, TIMER & STEP-BUTTON ENGINE
# =========================================================

VERIFY_KEYWORDS = [
    "تایید", "تأیید", "بررسی", "عضو شدم", "جوین شدم", "دریافت",
    "دانلود", "ادامه", "بعدی", "بخش", "قسمت", "کیفیت", "فایل",
    "check", "verify", "joined", "continue", "download", "get",
    "start", "try again", "✅", "🔄", "📥", "📂",
]


def detect_wait_timer(text: str) -> int:
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
    parsed = parse_tg_link(url)
    if not parsed:
        return False

    try:
        if parsed["type"] == "invite":
            updates = await tg(ImportChatInviteRequest(parsed["hash"]))
            if hasattr(updates, "chats") and updates.chats:
                joined_channels.append(updates.chats[0])
            log(f"✅ Joined private sponsor invite: {parsed['hash']}")
            await asyncio.sleep(1.5)
            return True

        if parsed["type"] in ("bot_or_entity", "public_post"):
            username = parsed["username"]
            if username.lower().endswith("bot"):
                return False
            entity = await tg.get_entity(username)
            if getattr(entity, "bot", False):
                return False
            await tg(JoinChannelRequest(entity))
            joined_channels.append(entity)
            log(f"✅ Joined public sponsor channel: @{username}")
            await asyncio.sleep(1.5)
            return True

    except (UserAlreadyParticipantError, InviteRequestSentError):
        return True
    except FloodWaitError as e:
        log(f"⏳ FloodWait on JoinChannel: {e.seconds}s")
        await asyncio.sleep(e.seconds + 1)
    except Exception as e:
        log(f"⚠️ Could not join {url}: {repr(e)}")

    return False


async def cleanup_joined_channels(joined_channels: list):
    if not settings.get("auto_leave", True) or not joined_channels:
        return
    for entity in joined_channels:
        try:
            await tg(LeaveChannelRequest(entity))
            log(f"🚪 Left sponsor channel: {getattr(entity, 'title', entity)}")
            await asyncio.sleep(1)
        except Exception as e:
            log(f"⚠️ Error leaving sponsor channel: {repr(e)}")


async def inspect_and_interact_with_bot_message(
    msg,
    current_bot_username: str,
    joined_channels: list,
    clicked_buttons: set,
):
    text = msg.message or ""

    wait_sec = detect_wait_timer(text)
    if wait_sec > 0:
        log(f"⏱ Timer detected in bot message! Waiting {wait_sec}s...")
        await asyncio.sleep(wait_sec + 1)

    next_bot_links = []
    joined_any = False
    re_start_url = None

    # استفاده از msg.buttons استاندارد خود Telethon (بدون نیاز به ایمپورت کلاس‌های خاص)
    if getattr(msg, "buttons", None):
        for r_idx, row in enumerate(msg.buttons):
            for b_idx, btn in enumerate(row):
                btn_url = getattr(btn, "url", None)
                if btn_url:
                    parsed = parse_tg_link(btn_url)
                    if not parsed:
                        continue

                    if (
                        parsed["type"] == "bot_or_entity"
                        and parsed["username"].lower() == current_bot_username.lower()
                    ):
                        re_start_url = btn_url
                        continue

                    if parsed["type"] == "bot_or_entity" and (
                        parsed["payload"] or parsed["username"].lower().endswith("bot")
                    ):
                        try:
                            ent = await tg.get_entity(parsed["username"])
                            if getattr(ent, "bot", False):
                                next_bot_links.append(btn_url)
                                continue
                        except Exception:
                            pass

                    did_join = await join_sponsor_link(btn_url, joined_channels)
                    if did_join:
                        joined_any = True

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

    if getattr(msg, "buttons", None):
        target_callback = None
        fallback_callback = None

        for r_idx, row in enumerate(msg.buttons):
            for b_idx, btn in enumerate(row):
                btn_key = (msg.id, r_idx, b_idx)
                if btn_key in clicked_buttons:
                    continue

                # اگر دکمه لینک (URL) ندارد، یعنی دکمه کلیکی (Callback) است
                if not getattr(btn, "url", None):
                    btn_text = (getattr(btn, "text", "") or "").lower()
                    if any(kw in btn_text for kw in VERIFY_KEYWORDS):
                        target_callback = (r_idx, b_idx, getattr(btn, "text", ""))
                        break
                    fallback_callback = (r_idx, b_idx, getattr(btn, "text", ""))
            if target_callback:
                break

        chosen = target_callback or (fallback_callback if joined_any else None)
        if chosen:
            r_idx, b_idx, b_title = chosen
            clicked_buttons.add((msg.id, r_idx, b_idx))
            try:
                log(f"🔘 Clicking inline button [{b_title}] on message {msg.id}...")
                await asyncio.sleep(1.5)
                await msg.click(r_idx, b_idx)
                await asyncio.sleep(2)
            except Exception as e:
                log(f"⚠️ Button click error: {repr(e)}")

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

    try:
        command = f"/start {payload}" if payload else "/start"
        request = await tg.send_message(bot, command)
        log(f"📨 [Hop {hop_depth + 1}] Sent to @{username}: {command}")
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
                log(f"⚠️ Read messages error: {repr(e)}")
                await asyncio.sleep(1.5)
                continue

            messages = list(reversed(messages))

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
                await asyncio.sleep(1.5)
                media_batches.append(current_group_list)

            for batch in media_batches:
                res = await send_media_group_or_single(batch)
                if res["success"]:
                    media_count += res["count"]
                    skipped_dups += res.get("skipped", 0)
                    last_activity_time = asyncio.get_running_loop().time()
                else:
                    skipped_dups += res.get("skipped", 0)
                    log(f"❌ Media batch error: {res['error']}")

            if media_count == 0 and discovered_bot_hops:
                next_link = discovered_bot_hops.pop(0)
                log(f"🔀 Hopping to next bot link: {next_link}")
                hop_res = await process_one_link(
                    next_link,
                    hop_depth=hop_depth + 1,
                    joined_channels=joined_channels,
                )
                media_count += hop_res["count"]
                skipped_dups += hop_res.get("skipped", 0)
                if hop_res["success"]:
                    break

            if (
                (media_count > 0 or skipped_dups > 0)
                and (asyncio.get_running_loop().time() - last_activity_time)
                >= AFTER_MEDIA_WAIT
            ):
                break

            await asyncio.sleep(1.5)

    finally:
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
        "reason": "فایلی دریافت نشد.",
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
                log(f"❌ Link failed ({link}): {result['reason']}")

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
            "❌ ابتدا مقصد را از منو انتخاب کن.",
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
    user_id = update.effective_user.id if update.effective_user else None
    log(f"📩 Received /start from user_id: {user_id}")

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
    log("🔌 Starting Bot & Connecting Telegram...")

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

    log("🤖 Advanced Bot is running and polling for messages!")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("🛑 Stopped.")
    except Exception as e:
        log(f"🔥 FATAL ERROR: {type(e).__name__}: {repr(e)}")
