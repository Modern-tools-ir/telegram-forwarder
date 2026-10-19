import os
import re
import gc
import sys
import html
import json
import time
import shutil
import asyncio
import tempfile
import subprocess
import urllib.request

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    ChannelsTooMuchError,
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

GITHUB_RAW_URL = os.environ.get(
    "GITHUB_RAW_URL",
    "https://raw.githubusercontent.com/Modern-tools-ir/telegram-forwarder/main/main.py",
)

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
    DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(DATA_DIR, exist_ok=True)
    log(f"📁 Using local data directory: {DATA_DIR}")

SESSION_FILE = os.path.join(DATA_DIR, "telegram")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

WAIT_SECONDS = 48
AFTER_MEDIA_WAIT = 2.6
POLL_INTERVAL = 0.35
PAGE_SIZE = 8
MAX_BOT_HOPS = 4
BOT_START_TIME = time.time()

tg: TelegramClient = None


# =========================================================
# STATE & SETTINGS MANAGEMENT
# =========================================================

default_settings = {
    "destination_id": None,
    "destination_name": None,
    "destination_type": None,
    "button_mode": "prompt",          # "prompt" | "auto"
    "anti_duplicate": True,
    "auto_leave": True,               # خروج خودکار از کانال/گروه‌های اسپانسر
    "auto_delete_bot": True,          # حذف خودکار پی‌وی ربات آپلودر پس از پایان کار
    "safety_vault": True,             # سپر نجات ۲۰ ثانیه‌ای در Saved Messages
    "caption_mode": "none",           # "none" | "clean" | "replace" | "custom" | "original"
    "custom_caption": "",
    "sent_media_ids": [],
    "pending_cleanup_ids": [],
}

settings = default_settings.copy()

destination = None
processing = False
stop_requested = False
waiting_for_custom_caption = False
current_live_step = "آماده به کار"
last_bot_toast = ""

# نگهداری وضعیت رباتِ در حال پردازش برای «دیدبان زنده و کلیک دستی»
active_bot_session = {
    "bot_entity": None,
    "username": None,
    "last_msg": None,
}

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
    """بازیابی دقیق مقصد با پشتیبانی از آیدی‌های دارای پیشوند -100 برای کانال‌ها و سوپرگروه‌ها"""
    global destination
    if not tg or not await tg.is_user_authorized():
        return
    dest_id = settings.get("destination_id")
    dtype = settings.get("destination_type")
    if not dest_id:
        return

    if dtype == "saved":
        destination = "me"
        return

    # اصلاح خودکار آیدی‌های مثبت قدیمی برای کانال‌ها و سوپرگروه‌ها
    candidates = [dest_id]
    str_id = str(dest_id)
    if dtype in ("channels", "groups") and not str_id.startswith("-"):
        candidates.insert(0, int(f"-100{str_id}"))
        candidates.append(-abs(int(dest_id)))

    for cand in candidates:
        try:
            destination = await asyncio.wait_for(tg.get_entity(cand), timeout=6.0)
            if settings["destination_id"] != cand:
                settings["destination_id"] = cand
                save_settings()
            log(f"✅ Restored destination: {settings.get('destination_name')} ({cand})")
            return
        except Exception:
            pass

    try:
        await load_destinations()
        raw_target = int(str(dest_id).replace("-100", "").lstrip("-"))
        for dt in ("groups", "channels", "saved"):
            for dialog in destination_cache[dt]:
                if dialog.id == dest_id or getattr(dialog.entity, "id", None) == raw_target:
                    destination = "me" if dt == "saved" else dialog.entity
                    settings["destination_id"] = dialog.id
                    save_settings()
                    log(f"✅ Restored destination from dialogs: {dialog.name} ({dialog.id})")
                    return
    except Exception as e:
        log(f"⚠️ Could not restore destination: {repr(e)}")


# =========================================================
# SERVER MANAGEMENT & DIAGNOSTICS ENGINE (AWS UBUNTU)
# =========================================================

def format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days > 0:
        parts.append(f"{days} روز")
    if hours > 0:
        parts.append(f"{hours} ساعت")
    if mins > 0:
        parts.append(f"{mins} دقیقه")
    if not parts:
        parts.append(f"{secs} ثانیه")
    return " و ".join(parts)


async def get_server_stats_text() -> str:
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(tg.get_me(), timeout=5.0)
        ping_ms = int((time.perf_counter() - t0) * 1000)
        ping_str = f"{ping_ms} ms ⚡"
    except Exception:
        ping_str = "نامشخص"

    ram_str = "نامشخص"
    try:
        mem_info = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = int(parts[1].strip().split()[0])
                    mem_info[key] = val
        total_mb = mem_info.get("MemTotal", 0) // 1024
        avail_mb = mem_info.get("MemAvailable", mem_info.get("MemFree", 0)) // 1024
        used_mb = max(0, total_mb - avail_mb)
        pct = int((used_mb / total_mb) * 100) if total_mb else 0
        ram_str = f"{used_mb} MB / {total_mb} MB ({pct}%)"
    except Exception:
        pass

    cpu_str = "نامشخص"
    try:
        load1, load5, load15 = os.getloadavg()
        cores = os.cpu_count() or 1
        cpu_pct = min(100, int((load1 / cores) * 100))
        cpu_str = f"{cpu_pct}% (Load: {load1:.2f} | هسته‌ها: {cores})"
    except Exception:
        pass

    disk_str = "نامشخص"
    try:
        total_b, used_b, free_b = shutil.disk_usage("/")
        total_gb = total_b / (1024 ** 3)
        used_gb = used_b / (1024 ** 3)
        free_gb = free_b / (1024 ** 3)
        disk_pct = int((used_b / total_b) * 100) if total_b else 0
        disk_str = f"{used_gb:.1f} GB / {total_gb:.1f} GB ({disk_pct}% پر - {free_gb:.1f} GB خالی)"
    except Exception:
        pass

    sys_uptime_str = "نامشخص"
    try:
        with open("/proc/uptime", "r") as f:
            up_sec = float(f.read().split()[0])
            sys_uptime_str = format_uptime(up_sec)
    except Exception:
        pass

    bot_uptime_str = format_uptime(time.time() - BOT_START_TIME)
    py_ver = sys.version.split()[0]

    return (
        "🖥 <b>داشبورد مدیریت سرور (AWS Ubuntu)</b>\n\n"
        f"📡 <b>پینگ دیتاسنتر تلگرام:</b> <code>{ping_str}</code>\n"
        f"🧠 <b>مصرف رم (RAM):</b> <code>{ram_str}</code>\n"
        f"⚙️ <b>پردازنده (CPU):</b> <code>{cpu_str}</code>\n"
        f"💿 <b>فضای هارد (Disk):</b> <code>{disk_str}</code>\n\n"
        f"⏱ <b>آپتایم سرور لینوکس:</b> <b>{sys_uptime_str}</b>\n"
        f"🤖 <b>آپتایم ربات:</b> <b>{bot_uptime_str}</b>\n"
        f"🐍 <b>نسخه پایتون:</b> <code>Python {py_ver}</code>\n\n"
        "از دکمه‌های زیر برای مدیریت مستقیم سرور استفاده کنید:"
    )


def get_recent_service_logs(lines: int = 25) -> str:
    try:
        out = subprocess.check_output(
            ["journalctl", "-u", "tgbot", "-n", str(lines), "--no-pager"],
            stderr=subprocess.STDOUT,
            timeout=5,
        ).decode("utf-8", errors="replace")
        cleaned_lines = []
        for ln in out.splitlines():
            if "python[" in ln:
                ln = ln.split("python[", 1)[-1]
                if "]: " in ln:
                    ln = ln.split("]: ", 1)[-1]
            cleaned_lines.append(ln)
        res = "\n".join(cleaned_lines[-lines:])
        return html.escape(res[-3400:]) if res else "لاگی یافت نشد."
    except Exception as e:
        return html.escape(f"خطا در خواندن لاگ: {e}")


def clean_server_temp_files() -> str:
    removed_files = 0
    freed_bytes = 0
    try:
        for root_dir in ("/tmp", "/var/tmp"):
            if not os.path.exists(root_dir):
                continue
            for item in os.listdir(root_dir):
                full_p = os.path.join(root_dir, item)
                if item.startswith("tmp") or item.endswith(".tmp") or item.endswith(".part"):
                    try:
                        if os.path.isfile(full_p):
                            sz = os.path.getsize(full_p)
                            os.remove(full_p)
                            freed_bytes += sz
                            removed_files += 1
                    except Exception:
                        pass
        gc.collect()
        try:
            subprocess.run("sync && echo 3 > /proc/sys/vm/drop_caches", shell=True, timeout=3)
        except Exception:
            pass
    except Exception as e:
        return f"خطا در پاکسازی: {e}"

    freed_mb = freed_bytes / (1024 * 1024)
    return f"تعداد {removed_files} فایل موقت حذف شد ({freed_mb:.2f} مگابایت) و کش رم سرور آزاد گردید."


