import asyncio
import json
import os
import logging
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
XRAY_MANAGE_URL = os.environ["XRAY_MANAGE_URL"]
XRAY_MANAGE_SECRET = os.environ["XRAY_MANAGE_SECRET"]

XRAY_BASE_URL = XRAY_MANAGE_URL.rsplit("/", 1)[0]
XRAY_USAGE_URL = XRAY_BASE_URL + "/usage"
XRAY_REVOKE_URL = XRAY_BASE_URL + "/revoke"

# ------------------------------------------------------------- سیستم دعوت
# مقدار اولیه (فقط بار اولی که فایل کانال‌ها هنوز وجود نداره استفاده می‌شه).
# بعد از اون، کانال‌ها با دستورهای /addchannel و /removechannel مدیریت می‌شن.
_SEED_FORCE_JOIN_CHANNELS = [
    c.strip() for c in os.environ.get("FORCE_JOIN_CHANNELS", "").split(",") if c.strip()
]
REFERRAL_REQUIRED = int(os.environ.get("REFERRAL_REQUIRED", "5"))
REFERRAL_REWARD_GB = int(os.environ.get("REFERRAL_REWARD_GB", "5"))
REFERRAL_REWARD_DAYS = int(os.environ.get("REFERRAL_REWARD_DAYS", "10"))
REFERRAL_DB_PATH = os.environ.get("REFERRAL_DB_PATH", "/data/referral_users.json")
CHANNELS_DB_PATH = os.environ.get("CHANNELS_DB_PATH", "/data/force_join_channels.json")

_db_lock = asyncio.Lock()
_channels_lock = asyncio.Lock()

ASK_GB, ASK_DAYS = range(2)
ASK_REVOKE_CHOICE = range(2, 3)[0]


def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID


# ------------------------------------------------------------- سیستم دعوت
def _load_db():
    try:
        with open(REFERRAL_DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_db(db):
    os.makedirs(os.path.dirname(REFERRAL_DB_PATH), exist_ok=True)
    tmp_path = REFERRAL_DB_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, REFERRAL_DB_PATH)


def _get_user(db, user_id):
    key = str(user_id)
    if key not in db:
        db[key] = {
            "username": None,
            "referred_by": None,
            "referral_credited": False,
            "joined_verified": False,
            "referral_count": 0,
            "referred_usernames": [],
        }
    return db[key]


def _load_channels():
    try:
        with open(CHANNELS_DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return list(_SEED_FORCE_JOIN_CHANNELS)


def _save_channels(channels):
    os.makedirs(os.path.dirname(CHANNELS_DB_PATH), exist_ok=True)
    tmp_path = CHANNELS_DB_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(channels, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CHANNELS_DB_PATH)


def _normalize_channel(raw):
    """یه ورودی خام (با یا بدون @، یا لینک t.me/xxx) رو به فرمت @xxx تبدیل می‌کنه."""
    raw = raw.strip()
    if raw.startswith("https://t.me/"):
        raw = raw[len("https://t.me/"):]
    elif raw.startswith("t.me/"):
        raw = raw[len("t.me/"):]
    raw = raw.lstrip("@").strip()
    return f"@{raw}" if raw else ""


def _display_name(user):
    if user.username:
        return f"@{user.username}"
    return user.first_name or "یه کاربر"


async def _is_member_of_all_channels(bot, user_id):
    """True اگه کاربر عضو همه‌ی کانال‌های اجباری باشه. اگه چک یه کانال به هر
    دلیلی (مثلا ربات ادمین اون کانال نیست) خطا بده، همون کانال رو
    "عضو نیست" در نظر می‌گیریم تا کسی اشتباهی رد نشه."""
    channels = _load_channels()
    for channel in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
        except TelegramError as e:
            logger.warning("خطا در چک عضویت %s برای کاربر %s: %s", channel, user_id, e)
            return False
        if member.status not in ("member", "administrator", "creator"):
            return False
    return True


def _build_join_keyboard():
    channels = _load_channels()
    rows = []
    for channel in channels:
        handle = channel.lstrip("@")
        rows.append([InlineKeyboardButton(f"📢 عضویت در {channel}", url=f"https://t.me/{handle}")])
    rows.append([InlineKeyboardButton("✅ عضویت من رو بررسی کن", callback_data="check_join")])
    return InlineKeyboardMarkup(rows)


def _referral_link(bot_username, user_id):
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def _build_referral_text(bot_username, user_id, record):
    count = record["referral_count"]
    remaining = max(REFERRAL_REQUIRED - count, 0)
    link = _referral_link(bot_username, user_id)

    lines = [
        "🎁 <b>دریافت کانفیگ اختصاصی رایگان</b>",
        "",
        f"با دعوت <b>{REFERRAL_REQUIRED} نفر</b> از طریق لینک اختصاصی‌ت، یه کانفیگ",
        f"<b>{REFERRAL_REWARD_GB} گیگ / {REFERRAL_REWARD_DAYS} روزه</b> کاملاً رایگان می‌گیری 🚀",
        "",
        "🔗 لینک اختصاصی شما:",
        f"<code>{link}</code>",
        "",
        f"📊 پیشرفت: <b>{count} از {REFERRAL_REQUIRED}</b> نفر دعوت شده",
    ]
    if record["referred_usernames"]:
        lines.append("👥 آخرین دعوت‌شده‌ها: " + "، ".join(record["referred_usernames"][-5:]))
    if remaining > 0:
        lines.append(f"🎯 فقط <b>{remaining} نفر</b> دیگه مونده تا کانفیگ اختصاصی رایگانت!")
    else:
        lines.append("✅ به هدف رسیدی! کانفیگت داره ساخته می‌شه...")
    return "\n".join(lines)


def _build_referral_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 بروزرسانی وضعیت", callback_data="refresh_referral")]]
    )


