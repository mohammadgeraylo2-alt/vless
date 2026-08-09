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

ASK_GB, ASK_DAYS = range(2)


def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(
        "سلام 👋\n"
        "برای ساخت کانفیگ VLESS دستور /getconfig رو بزن."
    )


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
    except (ValueError, KeyError) as e:
        await update.message.reply_text(f"❌ پاسخ سرور نامعتبر بود: {e}")
        return ConversationHandler.END

    message = (
        "✅ کانفیگ جدید ساخته شد\n"
        f"حجم: {resp_gb} گیگ\n"
        f"اعتبار: {resp_days} روز (تا {expires})\n\n"
        f"`{vless_link}`"
    )
    await update.message.reply_text(message, parse_mode="Markdown")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("لغو شد.")
    return ConversationHandler.END


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("getconfig", getconfig_start)],
        states={
            ASK_GB: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_gb)],
            ASK_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_days)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)

    application.run_polling()


main()