async def restart_bot_process():
    log("🔄 Restarting bot process...")
    try:
        if tg and tg.is_connected():
            await tg.disconnect()
    except Exception:
        pass
    await asyncio.sleep(0.4)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def fetch_latest_code_from_github() -> str:
    url = f"{GITHUB_RAW_URL}?t={int(time.time())}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read().decode("utf-8")


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
            InlineKeyboardButton("📊 وضعیت و دیدبان", callback_data="status"),
        ],
        [
            InlineKeyboardButton("⚙️ تنظیمات پیشرفته", callback_data="settings_menu"),
            InlineKeyboardButton("🖥 مدیریت سرور (AWS)", callback_data="server_menu"),
        ],
        [
            InlineKeyboardButton("🚪 پاکسازی اسپانسرها", callback_data="force_cleanup"),
            InlineKeyboardButton("🧹 ریست ضدتکرار", callback_data="clear_dups"),
        ],
        [
            InlineKeyboardButton("🔄 آپدیت خودکار ربات (GitHub)", callback_data="self_update"),
        ],
        [
            InlineKeyboardButton("🛑 توقف و خالی کردن صف", callback_data="stop"),
            InlineKeyboardButton("❓ راهنمای کامل", callback_data="help"),
        ],
    ])


def server_control_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 بروزرسانی آمار سرور", callback_data="server_menu"),
            InlineKeyboardButton("📜 مشاهده لاگ زنده", callback_data="server_logs"),
        ],
        [
            InlineKeyboardButton("🧹 پاکسازی کش و رم سرور", callback_data="server_clean"),
            InlineKeyboardButton("♻️ ری‌استارت سرویس ربات", callback_data="server_restart_bot"),
        ],
        [
            InlineKeyboardButton("🔄 آپدیت کد از گیت‌هاب", callback_data="self_update"),
            InlineKeyboardButton("🔥 ریبوت کل سرور (Ubuntu)", callback_data="server_reboot_confirm"),
        ],
        [
            InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="home"),
        ],
    ])


def settings_menu():
    btn_mode_text = (
        "🤔 پرسش هنگام چند دکمه"
        if settings["button_mode"] == "prompt"
        else "⚡ دانلود خودکار همه"
    )
    dup_text = "🟢 فعال" if settings.get("anti_duplicate", True) else "🔴 غیرفعال"
    leave_text = "🟢 فعال (حذف کامل)" if settings.get("auto_leave", True) else "🔴 غیرفعال"
    del_bot_text = "🟢 فعال (حذف دوطرفه)" if settings.get("auto_delete_bot", True) else "🔴 غیرفعال"
    vault_text = "🟢 فعال (Saved Messages)" if settings.get("safety_vault", True) else "🔴 غیرفعال"

    cap_map = {
        "none": "🚫 بدون کپشن",
        "clean": "🧹 پاکسازی تبلیغات",
        "replace": "🔄 جایگزینی هوشمند آیدی",
        "custom": "✍️ فقط کپشن اختصاصی",
        "original": "📋 حفظ کپشن اصلی",
    }
    cap_text = cap_map.get(settings.get("caption_mode", "none"), "🚫 بدون کپشن")

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
                f"💣 حذف خودکار چتِ ربات آپلودر: {del_bot_text}",
                callback_data="toggle:auto_delete_bot",
            )
        ],
        [
            InlineKeyboardButton(
                f"🛡 سپر نجات ۲۰ ثانیه‌ای: {vault_text}",
                callback_data="toggle:safety_vault",
            )
        ],
        [
            InlineKeyboardButton(
                f"♻️ سیستم ضد تکرار رسانه: {dup_text}",
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
                "✏️ تعیین آیدی / متن کپشن اختصاصی",
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


def live_processing_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👁 دیدبان زنده (مشاهده و کلیک دستی)", callback_data="inspect_live"),
        ],
        [
            InlineKeyboardButton("🛑 توقف فوری عملیات", callback_data="stop"),
        ],
    ])


# =========================================================
# LOAD DESTINATIONS & DIALOG SNAPSHOT
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


async def take_chats_snapshot() -> set:
    chat_ids = set()
    try:
        async for dialog in tg.iter_dialogs(limit=220):
            ent = dialog.entity
            if getattr(ent, "broadcast", False) or getattr(ent, "megagroup", False) or ent.__class__.__name__ == "Chat":
                chat_ids.add(ent.id)
    except Exception as e:
        log(f"⚠️ Snapshot error: {repr(e)}")
    return chat_ids


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
# UI VIEWS & REMOTE LIVE INSPECTOR
# =========================================================

