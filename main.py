import asyncio
import logging
import os

from dotenv import load_dotenv

# تحميل .env قبل استيراد handlers/utils
load_dotenv()

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    ApplicationHandlerStop,
    filters,
)

from admin_panel import admin_callback_handler, panel_handler, broadcast_handler
from handlers import (
    callback_query_handler,
    media_handler,
    photo_handler,
    start_handler,
    text_handler,
)
from utils import auto_clear_cache, init_db, check_subscription, required_channels, OWNER_ID


# =========================================================
# Logging
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)

logger = logging.getLogger(__name__)


# =========================================================
# Environment
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")


# Local Telegram Bot API (optional)
# يبقى مفعّلًا على البيئة القديمة، ويمكن تعطيله على Railway.
LOCAL_API_URL = "http://127.0.0.1:8081/bot"
LOCAL_FILE_API_URL = "http://127.0.0.1:8081/file/bot"
USE_LOCAL_API = os.getenv("USE_LOCAL_API", "true").strip().lower() in {"1", "true", "yes", "on"}


# =========================================================
# Cancel Handler
# =========================================================

async def cancel_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    context.user_data.pop("admin_step", None)
    context.user_data.clear()

    if update.effective_message:
        await update.effective_message.reply_text(
            "✅ تم إلغاء العملية."
        )



async def combined_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and user.id == OWNER_ID and context.user_data.get("admin_step"):
        await broadcast_handler(update, context)
        return
    await text_handler(update, context)


async def subscription_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """منع جميع التفاعلات من غير المشتركين، وليس /start فقط."""
    user = update.effective_user
    if not user or user.id == OWNER_ID:
        return
    query = update.callback_query
    if query and query.data == "subscription_check":
        return
    message = update.effective_message
    if message and (message.text or "").split(maxsplit=1)[0:1] == ["/cancel"]:
        return
    if await check_subscription(user.id, context):
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    buttons = [
        [InlineKeyboardButton(f"📢 اشترك في @{name}", url=f"https://t.me/{name}")]
        for name in required_channels()
    ]
    buttons.append([InlineKeyboardButton("✅ تحققت من الاشتراك", callback_data="subscription_check")])
    import utils
    text = utils.get_force_message() + "\nاشترك في جميع القنوات التالية ثم اضغط «تحققت من الاشتراك»."
    if query:
        await query.answer("🔒 يجب الاشتراك بالقنوات أولاً", show_alert=True)
        if query.message:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    elif message:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    raise ApplicationHandlerStop


async def subscription_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if await check_subscription(query.from_user.id, context):
        await query.answer("✅ تم التحقق من الاشتراك")
        await query.message.reply_text("✅ تم التحقق من اشتراكك. أرسل /start لفتح القائمة.")
    else:
        await query.answer("❌ لم تشترك في جميع القنوات بعد.", show_alert=True)
    raise ApplicationHandlerStop


# =========================================================
# Global Error Handler
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    error = context.error

    logger.error(
        "Unhandled exception while processing update: %s",
        error,
        exc_info=error,
    )

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ حدث خطأ غير متوقع.\n"
                "يرجى المحاولة مرة أخرى بعد قليل."
            )
        except Exception:
            pass


# =========================================================
# Cleanup Job
# =========================================================

async def cleanup_job(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    try:
        await auto_clear_cache()

    except Exception:
        logger.exception(
            "❌ فشل التنظيف الدوري للملفات المؤقتة"
        )


# =========================================================
# Build Application
# =========================================================

def build_application() -> Application:

    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN غير موجود في متغيرات البيئة"
        )

    # تهيئة قاعدة البيانات
    init_db()

    # -----------------------------------------------------
    # Telegram Application
    # -----------------------------------------------------
    # على Railway نستخدم Telegram Bot API الرسمي.
    # على البيئة القديمة يمكن إبقاء Local Bot API كما كان.
    builder = Application.builder().token(TOKEN)

    if USE_LOCAL_API:
        builder = (
            builder
            .base_url(LOCAL_API_URL)
            .base_file_url(LOCAL_FILE_API_URL)
            .local_mode(True)
        )

    app = builder.build()

    # -----------------------------------------------------
    # Automatic temporary-file cleanup
    # -----------------------------------------------------

    if app.job_queue:

        app.job_queue.run_repeating(
            cleanup_job,
            interval=1800,
            first=60,
            name="temp-cleanup",
        )

    # فحص الاشتراك قبل تمرير أي تحديث إلى معالجات البوت
    app.add_handler(TypeHandler(Update, subscription_guard), group=-1)

    # =====================================================
    # Commands
    # =====================================================

    app.add_handler(
        CommandHandler(
            "start",
            start_handler,
        )
    )

    app.add_handler(
        CommandHandler(
            "panel",
            panel_handler,
        )
    )

    app.add_handler(
        CommandHandler(
            "cancel",
            cancel_handler,
        )
    )

    # =====================================================
    # Admin Callback Queries
    # =====================================================
    app.add_handler(
        CallbackQueryHandler(
            admin_callback_handler,
            pattern=(
                r"^(admin_stats|"
                r"admin_broadcast|"
                r"admin_clean|"
                r"toggle_maintenance|"
                r"close_admin|"
                r"force_menu|"
                r"force_back|"
                r"force_add|"
                r"force_message|"
                r"force_del_\d+)$"
            ),
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            subscription_check_callback,
            pattern=r"^subscription_check$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_query_handler
        )
    )
    # =====================================================
    # General Callback Queries
    # =====================================================

    app.add_handler(
        CallbackQueryHandler(
            callback_query_handler
        )
    )

    # =====================================================
    # Images
    # =====================================================

    app.add_handler(
        MessageHandler(
            filters.PHOTO
            | filters.Document.IMAGE,
            photo_handler,
        )
    )

    # =====================================================
    # Audio / Video
    # =====================================================

    app.add_handler(
        MessageHandler(
            filters.AUDIO
            | filters.VIDEO
            | filters.Document.AUDIO
            | filters.Document.VIDEO,
            media_handler,
        )
    )

    # =====================================================
    # Text
    # =====================================================

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            combined_text_handler,
        )
    )

    # =====================================================
    # Global Error Handler
    # =====================================================

    app.add_error_handler(
        error_handler
    )

    return app


# =========================================================
# Main
# =========================================================

def main() -> None:

    try:

        app = build_application()

        logger.info(
            "🤖 Music Bot started successfully"
        )

        logger.info(
            "🚀 Local Telegram Bot API enabled"
        )

        logger.info(
            "📦 Large file support enabled"
        )

        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )

    except KeyboardInterrupt:

        logger.info(
            "🛑 Bot stopped by user"
        )

    except Exception:

        logger.exception(
            "❌ فشل تشغيل البوت"
        )

        raise


# =========================================================
# Entry Point
# =========================================================

if __name__ == "__main__":
    main()
