import os
import logging
import requests
from telegram import Update
from telegram.ext import (
    Application,
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

ASK_GB, ASK_DAYS = range(2)
ASK_REVOKE_CHOICE = range(2, 3)[0]


def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(
        "سلام 👋\n"
        "/getconfig — ساخت کانفیگ جدید\n"
        "/usage — دیدن حجم باقی‌مونده\n"
        "/revoke — منقضی کردن دستی یه کانفیگ"
    )


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
    elif len(text) >= 4:
        # کاربر به‌جای عدد ردیف، بخشی از UUID رو فرستاده — همینو مستقیم به
        # سرور می‌فرستیم، خودِ سرور با پیشوند UUID پیدا می‌کنه.
        target_uuid = text
    else:
        await update.message.reply_text(
            "عدد نامعتبره. یکی از شماره‌های لیست (یا خودِ UUID) رو بفرست، یا /cancel."
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
    application.add_handler(getconfig_conv)
    application.add_handler(revoke_conv)

    application.run_polling()


main()