async def show_home(update: Update):
    me = await tg.get_me()
    dest_title = html.escape(settings.get("destination_name") or "انتخاب نشده")
    state_icon = "⚡ در حال استخراج..." if processing else "🟢 آماده"

    text = (
        "🤖 <b>پنل پیشرفته استخراج و فوروارد هوشمند (V5 Ultra)</b>\n\n"
        f"👤 <b>اکانت متصل:</b> {html.escape(me.first_name or 'بدون نام')} (<code>{me.id}</code>)\n"
        f"📍 <b>مقصد فعلی:</b> <b>{dest_title}</b> (<code>{settings.get('destination_id') or '-'}</code>)\n"
        f"📡 <b>وضعیت:</b> {state_icon} | <b>صف:</b> {len(link_queue)} لینک\n"
        f"🧭 <b>آخرین مرحله:</b> <i>{html.escape(current_live_step)}</i>\n"
        f"🛡 <b>آرشیو ضدتکرار:</b> {len(settings['sent_media_ids'])} فایل\n\n"
        "💡 <i>لینک، پست دارای دکمه، یا بازه پست (مثل <code>t.me/ch/10-20</code>) بفرستید:</i>"
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
    custom_cap = html.escape(settings.get("custom_caption") or "تنظیم نشده")
    text = (
        "⚙️ <b>تنظیمات پیشرفته ربات</b>\n\n"
        "• <b>حذف خودکار اسپانسر:</b> خروج و پاکسازی تمام کانال‌ها و گروه‌های جوین اجباری.\n"
        "• <b>حذف چت ربات آپلودر:</b> پاک کردن دوطرفه پی‌وی رباتِ فایل‌دهنده پس از دریافت فایل.\n"
        "• <b>سپر نجات ۲۰ ثانیه‌ای:</b> بکاپ آنی فایل در Saved Messages برای جلوگیری از حذف شدن توسط تایمر ربات‌ها.\n"
        "• <b>حالت جایگزینی کپشن:</b> حفظ متن اصلی پست (رمز، توضیحات و...) و جایگزین کردن آیدی‌های تبلیغاتی با آیدی شما.\n"
        f"• <b>کپشن / آیدی اختصاصی فعلی:</b> <code>{custom_cap}</code>\n\n"
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
    dest_name = html.escape(settings.get("destination_name") or "انتخاب نشده")
    state = "⚡ در حال پردازش فعال" if processing else "🟢 آماده به کار"
    toast_line = f"\n💬 <b>آخرین پاپ‌آپ ربات مقصد:</b> <code>{html.escape(last_bot_toast)}</code>" if last_bot_toast else ""

    text = (
        "📊 <b>وضعیت زنده سیستم و دیدبان</b>\n\n"
        f"⚙️ <b>حالت فعلی:</b> {state}\n"
        f"🧭 <b>مرحله جاری:</b> <code>{html.escape(current_live_step)}</code>"
        f"{toast_line}\n"
        f"📍 <b>مقصد:</b> <b>{dest_name}</b> (<code>{settings.get('destination_id') or '-'}</code>)\n"
        f"📥 <b>لینک‌های در صف:</b> <b>{len(link_queue)}</b>\n"
        f"🚪 <b>اسپانسرهای در لیست حذف:</b> <b>{len(settings.get('pending_cleanup_ids', []))}</b>\n"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👁 دیدبان زنده (مشاهده پیام ربات مقصد)", callback_data="inspect_live"),
        ],
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


async def show_live_inspector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش آخرین پیام و دکمه‌های رباتِ مقصد برای کنترل و کلیک دستی از راه دور"""
    query = update.callback_query
    bot_ent = active_bot_session.get("bot_entity")
    uname = active_bot_session.get("username") or "ربات مقصد"

    if not bot_ent:
        await query.answer("❌ در حال حاضر هیچ ربات فعالی در دست بررسی نیست.", show_alert=True)
        return

    try:
        msgs = await asyncio.wait_for(tg.get_messages(bot_ent, limit=4), timeout=5.0)
        target_msg = None
        for m in msgs:
            if m and not m.out:
                target_msg = m
                break
        if not target_msg and msgs:
            target_msg = msgs[0]

        if not target_msg:
            await query.answer("هنوز پیامی از ربات مقصد دریافت نشده است.", show_alert=True)
            return

        active_bot_session["last_msg"] = target_msg
        msg_text = html.escape(target_msg.message or "[رسانه / بدون متن]")

        kb = []
        if getattr(target_msg, "buttons", None):
            for r_idx, row in enumerate(target_msg.buttons):
                kb_row = []
                for b_idx, btn in enumerate(row):
                    b_title = (getattr(btn, "text", "") or "دکمه").strip()
                    b_url = getattr(btn, "url", None)
                    if b_url:
                        kb_row.append(
                            InlineKeyboardButton(
                                f"🔗 جوین/بازکردن: {b_title[:22]}",
                                callback_data=f"rurl:{target_msg.id}:{r_idx}:{b_idx}",
                            )
                        )
                    else:
                        kb_row.append(
                            InlineKeyboardButton(
                                f"🔘 کلیک: {b_title[:24]}",
                                callback_data=f"rclick:{target_msg.id}:{r_idx}:{b_idx}",
                            )
                        )
                if kb_row:
                    kb.append(kb_row)

        kb.append([
            InlineKeyboardButton("🔄 رفرش پیام ربات", callback_data="inspect_live"),
            InlineKeyboardButton("❌ بستن دیدبان", callback_data="close_inspector"),
        ])

        inspect_text = (
            f"🕵️‍♂️ <b>دیدبان زنده ربات @{html.escape(uname)}</b>\n\n"
            f"📩 <b>آخرین پیام ربات:</b>\n"
            f"<blockquote>{msg_text[:1500]}</blockquote>\n\n"
            "💡 <i>با زدن هر کدام از دکمه‌های زیر، ربات شما مستقیماً همان دکمه را در ربات مقصد فشار می‌دهد یا در آن عضو می‌شود:</i>"
        )

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=inspect_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except Exception as e:
        await query.answer(f"خطا در دیدبان: {str(e)[:100]}", show_alert=True)


async def show_help(update: Update):
    text = (
        "❓ <b>راهنمای کامل قابلیت‌های V5 Ultra</b>\n\n"
        "1️⃣ <b>دیدبان زنده و کنترل دستی:</b> حین پردازش هر لینک، دکمه «👁 دیدبان زنده» را بزنید تا دقیقاً ببینید ربات مقصد چه پیامی داده و بتوانید از راه دور روی دکمه‌هایش کلیک کنید.\n\n"
        "2️⃣ <b>سپر نجات ۲۰ ثانیه‌ای:</b> فایل‌های دریافتی در ۰.۱ ثانیه اول در Saved Messages بکاپ می‌شوند تا تایمر حذف ۲۰ ثانیه‌ای ربات‌ها نتواند آن‌ها را پاک کند، سپس به مقصد ارسال و از Saved Messages پاک می‌شوند.\n\n"
        "3️⃣ <b>حذف خودکار چت ربات و اسپانسر:</b> علاوه بر خروج و حذف کانال‌ها و گروه‌های اسپانسر (عمومی، خصوصی و Join-Request)، چتِ خودِ ربات آپلودر هم در پایان کار حذف می‌شود.\n\n"
        "4️⃣ <b>استخراج بازه پست‌های کانال:</b> ارسال لینک به شکل <code>https://t.me/Channel/100-120</code> تمام پست‌های ۱۰۰ تا ۱۲۰ را استخراج می‌کند.\n\n"
        "5️⃣ <b>جایگزینی هوشمند کپشن:</b> در تنظیمات، حالت کپشن را روی «🔄 جایگزینی هوشمند آیدی» بگذارید تا متن اصلی پست حفظ شود ولی تمام آیدی‌ها و لینک‌های تبلیغاتی با آیدی شما جایگزین شوند."
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
                f"❌ خطا در ارسال کد:\n<code>{html.escape(str(e))}</code>\n\nدوباره شماره را بفرستید:",
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
            await msg.reply_text(f"❌ خطا در ورود:\n<code>{html.escape(str(e))}</code>", parse_mode="HTML")
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
                f"❌ رمز اشتباه است:\n<code>{html.escape(str(e))}</code>\n\nدوباره رمز را ارسال کنید:",
                parse_mode="HTML",
            )
        return


# =========================================================
# LINK & BATCH RANGE EXTRACTION
# =========================================================

def normalize_persian(text: str) -> str:
    if not text:
        return ""
    return (
        text.replace("ي", "ی")
        .replace("ك", "ک")
        .replace("ئ", "ی")
        .replace("أ", "ا")
        .replace("إ", "ا")
        .replace("ؤ", "و")
        .replace("\u200c", " ")
        .strip()
    )


def normalize_tg_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip().rstrip(".,!?،؛)]}>\"'")
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
        if not is_valid_tg_link(url):
            return

        # پشتیبانی از استخراج رگباری بازه پست‌ها (مثلاً https://t.me/channel/100-150)
        m_range = re.search(
            r"^(https?://(?:www\.)?(?:t\.me|telegram\.me)/(?:c/\d+|[A-Za-z0-9_]{4,}))/(\d+)\s*-\s*(\d+)$",
            url,
            re.IGNORECASE,
        )
        if m_range:
            base_prefix = m_range.group(1)
            start_id = int(m_range.group(2))
            end_id = int(m_range.group(3))
            if start_id > end_id:
                start_id, end_id = end_id, start_id
            # سقف ۱۰۰ پست در هر بازه برای حفظ سرعت
            end_id = min(end_id, start_id + 99)
            for pid in range(start_id, end_id + 1):
                single_url = f"{base_prefix}/{pid}"
                if single_url not in seen_urls:
                    seen_urls.add(single_url)
                    items.append({"title": f"پست #{pid}", "url": single_url})
            return

        if url not in seen_urls:
            seen_urls.add(url)
            items.append({"title": (title or "لینک").strip(), "url": url})

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

    if re.search(r"(?:t\.me|telegram\.me)/addlist/", link, re.IGNORECASE):
        return {"type": "folder", "url": link}

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
# MEDIA VALIDATION, ANTI-DUPLICATE & SMART CAPTION REPLACER
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
    """
    پشتیبانی از ۵ حالت کپشن شامل جایگزین‌کننده هوشمند آیدی/لینک
    """
    mode = settings.get("caption_mode", "none")
    custom_tag = (settings.get("custom_caption") or "").strip()

    if mode == "none":
        return None

    if mode == "original":
        return original_text or None

    if mode == "custom":
        return custom_tag or None

    if mode == "clean":
        if not original_text:
            return None
        cleaned = re.sub(r"https?://[^\s]+", "", original_text)
        cleaned = re.sub(r"(?:t\.me|telegram\.me)/[^\s]+", "", cleaned)
        cleaned = re.sub(r"@[A-Za-z0-9_]+", "", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned or None

    if mode == "replace":
        if not original_text:
            return custom_tag or None
        replacement = custom_tag if custom_tag else ""
        replaced = re.sub(r"https?://(?:www\.)?(?:t\.me|telegram\.me)/[^\s]+", replacement, original_text, flags=re.IGNORECASE)
        replaced = re.sub(r"(?:t\.me|telegram\.me)/[^\s]+", replacement, replaced, flags=re.IGNORECASE)
        replaced = re.sub(r"@[A-Za-z0-9_]+", replacement, replaced)
        replaced = re.sub(r"\n{3,}", "\n\n", replaced).strip()
        if custom_tag and custom_tag not in replaced:
            replaced = f"{replaced}\n\n{custom_tag}".strip()
        return replaced or custom_tag or None

    return None


# =========================================================
# 3-LAYER MEDIA SENDER + 20-SECOND SAFETY VAULT (SAVED MESSAGES)
# =========================================================

async def resolve_destination_target():
    global destination
    if settings.get("destination_type") == "saved":
        return "me"
    if destination is None:
        await restore_destination()
    if destination is not None:
        return destination
    dest_id = settings.get("destination_id")
    if dest_id:
        return dest_id
    return None


async def send_media_group_or_single(messages_list):
    """
    ۱. ذخیره فوری در سپر نجات (Saved Messages) در کمتر از ۰.۱ ثانیه برای دور زدن تایمر حذف ۲۰ ثانیه‌ای
    ۲. ارسال ۳ لایه تضمینی به مقصد (send_file -> forward drop_author -> download & upload)
    ۳. پاکسازی خودکار نسخه پشتیبان از Saved Messages پس از ارسال
    """
    target_dest = await resolve_destination_target()
    if target_dest is None:
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
    first_cap = next((c for c in captions if c), None)

    # ---------------------------------------------------------
    # مرحله ۱: سپر نجات ۲۰ ثانیه‌ای در Saved Messages
    # ---------------------------------------------------------
    vault_msgs = []
    is_dest_saved = (target_dest == "me" or settings.get("destination_type") == "saved")

    if settings.get("safety_vault", True) and not is_dest_saved:
        try:
            forwarded_vault = await asyncio.wait_for(
                tg.forward_messages("me", valid_messages),
                timeout=6.0,
            )
            if forwarded_vault:
                vault_msgs = forwarded_vault if isinstance(forwarded_vault, list) else [forwarded_vault]
                log(f"🛡 Vaulted {len(vault_msgs)} media item(s) to Saved Messages in 0.1s!")
        except Exception as ve:
            log(f"⚠️ Safety vault skip: {repr(ve)}")

    source_msgs = vault_msgs if len(vault_msgs) == len(valid_messages) else valid_messages
    media_files = [m.media for m in source_msgs]
    sent_ok = False
    sent_count = 0
    last_err = ""

    try:
        # ---------------------------------------------------------
        # لایه ۱: ارسال مستقیم با send_file (پشتیبانی کامل از کپشن دلخواه)
        # ---------------------------------------------------------
        try:
            if len(source_msgs) == 1:
                sent = await asyncio.wait_for(
                    tg.send_file(
                        entity=target_dest,
                        file=media_files[0],
                        caption=captions[0],
                        force_document=False,
                    ),
                    timeout=15.0,
                )
            else:
                sent = await asyncio.wait_for(
                    tg.send_file(
                        entity=target_dest,
                        file=media_files,
                        caption=first_cap,
                        force_document=False,
                    ),
                    timeout=20.0,
                )
            if sent:
                sent_ok = True
                sent_count = len(sent) if isinstance(sent, list) else 1
                log(f"⚡ Layer-1 (send_file) success: {sent_count} item(s)")
        except FloodWaitError as fe:
            if fe.seconds <= 12:
                await asyncio.sleep(fe.seconds + 0.5)
            raise fe
        except Exception as e1:
            last_err = str(e1)
            log(f"⚠️ Layer-1 send_file failed ({type(e1).__name__}: {e1}), trying Layer-2 forward...")

        # ---------------------------------------------------------
        # لایه ۲: فوروارد بدون نام فرستنده (drop_author=True) روی سرور تلگرام
        # ---------------------------------------------------------
        if not sent_ok:
            try:
                fwd = await asyncio.wait_for(
                    tg.forward_messages(
                        entity=target_dest,
                        messages=source_msgs,
                        drop_author=True,
                    ),
                    timeout=12.0,
                )
                if fwd:
                    sent_ok = True
                    sent_count = len(fwd) if isinstance(fwd, list) else 1
                    log(f"⚡ Layer-2 (forward drop_author) success: {sent_count} item(s)")
            except Exception as e2:
                last_err = str(e2)
                log(f"⚠️ Layer-2 forward failed ({type(e2).__name__}: {e2}), trying Layer-3 download/upload...")

        # ---------------------------------------------------------
        # لایه ۳: دانلود سریع در رم سرور AWS و آپلود مجدد (مخصوص کانال‌های قفل‌دار)
        # ---------------------------------------------------------
        if not sent_ok:
            for idx, m in enumerate(source_msgs):
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(delete=False, dir="/tmp") as tmp:
                        tmp_path = tmp.name
                    file_path = await asyncio.wait_for(tg.download_media(m, file=tmp_path), timeout=40.0)
                    if file_path:
                        await asyncio.wait_for(
                            tg.send_file(
                                entity=target_dest,
                                file=file_path,
                                caption=captions[idx],
                                force_document=False,
                            ),
                            timeout=40.0,
                        )
                        sent_count += 1
                except Exception as dl_err:
                    last_err = str(dl_err)
                    log(f"❌ Layer-3 fallback failed: {repr(dl_err)}")
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass
            if sent_count > 0:
                sent_ok = True
                log(f"⚡ Layer-3 (re-upload) success: {sent_count} item(s)")

        if sent_ok:
            for orig_m in valid_messages:
                mark_media_sent(orig_m)
            return {"success": True, "count": sent_count, "skipped": skipped_dups, "error": None}

        return {"success": False, "count": 0, "skipped": skipped_dups, "error": last_err or "ارسال ناموفق"}

    finally:
        # پاک کردن نسخه پشتیبان موقت از Saved Messages
        if vault_msgs and not is_dest_saved:
            try:
                v_ids = [vm.id for vm in vault_msgs if getattr(vm, "id", None)]
                if v_ids:
                    await tg.delete_messages("me", v_ids)
            except Exception:
                pass


# =========================================================
# SPONSOR JOIN, CHANNELS-TOO-MUCH RECOVERY & FULL BOT/CHANNEL CLEANUP
# =========================================================

VERIFY_KEYWORDS = [
    "تایید", "تأیید", "تائید", "بررسی", "عضو شدم", "عضوشدم", "جوین شدم",
    "جوینشدم", "دریافت", "دانلود", "ادامه", "بعدی", "بخش", "قسمت", "کیفیت",
    "فایل", "پارت", "مشاهده", "کلیک", "باز کردن", "ورود", "عضویت", "چک",
    "ارسال", "پخش", "نمایش",
    "check", "verify", "joined", "continue", "download", "get",
    "start", "try again", "unlock", "confirm", "✅", "🔄", "📥", "📂", "🎬", "☑️", "✔️",
]

SELF_DESTRUCT_WORDS = [
    "حذف", "پاک", "ذخیره", "سیو", "فوروارد", "منقضی", "انقضا",
    "پیام های ذخیره", "سریع", "ثانیه دیگر", "ثانيه ديگر",
    "delete", "remove", "save", "forward", "expire", "destruct",
]

FORCE_JOIN_HINTS = [
    "عضو", "جوین", "کانال", "گروه", "اسپانسر", "قفل", "ابتدا", "حمایت",
    "join", "channel", "sponsor", "subscribe", "first",
]


def detect_wait_timer(text: str) -> int:
    if not text:
        return 0
    low = normalize_persian(text).lower()
    if any(w in low for w in SELF_DESTRUCT_WORDS):
        return 0

    if "صبر" in low or "منتظر" in low or "wait" in low:
        m = re.search(
            r"(\d+)\s*(?:ثانیه|ثانيه|second|seconds|sec|s)\b",
            low,
        )
        if m:
            sec = int(m.group(1))
            if 1 <= sec <= 10:
                return sec
    return 0


def register_sponsor_for_cleanup(entity):
    if not entity:
        return
    cid = getattr(entity, "id", None)
    if not cid:
        return
    if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
        full_id = int(f"-100{cid}") if not str(cid).startswith("-100") else cid
    elif entity.__class__.__name__ == "Chat":
        full_id = -abs(cid)
    else:
        full_id = cid

    if full_id not in settings["pending_cleanup_ids"]:
        settings["pending_cleanup_ids"].append(full_id)
        save_settings()


async def emergency_free_channel_slots(joined_entities: dict):
    """آزادسازی فوری ظرفیت کانال‌های اکانت در صورت بروز خطای پر شدن سقف ۵۰۰ کانال"""
    log("🚦 ChannelsTooMuchError detected! Running emergency channel slot cleanup...")
    await cleanup_joined_channels(joined_entities)


async def _do_join_single_sponsor(url: str, joined_entities: dict, visited_bots: dict) -> dict:
    parsed = parse_tg_link(url)
    if not parsed or parsed["type"] == "folder":
        return {"joined": True, "is_request": False}

    for attempt in range(2):
        try:
            if parsed["type"] == "invite":
                inv_hash = parsed["hash"]
                try:
                    chk = await tg(CheckChatInviteRequest(inv_hash))
                    if hasattr(chk, "chat") and chk.chat:
                        joined_entities[chk.chat.id] = chk.chat
                        register_sponsor_for_cleanup(chk.chat)
                        if chk.__class__.__name__ == "ChatInviteAlready":
                            return {"joined": True, "is_request": False}
                except Exception:
                    pass

                updates = await tg(ImportChatInviteRequest(inv_hash))
                if hasattr(updates, "chats") and updates.chats:
                    for ch in updates.chats:
                        joined_entities[ch.id] = ch
                        register_sponsor_for_cleanup(ch)
                log(f"✅ Joined private sponsor: {inv_hash}")
                return {"joined": True, "is_request": False}

            if parsed["type"] in ("bot_or_entity", "public_post"):
                username = parsed["username"]
                entity = await tg.get_entity(username)

                if getattr(entity, "bot", False):
                    payload = parsed.get("payload")
                    cmd = f"/start {payload}" if payload else "/start"
                    await tg.send_message(entity, cmd)
                    visited_bots[entity.id] = entity
                    log(f"🤖 Started sponsor bot: @{username}")
                    return {"joined": True, "is_request": False}

                await tg(JoinChannelRequest(entity))
                joined_entities[entity.id] = entity
                register_sponsor_for_cleanup(entity)
                log(f"✅ Joined public sponsor: @{username}")
                return {"joined": True, "is_request": False}

        except UserAlreadyParticipantError:
            return {"joined": True, "is_request": False}

        except InviteRequestSentError:
            log(f"📨 Join-Request sent for sponsor: {url}")
            return {"joined": True, "is_request": True}

        except ChannelsTooMuchError:
            if attempt == 0:
                await emergency_free_channel_slots(joined_entities)
                continue
            return {"joined": True, "is_request": False}

        except FloodWaitError as e:
            log(f"⏳ FloodWait on Join ({url}): {e.seconds}s")
            if e.seconds <= 8:
                await asyncio.sleep(e.seconds + 0.5)
            return {"joined": True, "is_request": False}

        except Exception as e:
            log(f"⚠️ Could not join sponsor {url}: {repr(e)}")
            return {"joined": True, "is_request": False}

    return {"joined": True, "is_request": False}


async def join_single_sponsor(url: str, joined_entities: dict, visited_bots: dict) -> dict:
    try:
        return await asyncio.wait_for(
            _do_join_single_sponsor(url, joined_entities, visited_bots),
            timeout=7.0,
        )
    except asyncio.TimeoutError:
        log(f"⏱ Timeout while joining sponsor: {url} (skipping)")
        return {"joined": True, "is_request": False}
    except Exception:
        return {"joined": True, "is_request": False}


async def cleanup_joined_channels(
    joined_entities: dict,
    visited_bots: dict = None,
    pre_snapshot_ids: set = None,
    step_cb=None,
) -> int:
    """
    ۱. خروج و حذف کامل تمام کانال‌ها و گروه‌های اسپانسر (عمومی، خصوصی، Join-Request)
    ۲. حذف دوطرفه تاریخچه چت با تمام ربات‌های آپلودر/اسپانسر (در صورت فعال بودن auto_delete_bot)
    """
    left_count = 0
    dest_id = settings.get("destination_id")
    dest_raw_id = int(str(dest_id).replace("-100", "").lstrip("-")) if dest_id else None

    if settings.get("auto_leave", True):
        if pre_snapshot_ids is not None:
            try:
                async for dialog in tg.iter_dialogs(limit=100):
                    ent = dialog.entity
                    if getattr(ent, "broadcast", False) or getattr(ent, "megagroup", False) or ent.__class__.__name__ == "Chat":
                        if ent.id not in pre_snapshot_ids and ent.id != dest_raw_id:
                            joined_entities[ent.id] = ent
                            log(f"🔍 Snapshot caught auto-approved sponsor chat: {dialog.name} ({ent.id})")
            except Exception as e:
                log(f"⚠️ Post-snapshot check error: {repr(e)}")

        targets = list(joined_entities.values())
        seen_raw_ids = {getattr(e, "id", None) for e in targets if getattr(e, "id", None)}

        for saved_id in list(settings.get("pending_cleanup_ids", [])):
            raw_id = int(str(saved_id).replace("-100", "").lstrip("-"))
            if raw_id not in seen_raw_ids and raw_id != dest_raw_id:
                targets.append(saved_id)

        if targets:
            if step_cb:
                await step_cb(f"🧹 خروج و حذف فوری {len(targets)} کانال/گروه اسپانسر...")
            for item in targets:
                try:
                    await asyncio.wait_for(tg.delete_dialog(item), timeout=5.0)
                    left_count += 1
                    log(f"🚪 Deleted & left sponsor dialog: {getattr(item, 'title', item)}")
                except Exception:
                    try:
                        await asyncio.wait_for(tg(LeaveChannelRequest(item)), timeout=5.0)
                        left_count += 1
                    except Exception:
                        pass
                await asyncio.sleep(0.15)

        settings["pending_cleanup_ids"] = []
        save_settings()
        joined_entities.clear()

    # حذف کامل پی‌ویِ ربات‌های آپلودر بعد از اتمام کار
    if settings.get("auto_delete_bot", True) and visited_bots:
        if step_cb:
            await step_cb(f"💣 پاکسازی خودکار چت با {len(visited_bots)} ربات آپلودر...")
        for bot_ent in list(visited_bots.values()):
            try:
                await asyncio.wait_for(tg.delete_dialog(bot_ent, revoke=True), timeout=5.0)
                log(f"💣 Deleted bot dialog: @{getattr(bot_ent, 'username', bot_ent.id)}")
            except Exception as be:
                log(f"⚠️ Could not delete bot dialog: {repr(be)}")
            await asyncio.sleep(0.15)
        visited_bots.clear()

    return left_count


# =========================================================
# SMART BOT INTERACTION, POPUP TOAST CAPTURE & MULTI-BUTTON CLICKER
# =========================================================

async def inspect_and_interact_with_bot_message(
    msg,
    bot_entity,
    current_bot_username: str,
    current_payload: str,
    joined_entities: dict,
    visited_bots: dict,
    clicked_buttons: set,
    step_cb=None,
):
    global last_bot_toast
    active_bot_session["bot_entity"] = bot_entity
    active_bot_session["username"] = current_bot_username
    active_bot_session["last_msg"] = msg

    text = msg.message or ""
    norm_text_low = normalize_persian(text).lower()
    is_force_join_msg = any(h in norm_text_low for h in FORCE_JOIN_HINTS)

    wait_sec = detect_wait_timer(text)
    if wait_sec > 0:
        if step_cb:
            await step_cb(f"⏱ انتظار کوتاه برای تایمر ربات ({wait_sec} ثانیه)...")
        await asyncio.sleep(wait_sec + 0.3)

    next_bot_links = []
    sponsor_urls_to_join = []
    re_start_url = None
    re_start_payload = current_payload

    has_callback_button = False
    if getattr(msg, "buttons", None):
        for row in msg.buttons:
            for btn in row:
                if not getattr(btn, "url", None):
                    has_callback_button = True
                    break

    button_urls = []
    if getattr(msg, "buttons", None):
        for row in msg.buttons:
            for btn in row:
                b_url = getattr(btn, "url", None)
                if b_url:
                    button_urls.append((getattr(btn, "text", "") or "", normalize_tg_url(b_url)))

    for ent in getattr(msg, "entities", None) or []:
        ent_url = getattr(ent, "url", None)
        if ent_url:
            button_urls.append(("لینک متن", normalize_tg_url(ent_url)))

    for match in re.finditer(
        r"(?:https?://(?:www\.)?(?:t\.me|telegram\.me)/[^\s]+|tg://(?:resolve|join)\?[^\s]+)",
        text,
        re.IGNORECASE,
    ):
        u = normalize_tg_url(match.group(0))
        if all(u != existing_url for _, existing_url in button_urls):
            button_urls.append(("لینک متنی", u))

    for btn_label, b_url in button_urls:
        parsed = parse_tg_link(b_url)
        if not parsed or parsed["type"] == "folder":
            continue

        norm_label = normalize_persian(btn_label).lower()

        if parsed["type"] == "invite":
            if b_url not in sponsor_urls_to_join:
                sponsor_urls_to_join.append(b_url)
            continue

        if (
            parsed["type"] == "bot_or_entity"
            and parsed["username"].lower() == current_bot_username.lower()
        ):
            re_start_url = b_url
            if parsed.get("payload"):
                re_start_payload = parsed["payload"]
            if parsed.get("payload") and parsed["payload"] != current_payload:
                if b_url not in next_bot_links:
                    next_bot_links.append(b_url)
            continue

        if parsed["type"] == "bot_or_entity" and parsed["username"].lower().endswith("bot"):
            if (is_force_join_msg or has_callback_button or re_start_url) and any(
                w in norm_label for w in ("اسپانسر", "کانال", "جوین", "عضو", "ربات", "اول", "دوم", "سوم", "چهارم", "sponsor", "join", "bot")
            ):
                if b_url not in sponsor_urls_to_join:
                    sponsor_urls_to_join.append(b_url)
            if b_url not in next_bot_links:
                next_bot_links.append(b_url)
            continue

        if parsed["type"] in ("bot_or_entity", "public_post"):
            if b_url not in sponsor_urls_to_join:
                sponsor_urls_to_join.append(b_url)

    # ۱. عضویت در تمام اسپانسرها
    joined_any = False
    had_join_request = False
    total_sp = len(sponsor_urls_to_join)

    if total_sp > 0:
        for idx, sp_url in enumerate(sponsor_urls_to_join, start=1):
            if step_cb:
                await step_cb(f"🔗 عضویت در اسپانسر {idx} از {total_sp}...")
            res = await join_single_sponsor(sp_url, joined_entities, visited_bots)
            if res.get("joined"):
                joined_any = True
            if res.get("is_request"):
                had_join_request = True
            await asyncio.sleep(0.25)

        if had_join_request:
            if step_cb:
                await step_cb("⏳ انتظار ۱.۲ ثانیه‌ای برای تایید خودکار درخواست عضویت...")
            await asyncio.sleep(1.2)
        else:
            await asyncio.sleep(0.4)

    # ۲. کلیک روی دکمه تایید عضویت / دریافت فایل (و شکار متن پاپ‌آپ Alert!)
    clicked_any_confirm = False

    if getattr(msg, "buttons", None):
        matching_callbacks = []
        fallback_callback = None
        edit_stamp = str(getattr(msg, "edit_date", "") or "")

        for r_idx, row in enumerate(msg.buttons):
            for b_idx, btn in enumerate(row):
                if getattr(btn, "url", None):
                    continue

                btn_title = (getattr(btn, "text", "") or "").strip()
                if any(bad in btn_title.lower() for bad in ("لغو", "انصراف", "بستن", "cancel", "close")):
                    continue

                btn_key = (msg.id, edit_stamp, r_idx, b_idx, btn_title)
                if btn_key in clicked_buttons:
                    continue

                btn_low = normalize_persian(btn_title).lower()
                if any(normalize_persian(kw).lower() in btn_low for kw in VERIFY_KEYWORDS):
                    matching_callbacks.append((r_idx, b_idx, btn_title, btn_key))
                elif fallback_callback is None:
                    fallback_callback = (r_idx, b_idx, btn_title, btn_key)

        to_click_list = matching_callbacks[:2] if matching_callbacks else ([fallback_callback] if fallback_callback else [])

        for r_idx, b_idx, b_title, btn_key in to_click_list:
            clicked_buttons.add(btn_key)
            clicked_any_confirm = True
            try:
                if step_cb:
                    await step_cb(f"🔘 کلیک خودکار روی دکمه «{b_title}»...")
                log(f"🔘 Clicking button [{b_title}] on msg {msg.id}...")
                click_res = await asyncio.wait_for(msg.click(r_idx, b_idx), timeout=6.5)

                # شکار پیام پاپ‌آپ (Toast / Alert) از ربات آپلودر!
                toast_msg = getattr(click_res, "message", None)
                if toast_msg:
                    last_bot_toast = toast_msg.strip()
                    log(f"💬 Bot Popup Toast: {last_bot_toast}")
                    if step_cb:
                        await step_cb(f"💬 پاسخ ربات: {last_bot_toast[:55]}", force=True)

                res_url = getattr(click_res, "url", None)
                if res_url:
                    norm_res_url = normalize_tg_url(res_url)
                    if norm_res_url and norm_res_url not in next_bot_links:
                        log(f"🔀 Callback redirect URL: {norm_res_url}")
                        next_bot_links.insert(0, norm_res_url)

                await asyncio.sleep(0.75)
            except asyncio.TimeoutError:
                log(f"⏱ Timeout on msg.click([{b_title}]), continuing...")
            except Exception as e:
                log(f"⚠️ Button click error: {repr(e)}")

    # ۳. اگر دکمه «عضو شدم» از نوع لینک به خودِ ربات بود (Restart URL) یا بعد از عضویت نیاز به استارت مجدد داشت:
    if (re_start_url and (joined_any or not clicked_any_confirm)) or (joined_any and not clicked_any_confirm):
        restart_key = (msg.id, "auto_restart_cmd", re_start_payload)
        if restart_key not in clicked_buttons:
            clicked_buttons.add(restart_key)
            cmd = f"/start {re_start_payload}" if re_start_payload else "/start"
            if step_cb:
                await step_cb(f"🔄 تایید عضویت و ارسال مجدد دستور دریافت به @{current_bot_username}...")
            try:
                log(f"🔄 Re-sending {cmd} to @{current_bot_username} after joining sponsors!")
                await asyncio.wait_for(tg.send_message(bot_entity, cmd), timeout=6.0)
                await asyncio.sleep(0.8)
            except Exception as e:
                log(f"⚠️ Re-start send error: {repr(e)}")

    return next_bot_links


# =========================================================
# PROCESS DIRECT CHANNEL POSTS & BOT LINKS
# =========================================================

async def process_channel_post(parsed_info, step_cb=None):
    try:
        if step_cb:
            await step_cb("📥 دریافت مستقیم پست از کانال...")

        target = (
            parsed_info["chat_id"]
            if parsed_info["type"] == "private_post"
            else parsed_info["username"]
        )
        entity = await asyncio.wait_for(tg.get_entity(target), timeout=8.0)
        msg_id = parsed_info["msg_id"]
        msg = await asyncio.wait_for(tg.get_messages(entity, ids=msg_id), timeout=8.0)

        if not msg:
            return {"success": False, "count": 0, "skipped": 0, "reason": "پست یافت نشد."}

        # اگر پست کانال خودش رسانه نداشت ولی دکمه دانلود ربات داشت، دکمه‌اش را استخراج و اجرا کن!
        if not is_real_media(msg) and getattr(msg, "buttons", None):
            for row in msg.buttons:
                for btn in row:
                    b_url = getattr(btn, "url", None)
                    if b_url and is_valid_tg_link(normalize_tg_url(b_url)):
                        return await process_one_link(normalize_tg_url(b_url), step_cb=step_cb)

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
    visited_bots: dict = None,
    visited_links: set = None,
    pre_snapshot_ids: set = None,
    step_cb=None,
):
    global last_bot_toast
    if joined_entities is None:
        joined_entities = {}
    if visited_bots is None:
        visited_bots = {}
    if visited_links is None:
        visited_links = set()

    if hop_depth == 0:
        last_bot_toast = ""
        if pre_snapshot_ids is None and settings.get("auto_leave", True):
            pre_snapshot_ids = await take_chats_snapshot()

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
            await step_cb(f"📡 اتصال سریع به @{username} (مرحله {hop_depth + 1})...")
        bot = await asyncio.wait_for(tg.get_entity(username), timeout=8.0)
    except Exception as e:
        return {"success": False, "count": 0, "skipped": 0, "reason": str(e)}

    if not getattr(bot, "bot", False):
        return {
            "success": False,
            "count": 0,
            "skipped": 0,
            "reason": f"@{username} یک ربات نیست.",
        }

    visited_bots[bot.id] = bot
    active_bot_session["bot_entity"] = bot
    active_bot_session["username"] = username

    try:
        command = f"/start {payload}" if payload else "/start"
        if step_cb:
            await step_cb(f"📨 ارسال دستور شروع به @{username}...")
        request = await asyncio.wait_for(tg.send_message(bot, command), timeout=8.0)
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
    last_send_error = None
    last_activity_time = start_time
    fallback_restarted = False

    try:
        while not stop_requested:
            now = asyncio.get_running_loop().time()
            elapsed = int(now - start_time)
            if elapsed >= WAIT_SECONDS:
                break

            try:
                messages = await asyncio.wait_for(
                    tg.get_messages(bot, limit=25, min_id=min_id),
                    timeout=6.0,
                )
            except Exception as e:
                log(f"⚠️ Read error: {repr(e)}")
                await asyncio.sleep(0.4)
                continue

            messages = [
                m for m in reversed(messages)
                if m and m.id > request.id and not m.out
            ]

            if messages:
                active_bot_session["last_msg"] = messages[-1]

            # اولویت ۱: شکار آنی رسانه‌ها + سپر نجات ۲۰ ثانیه‌ای + ارسال ۳ لایه به مقصد
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
                await asyncio.sleep(0.35)
                media_batches.append(current_group_list)

            for batch in media_batches:
                if step_cb:
                    await step_cb(f"⚡ رسانه شکار شد! انتقال امن به مقصد ({settings.get('destination_name')})...", force=True)
                res = await send_media_group_or_single(batch)
                if res["success"]:
                    media_count += res["count"]
                    skipped_dups += res.get("skipped", 0)
                    last_activity_time = asyncio.get_running_loop().time()
                else:
                    skipped_dups += res.get("skipped", 0)
                    last_send_error = res.get("error")
                    log(f"❌ Media batch error: {last_send_error}")
                    if step_cb:
                        await step_cb(f"⚠️ خطا در ارسال به مقصد: {last_send_error}", force=True)

            # اولویت ۲: بررسی پیام‌های جوین اجباری و کلیک دکمه‌ها
            for message in messages:
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
                        bot,
                        username,
                        payload,
                        joined_entities,
                        visited_bots,
                        clicked_buttons,
                        step_cb=step_cb,
                    )
                    last_activity_time = asyncio.get_running_loop().time()
                    for h in hops:
                        if h not in discovered_bot_hops and normalize_tg_url(h) not in visited_links:
                            discovered_bot_hops.append(h)

            # اولویت ۳: اگر جوین شدیم و دکمه را زدیم ولی بعد از ۳.۵ ثانیه فایلی نیامد، دوباره /start <payload> بفرست
            if (
                media_count == 0
                and not fallback_restarted
                and (joined_entities or clicked_buttons)
                and (asyncio.get_running_loop().time() - last_activity_time) >= 3.5
            ):
                fallback_restarted = True
                cmd = f"/start {payload}" if payload else "/start"
                if step_cb:
                    await step_cb(f"🔄 ارسال مجدد دستور دریافت فایل به @{username}...")
                try:
                    await asyncio.wait_for(tg.send_message(bot, cmd), timeout=5.0)
                    last_activity_time = asyncio.get_running_loop().time()
                except Exception:
                    pass

            # اولویت ۴: پرش هوشمند به ربات بعدی
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
                    visited_bots=visited_bots,
                    visited_links=visited_links,
                    pre_snapshot_ids=pre_snapshot_ids,
                    step_cb=step_cb,
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

            if media_count == 0 and step_cb and elapsed > 2:
                toast_hint = f" | 💬 {last_bot_toast[:30]}" if last_bot_toast else ""
                await step_cb(f"⏳ منتظر دریافت فایل از @{username} ({elapsed}s){toast_hint}...")

            await asyncio.sleep(POLL_INTERVAL)

    finally:
        if hop_depth == 0:
            await cleanup_joined_channels(
                joined_entities,
                visited_bots=visited_bots,
                pre_snapshot_ids=pre_snapshot_ids,
                step_cb=step_cb,
            )
            active_bot_session["bot_entity"] = None
            active_bot_session["username"] = None
            active_bot_session["last_msg"] = None

    if media_count > 0 or skipped_dups > 0:
        return {
            "success": True,
            "count": media_count,
            "skipped": skipped_dups,
            "reason": None,
        }

    fail_reason = last_send_error or (f"فایلی دریافت نشد ({last_bot_toast})" if last_bot_toast else "فایلی دریافت نشد.")
    return {
        "success": False,
        "count": 0,
        "skipped": 0,
        "reason": fail_reason,
    }


# =========================================================
# LIVE QUEUE RUNNER
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
            f"📍 مقصد: <b>{html.escape(settings.get('destination_name') or '')}</b>"
        ),
        parse_mode="HTML",
        reply_markup=live_processing_keyboard(),
    )

    last_edit_time = 0.0
    last_step_str = ""

    try:
        while link_queue and not stop_requested:
            item = link_queue.pop(0)
            total_processed += 1
            link = item["url"]
            title = html.escape(item.get("title") or "لینک")

            async def update_live_step(step_text: str, force: bool = False):
                global current_live_step
                nonlocal last_edit_time, last_step_str
                current_live_step = step_text
                now = time.monotonic()
                if not force and (step_text == last_step_str or (now - last_edit_time) < 0.75):
                    return
                last_edit_time = now
                last_step_str = step_text
                toast_row = f"💬 <b>پیام پاپ‌آپ ربات:</b> <code>{html.escape(last_bot_toast)}</code>\n" if last_bot_toast else ""
                try:
                    await status_message.edit_text(
                        "⚡ <b>در حال استخراج و ارسال هوشمند...</b>\n\n"
                        f"🏷 <b>عنوان:</b> {title}\n"
                        f"🧭 <b>مرحله فعلی:</b> <b>{html.escape(step_text)}</b>\n"
                        f"{toast_row}\n"
                        f"📊 <b>پیشرفت:</b> لینک {total_processed} (صف انتظار: {len(link_queue)})\n"
                        f"📦 <b>ارسال‌شده به مقصد:</b> <b>{media_total}</b> رسانه\n"
                        f"🛡 <b>تکراریِ ردشده:</b> {skipped_total}\n\n"
                        f"🔗 <code>{html.escape(link)}</code>",
                        parse_mode="HTML",
                        reply_markup=live_processing_keyboard(),
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

            await asyncio.sleep(0.3)

        current_live_step = "آماده به کار"
        header = "🛑 عملیات متوقف شد" if stop_requested else "🏁 عملیات با موفقیت کامل شد"
        if stop_requested:
            link_queue.clear()

        await status_message.edit_text(
            f"<b>{header}</b>\n\n"
            f"🔗 <b>کل لینک‌های پردازش‌شده:</b> {total_processed}\n"
            f"✅ <b>موفق:</b> {success_links} | ❌ <b>ناموفق:</b> {failed_links}\n"
            f"📦 <b>مجموع رسانه‌های ارسال‌شده به مقصد:</b> <b>{media_total}</b>\n"
            f"🛡 <b>فایل‌های تکراری ردشده:</b> {skipped_total}\n"
            f"🧹 <b>پاکسازی اسپانسرها و چت ربات:</b> انجام شد",
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
    global destination, stop_requested, waiting_for_custom_caption, last_bot_toast

    if not is_owner(update):
        return

    query = update.callback_query

    if not await tg.is_user_authorized():
        await query.answer()
        await prompt_login(update)
        return

    data = query.data

    # -----------------------------------------------------
    # دیدبان زنده و کلیک دستی از راه دور روی دکمه‌های ربات مقصد
    # -----------------------------------------------------
    if data == "inspect_live":
        await show_live_inspector(update, context)
        return

    if data == "close_inspector":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if data.startswith("rclick:"):
        _, msg_id_s, r_s, b_s = data.split(":")
        r_idx, b_idx = int(r_s), int(b_s)
        bot_ent = active_bot_session.get("bot_entity")
        target_msg = active_bot_session.get("last_msg")
        if not bot_ent or not target_msg:
            await query.answer("❌ جلسه ربات مقصد به پایان رسیده است.", show_alert=True)
            return
        try:
            res = await asyncio.wait_for(target_msg.click(r_idx, b_idx), timeout=6.0)
            t_msg = getattr(res, "message", None) or "کلیک با موفقیت انجام شد!"
            last_bot_toast = t_msg
            await query.answer(f"💬 پاسخ ربات: {t_msg[:150]}", show_alert=True)
        except Exception as e:
            await query.answer(f"خطا در کلیک: {str(e)[:100]}", show_alert=True)
        return

    if data.startswith("rurl:"):
        _, msg_id_s, r_s, b_s = data.split(":")
        r_idx, b_idx = int(r_s), int(b_s)
        target_msg = active_bot_session.get("last_msg")
        if not target_msg or not getattr(target_msg, "buttons", None):
            await query.answer("❌ دکمه یافت نشد.", show_alert=True)
            return
        try:
            btn = target_msg.buttons[r_idx][b_idx]
            b_url = normalize_tg_url(getattr(btn, "url", ""))
            await join_single_sponsor(b_url, {}, {})
            await query.answer(f"✅ عضویت/استارت لینک انجام شد:\n{b_url[:100]}", show_alert=True)
        except Exception as e:
            await query.answer(f"خطا: {str(e)[:100]}", show_alert=True)
        return

    await query.answer()

    # -----------------------------------------------------
    # پنل مدیریت سرور (AWS Ubuntu)
    # -----------------------------------------------------
    if data == "server_menu":
        stats_text = await get_server_stats_text()
        await query.edit_message_text(
            stats_text,
            parse_mode="HTML",
            reply_markup=server_control_menu(),
        )
        return

    if data == "server_logs":
        logs_str = await asyncio.to_thread(get_recent_service_logs, 25)
        await query.edit_message_text(
            f"📜 <b>آخرین لاگ‌های زنده سرور (tgbot.service):</b>\n\n<pre>{logs_str}</pre>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 رفرش لاگ", callback_data="server_logs"),
                    InlineKeyboardButton("🔙 پنل سرور", callback_data="server_menu"),
                ]
            ]),
        )
        return

    if data == "server_clean":
        res_msg = await asyncio.to_thread(clean_server_temp_files)
        stats_text = await get_server_stats_text()
        await query.edit_message_text(
            f"✅ <b>{html.escape(res_msg)}</b>\n\n{stats_text}",
            parse_mode="HTML",
            reply_markup=server_control_menu(),
        )
        return

    if data == "server_restart_bot":
        await query.edit_message_text(
            "♻️ <b>در حال ری‌استارت سرویس ربات...</b>\nچند ثانیه دیگر /start بزنید.",
            parse_mode="HTML",
        )
        asyncio.create_task(restart_bot_process())
        return

    if data == "server_reboot_confirm":
        await query.edit_message_text(
            "⚠️ <b>آیا از ریبوت کامل سرور اوبونتو (AWS) مطمئن هستید؟</b>\n\n"
            "با ریبوت شدن سرور، ارتباط حدود ۳۰ ثانیه قطع می‌شود و پس از بالا آمدن سرور، ربات به صورت خودکار روشن خواهد شد.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ بله، سرور ریبوت شود", callback_data="server_reboot_do"),
                    InlineKeyboardButton("❌ انصراف", callback_data="server_menu"),
                ]
            ]),
        )
        return

    if data == "server_reboot_do":
        await query.edit_message_text(
            "🔥 <b>دستور ریبوت سرور صادر شد!</b>\nسرور تا ۳۰ ثانیه دیگر مجدداً بالا می‌آید و ربات خودکار فعال می‌شود.",
            parse_mode="HTML",
        )
        subprocess.Popen(["sudo", "reboot"])
        return

    # -----------------------------------------------------
    # دکمه آپدیت خودکار از مخزن گیت‌هاب
    # -----------------------------------------------------
    if data == "self_update":
        await query.edit_message_text(
            "🔄 <b>در حال دریافت آخرین نسخه <code>main.py</code> از گیت‌هاب...</b>",
            parse_mode="HTML",
        )
        try:
            new_code = await asyncio.to_thread(fetch_latest_code_from_github)
            if "TelegramClient" not in new_code or "BOT_TOKEN" not in new_code:
                raise ValueError("فایل دانلود شده معتبر نیست.")

            target_file = os.path.abspath(__file__)
            with open(target_file, "w", encoding="utf-8") as f:
                f.write(new_code)

            await query.edit_message_text(
                "✅ <b>آپدیت با موفقیت از گیت‌هاب دریافت و نصب شد!</b>\n"
                "♻️ ربات در حال ری‌استارت خودکار است (چند ثانیه دیگر /start بزنید)...",
                parse_mode="HTML",
            )
            asyncio.create_task(restart_bot_process())
        except Exception as e:
            await query.edit_message_text(
                f"❌ <b>خطا در بروزرسانی خودکار:</b>\n<code>{html.escape(str(e))}</code>",
                parse_mode="HTML",
                reply_markup=main_menu(),
            )
        return

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
            settings["anti_duplicate"] = not settings.get("anti_duplicate", True)
        elif key == "auto_leave":
            settings["auto_leave"] = not settings.get("auto_leave", True)
        elif key == "auto_delete_bot":
            settings["auto_delete_bot"] = not settings.get("auto_delete_bot", True)
        elif key == "safety_vault":
            settings["safety_vault"] = not settings.get("safety_vault", True)
        elif key == "caption_mode":
            modes = ["none", "clean", "replace", "custom", "original"]
            curr = settings.get("caption_mode", "none")
            if curr not in modes:
                curr = "none"
            nxt = modes[(modes.index(curr) + 1) % len(modes)]
            settings["caption_mode"] = nxt
        save_settings()
        await show_settings(update)
        return

    if data == "set_custom_caption":
        waiting_for_custom_caption = True
        await query.edit_message_text(
            "✍️ <b>تنظیم آیدی یا متن کپشن اختصاصی</b>\n\n"
            "متن یا آیدی کانال خود (مثلاً <code>@MyChannel</code>) را بفرستید تا در حالت «جایگزینی هوشمند» یا «کپشن اختصاصی» استفاده شود:",
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
        destination = "me" if dtype == "saved" else dialog.entity
        # ذخیره dialog.id که دارای پیشوند دقیق -100 برای کانال‌ها و سوپرگروه‌هاست!
        settings["destination_id"] = dialog.id
        settings["destination_name"] = dialog.name or "بدون نام"
        settings["destination_type"] = dtype
        save_settings()

        await query.edit_message_text(
            "✅ <b>مقصد با موفقیت انتخاب و قفل شد</b>\n\n"
            f"📍 <b>{html.escape(settings['destination_name'])}</b>\n"
            f"🆔 آیدی مقصد: <code>{dialog.id}</code>\n"
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
# UNIVERSAL MESSAGE HANDLER (SUPPORTS DIRECT main.py UPLOAD)
# =========================================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_custom_caption, batch_counter

    if not is_owner(update) or not update.message:
        return

    msg = update.message

    # قابلیت نصب آپدیت با ارسال مستقیم فایل main.py داخل چت ربات
    if msg.document and msg.document.file_name == "main.py":
        status_msg = await msg.reply_text("🔄 <b>فایل <code>main.py</code> دریافت شد! در حال نصب آپدیت...</b>", parse_mode="HTML")
        try:
            tg_file = await context.bot.get_file(msg.document.file_id)
            content_bytes = await tg_file.download_as_bytearray()
            new_code = content_bytes.decode("utf-8")

            if "TelegramClient" not in new_code or "BOT_TOKEN" not in new_code:
                raise ValueError("کدهای داخل فایل main.py معتبر نیستند.")

            target_file = os.path.abspath(__file__)
            with open(target_file, "w", encoding="utf-8") as f:
                f.write(new_code)

            await status_msg.edit_text(
                "✅ <b>نسخه جدید <code>main.py</code> با موفقیت نصب شد!</b>\n"
                "♻️ ربات در حال ری‌استارت خودکار است (چند ثانیه دیگر /start بزنید)...",
                parse_mode="HTML",
            )
            asyncio.create_task(restart_bot_process())
        except Exception as e:
            await status_msg.edit_text(
                f"❌ <b>خطا در نصب فایل آپدیت:</b>\n<code>{html.escape(str(e))}</code>",
                parse_mode="HTML",
            )
        return

    if not await tg.is_user_authorized():
        if login_state.get("step"):
            await handle_login_step(update)
        else:
            await prompt_login(update)
        return

    if waiting_for_custom_caption and msg.text:
        waiting_for_custom_caption = False
        settings["custom_caption"] = msg.text.strip()
        if settings.get("caption_mode") in ("none", "clean"):
            settings["caption_mode"] = "replace"
        save_settings()
        await msg.reply_text(
            "✅ <b>آیدی / کپشن اختصاصی ذخیره شد و حالت «جایگزینی هوشمند» فعال گردید:</b>\n\n"
            f"<code>{html.escape(settings['custom_caption'])}</code>",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if not destination and not settings.get("destination_id"):
        await restore_destination()
    if not destination and not settings.get("destination_id"):
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

    if len(extracted_items) > 1 and settings.get("button_mode") == "prompt" and len(extracted_items) <= 15:
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
    log("🔌 Starting V5 Ultra Bot & Connecting Telegram...")

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

    log("🤖 V5 Ultra Bot is running!")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("🛑 Stopped.")
    except Exception as e:
        log(f"🔥 FATAL ERROR: {type(e).__name__}: {repr(e)}")