async def _send_referral_panel(bot, chat_id, user_id, record, edit_message=None):
    me = await bot.get_me()
    text = _build_referral_text(me.username, user_id, record)
    if edit_message is not None:
        await edit_message.edit_text(text, parse_mode="HTML", reply_markup=_build_referral_keyboard())
    else:
        await bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=_build_referral_keyboard()
        )


async def _grant_free_config(bot, user_id):
    """کانفیگ جایزه رو می‌سازه و برای کاربر می‌فرسته."""
    try:
        resp = requests.post(
            XRAY_MANAGE_URL,
            json={"secret": XRAY_MANAGE_SECRET, "gb": REFERRAL_REWARD_GB, "days": REFERRAL_REWARD_DAYS},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.exception("ساخت کانفیگ جایزه برای %s شکست خورد", user_id)
        await bot.send_message(
            chat_id=user_id,
            text="⚠️ تبریک، به هدف رسیدی! ولی تو ساخت کانفیگ یه خطای موقت پیش اومد. "
            "به‌زودی به‌صورت دستی برات ارسال می‌شه، یا /start رو دوباره بزن.",
        )
        await bot.send_message(chat_id=ADMIN_ID, text=f"❌ ساخت کانفیگ جایزه‌ی رفرال برای {user_id} شکست خورد: {e}")
        return False

    vless_link = data.get("vless_link", "")
    status_link = data.get("status_link")
    message = (
        "🎉 <b>تبریک! به هدف رسیدی</b>\n"
        f"کانفیگ اختصاصی {REFERRAL_REWARD_GB} گیگ / {REFERRAL_REWARD_DAYS} روزه‌ت آماده‌ست:\n\n"
        f"<code>{vless_link}</code>"
    )
    if status_link:
        message += f"\n\n📊 چک حجم/انقضا:\n{status_link}"
    await bot.send_message(chat_id=user_id, text=message, parse_mode="HTML")
    return True


async def _handle_verified_user(bot, user):
    """وقتی عضویت یه کاربر تازه تأیید می‌شه صدا زده می‌شه: کردیت دعوت‌کننده
    رو ثبت می‌کنه (اگه اولین‌باره) و پنل رفرال خودِ کاربر رو برمی‌گردونه."""
    async with _db_lock:
        db = _load_db()
        record = _get_user(db, user.id)
        record["username"] = _display_name(user)
        first_time_verified = not record["joined_verified"]
        record["joined_verified"] = True

        if first_time_verified and record["referred_by"] and not record["referral_credited"]:
            inviter_id = record["referred_by"]
            if str(inviter_id) != str(user.id):  # جلوگیری از دعوت خودی
                inviter = _get_user(db, inviter_id)
                inviter["referral_count"] += 1
                inviter["referred_usernames"].append(record["username"] or "کاربر ناشناس")
                record["referral_credited"] = True
                reward_ready = inviter["referral_count"] >= REFERRAL_REQUIRED

                # برای پیام پیشرفت، وضعیت "قبل از ریست" (مثلا ۵ از ۵) رو نشون می‌دیم
                progress_snapshot = {
                    "referral_count": inviter["referral_count"],
                    "referred_usernames": inviter["referred_usernames"],
                }
                if reward_ready:
                    inviter["referral_count"] = 0
                    inviter["referred_usernames"] = []
                _save_db(db)

                try:
                    me = await bot.get_me()
                    progress_text = _build_referral_text(me.username, inviter_id, progress_snapshot)
                    await bot.send_message(
                        chat_id=inviter_id,
                        text=f"👤 {record['username']} با لینک شما عضو شد!\n\n{progress_text}",
                        parse_mode="HTML",
                        reply_markup=_build_referral_keyboard(),
                    )
                except TelegramError:
                    logger.warning("نشد به دعوت‌کننده %s پیام بدیم (شاید بلاک کرده)", inviter_id)

                if reward_ready:
                    await _grant_free_config(bot, inviter_id)
            else:
                record["referral_credited"] = True  # جلوگیری از تلاش دوباره
                _save_db(db)
        else:
            _save_db(db)

        return record


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if is_admin(update):
        await update.message.reply_text(
            "سلام 👋\n"
            "/getconfig — ساخت کانفیگ جدید\n"
            "/usage — دیدن حجم باقی‌مونده\n"
            "/revoke — منقضی کردن دستی یه کانفیگ\n"
            "/channels — لیست کانال‌های جوین اجباری\n"
            "/addchannel @channel — اضافه‌کردن کانال جوین اجباری\n"
            "/removechannel @channel — حذف کانال جوین اجباری"
        )
        return

    # ثبت دعوت‌کننده (فقط بار اول، فقط اگه لینک با پارامتر ref_ باز شده باشه)
    if context.args and context.args[0].startswith("ref_"):
        inviter_id = context.args[0][len("ref_"):]
        if inviter_id.isdigit():
            async with _db_lock:
                db = _load_db()
                record = _get_user(db, user.id)
                if record["referred_by"] is None:
                    record["referred_by"] = int(inviter_id)
                    _save_db(db)

    if not _load_channels():
        # هیچ کانالی برای جوین اجباری تنظیم نشده؛ مستقیم بریم سراغ پنل رفرال
        record = await _handle_verified_user(context.bot, user)
        await _send_referral_panel(context.bot, update.effective_chat.id, user.id, record)
        return

    if await _is_member_of_all_channels(context.bot, user.id):
        record = await _handle_verified_user(context.bot, user)
        await _send_referral_panel(context.bot, update.effective_chat.id, user.id, record)
    else:
        await update.message.reply_text(
            "👋 <b>به ربات خوش اومدی!</b>\n\n"
            "برای دریافت <b>کانفیگ رایگان</b>، اول باید عضو کانال‌های زیر بشی:",
            parse_mode="HTML",
            reply_markup=_build_join_keyboard(),
        )


async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if await _is_member_of_all_channels(context.bot, user.id):
        await query.answer("✅ عضویت تأیید شد!")
        record = await _handle_verified_user(context.bot, user)
        await _send_referral_panel(context.bot, update.effective_chat.id, user.id, record, edit_message=query.message)
    else:
        await query.answer("❌ هنوز عضو همه‌ی کانال‌ها نشدی. اول جوین کن، بعد دوباره بزن.", show_alert=True)


async def refresh_referral_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    await query.answer()

    async with _db_lock:
        db = _load_db()
        record = _get_user(db, user.id)

    await _send_referral_panel(context.bot, update.effective_chat.id, user.id, record, edit_message=query.message)


# --------------------------------------------------------- مدیریت کانال‌ها
async def channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    channels = _load_channels()
    if not channels:
        await update.message.reply_text(
            "هیچ کانالی برای جوین اجباری تنظیم نشده.\n"
            "برای اضافه‌کردن: /addchannel @channel_username"
        )
        return
    lines = ["📋 کانال‌های جوین اجباری:\n"]
    for i, c in enumerate(channels, start=1):
        lines.append(f"{i}. {c}")
    lines.append("\nاضافه‌کردن: /addchannel @channel")
    lines.append("حذف: /removechannel @channel (یا شماره‌ش)")
    await update.message.reply_text("\n".join(lines))


async def addchannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("فرمت درست: /addchannel @channel_username")
        return

    channel = _normalize_channel(context.args[0])
    if not channel or len(channel) < 2:
        await update.message.reply_text("یوزرنیم کانال نامعتبره.")
        return

    async with _channels_lock:
        channels = _load_channels()
        if channel in channels:
            await update.message.reply_text(f"{channel} از قبل تو لیست هست.")
            return
        channels.append(channel)
        _save_channels(channels)

    await update.message.reply_text(
        f"✅ {channel} اضافه شد.\n\n"
        "⚠️ یادت نره ربات رو تو این کانال ادمین کنی، وگرنه چک عضویت کار نمی‌کنه."
    )


async def removechannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("فرمت درست: /removechannel @channel_username (یا شماره‌ش از /channels)")
        return

    target = context.args[0].strip()

    async with _channels_lock:
        channels = _load_channels()
        if target.isdigit():
            idx = int(target) - 1
            if idx < 0 or idx >= len(channels):
                await update.message.reply_text("شماره نامعتبره. /channels رو بزن تا لیست رو ببینی.")
                return
            removed = channels.pop(idx)
        else:
            channel = _normalize_channel(target)
            if channel not in channels:
                await update.message.reply_text(f"{channel} تو لیست نیست.")
                return
            channels.remove(channel)
            removed = channel
        _save_channels(channels)

    await update.message.reply_text(f"🗑 {removed} از لیست جوین اجباری حذف شد.")


# ---------------------------------------------------------------- getconfig
async def getconfig_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    await update.message.reply_text("چند گیگ حجم می‌خوای؟ (فقط عدد)")
    return ASK_GB


async def ask_gb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("لطفاً فقط عدد بفرست. چند گیگ؟")
        return ASK_GB
    context.user_data["gb"] = int(text)
    await update.message.reply_text("چند روز اعتبار داشته باشه؟ (فقط عدد)")
    return ASK_DAYS


async def ask_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("لطفاً فقط عدد بفرست. چند روز؟")
        return ASK_DAYS
    days = int(text)
    gb = context.user_data["gb"]

    await update.message.reply_text("⏳ در حال ساخت کانفیگ...")

    try:
        resp = requests.post(
            XRAY_MANAGE_URL,
            json={"secret": XRAY_MANAGE_SECRET, "gb": gb, "days": days},
            timeout=20,
        )
    except requests.RequestException as e:
        logger.exception("Request failed")
        await update.message.reply_text(f"❌ خطا در ارتباط با سرور: {e}")
        return ConversationHandler.END

    if resp.status_code != 200:
        await update.message.reply_text(
            f"❌ خطا از سمت سرور (status {resp.status_code}):\n{resp.text[:500]}"
        )
        return ConversationHandler.END

    try:
        data = resp.json()
        vless_link = data["vless_link"]
        expires = data.get("expires", "-")
        resp_gb = data.get("gb", gb)
        resp_days = data.get("days", days)
        status_link = data.get("status_link")
    except (ValueError, KeyError) as e:
        await update.message.reply_text(f"❌ پاسخ سرور نامعتبر بود: {e}")
        return ConversationHandler.END

    message = (
        "✅ کانفیگ جدید ساخته شد\n"
        f"حجم: {resp_gb} گیگ\n"
        f"اعتبار: {resp_days} روز (تا {expires})\n\n"
        f"`{vless_link}`"
    )
    if status_link:
        message += f"\n\n📊 چک حجم/انقضا:\n{status_link}"
    await update.message.reply_text(message, parse_mode="Markdown")
    return ConversationHandler.END


# -------------------------------------------------------------------- usage
async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    await update.message.reply_text("⏳ در حال گرفتن گزارش مصرف...")

    try:
        resp = requests.post(
            XRAY_USAGE_URL, json={"secret": XRAY_MANAGE_SECRET}, timeout=20
        )
    except requests.RequestException as e:
        await update.message.reply_text(f"❌ خطا در ارتباط با سرور: {e}")
        return

    if resp.status_code != 200:
        await update.message.reply_text(
            f"❌ خطا از سمت سرور (status {resp.status_code}):\n{resp.text[:500]}"
        )
        return

    try:
        clients = resp.json().get("clients", [])
    except ValueError:
        await update.message.reply_text("❌ پاسخ سرور نامعتبر بود.")
        return

    if not clients:
        await update.message.reply_text("هیچ کاربر فعالی ثبت نشده.")
        return

    lines = ["📊 گزارش مصرف کاربران:\n"]
    for c in clients:
        status = "⚠️ منقضی‌شده" if c["expired"] else "✅ فعال"
        lines.append(
            f"{status}\n"
            f"مصرف‌شده: {c['used_gb']} / {c['gb']} گیگ\n"
            f"باقی‌مانده: {c['remaining_gb']} گیگ\n"
            f"انقضا: {c['days_expires']}\n"
            f"UUID: `{c['uuid'][:8]}...`\n"
            "―――――――"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ------------------------------------------------------------------- revoke
async def revoke_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END

    await update.message.reply_text("⏳ در حال گرفتن لیست کاربران...")

    try:
        resp = requests.post(
            XRAY_USAGE_URL, json={"secret": XRAY_MANAGE_SECRET}, timeout=20
        )
    except requests.RequestException as e:
        await update.message.reply_text(f"❌ خطا در ارتباط با سرور: {e}")
        return ConversationHandler.END

    if resp.status_code != 200:
        await update.message.reply_text(f"❌ خطا از سمت سرور (status {resp.status_code})")
        return ConversationHandler.END

    clients = resp.json().get("clients", [])
    if not clients:
        await update.message.reply_text("هیچ کاربر فعالی برای حذف نیست.")
        return ConversationHandler.END

    context.user_data["revoke_map"] = {str(i + 1): c["uuid"] for i, c in enumerate(clients)}

    lines = ["کدوم کاربر رو می‌خوای منقضی کنی؟ عدد رو بفرست:\n"]
    for i, c in enumerate(clients, start=1):
        status = "⚠️ منقضی‌شده" if c["expired"] else "✅ فعال"
        lines.append(
            f"{i}. {status} — {c['remaining_gb']}/{c['gb']} گیگ باقی‌مونده — "
            f"UUID: `{c['uuid'][:8]}...`"
        )
    lines.append("\nبرای لغو /cancel رو بفرست.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return ASK_REVOKE_CHOICE


async def revoke_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    revoke_map = context.user_data.get("revoke_map", {})

    if text in revoke_map:
        target_uuid = revoke_map[text]
    else:
        # کاربر ممکنه بخشی از UUID رو با «...» انتهایی (که فقط برای نمایش
        # کوتاه‌شده تو لیست تلگرامه) کپی کرده باشه؛ همچین چیزی رو پاک می‌کنیم.
        cleaned = text.strip().rstrip(".").strip()
        if len(cleaned) >= 4:
            target_uuid = cleaned
        else:
            await update.message.reply_text(
                "عدد نامعتبره. یکی از شماره‌های لیست (یا خودِ UUID، بدون سه‌نقطه) رو بفرست، یا /cancel."
            )
            return ASK_REVOKE_CHOICE

    await update.message.reply_text("⏳ در حال حذف...")

    try:
        resp = requests.post(
            XRAY_REVOKE_URL,
            json={"secret": XRAY_MANAGE_SECRET, "uuid": target_uuid},
            timeout=20,
        )
    except requests.RequestException as e:
        await update.message.reply_text(f"❌ خطا در ارتباط با سرور: {e}")
        return ConversationHandler.END

    if resp.status_code != 200:
        try:
            err = resp.json().get("error", resp.text[:300])
        except ValueError:
            err = resp.text[:300]
        await update.message.reply_text(f"❌ {err}\nدوباره امتحان کن یا /cancel بفرست.")
        return ASK_REVOKE_CHOICE

    data = resp.json()
    await update.message.reply_text(f"✅ کانفیگ حذف شد ({data.get('email', '')})")
    return ConversationHandler.END


# -------------------------------------------------------------------- misc
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("لغو شد.")
    return ConversationHandler.END


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    getconfig_conv = ConversationHandler(
        entry_points=[CommandHandler("getconfig", getconfig_start)],
        states={
            ASK_GB: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_gb)],
            ASK_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_days)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    revoke_conv = ConversationHandler(
        entry_points=[CommandHandler("revoke", revoke_start)],
        states={
            ASK_REVOKE_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, revoke_choice)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("usage", usage_command))
    application.add_handler(CommandHandler("channels", channels_command))
    application.add_handler(CommandHandler("addchannel", addchannel_command))
    application.add_handler(CommandHandler("removechannel", removechannel_command))
    application.add_handler(getconfig_conv)
    application.add_handler(revoke_conv)
    application.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    application.add_handler(CallbackQueryHandler(refresh_referral_callback, pattern="^refresh_referral$"))

    application.run_polling()


main()
