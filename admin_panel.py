import os
import sqlite3
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from utils import BASE_DIR, DB_FILE, OWNER_ID
import utils


def _stats_text() -> str:
    with sqlite3.connect(DB_FILE) as conn:
        users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        files_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    status = "🟠 الصيانة مفعلة" if utils.MAINTENANCE_MODE else "🟢 يعمل بشكل طبيعي"
    return (
        "📊 إحصائيات البوت\n\n"
        f"👤 المستخدمون: {users_count}\n"
        f"🎵 العمليات الناجحة: {files_count}\n"
        f"⚙️ الحالة: {status}"
    )


async def panel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        if update.effective_message:
            await update.effective_message.reply_text("🚫 هذه اللوحة متاحة للمطور فقط.")
        return

    from keyboards import admin_panel_keyboard
    await update.effective_message.reply_text(
        "🛠️ لوحة تحكم المطور\n\nاختر العملية المطلوبة:",
        reply_markup=admin_panel_keyboard(utils.MAINTENANCE_MODE),
    )


async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not update.effective_user or update.effective_user.id != OWNER_ID:
        if query:
            await query.answer("🚫 غير مصرح لك", show_alert=True)
        return

    from keyboards import admin_panel_keyboard, force_subscription_keyboard
    await query.answer()

    if query.data == "force_menu":
        channels = utils.get_force_channels()
        await query.edit_message_text("📣 إدارة الاشتراك الإجباري\n\nالقنوات الحالية:\n" + ("\n".join(f"{i+1}. {c.get('title') or c.get('username')} (@{c.get('username')})" for i, c in enumerate(channels)) or "لا توجد قنوات") + "\n\nاختر إجراءً:", reply_markup=force_subscription_keyboard(channels))
    elif query.data == "force_back":
        await query.edit_message_text("🛠️ لوحة تحكم المطور\n\nاختر العملية المطلوبة:", reply_markup=admin_panel_keyboard(utils.MAINTENANCE_MODE))
    elif query.data == "force_add":
        context.user_data["admin_step"] = "force_add_channel"
        await query.edit_message_text("➕ أرسل القناة بهذا الشكل:\n@channel_username | اسم الزر\n\nيجب أن يكون البوت مشرفًا في القناة. أرسل /cancel للإلغاء.")
    elif query.data == "force_message":
        context.user_data["admin_step"] = "force_message"
        await query.edit_message_text("✏️ أرسل رسالة الاشتراك الإجباري الجديدة. أرسل /cancel للإلغاء.")
    elif query.data.startswith("force_del_"):
        try:
            index = int(query.data.rsplit("_", 1)[1])
            channels = utils.get_force_channels()
            if 0 <= index < len(channels):
                channels.pop(index)
                utils.save_force_settings(channels=channels)
        except (ValueError, IndexError):
            pass
        channels = utils.get_force_channels()
        await query.edit_message_text("📣 إدارة الاشتراك الإجباري\n\nتم تحديث قائمة القنوات.", reply_markup=force_subscription_keyboard(channels))

    elif query.data == "admin_stats":
        await query.edit_message_text(_stats_text(), reply_markup=admin_panel_keyboard(utils.MAINTENANCE_MODE))

    elif query.data == "toggle_maintenance":
        utils.MAINTENANCE_MODE = not utils.MAINTENANCE_MODE
        state = "مفعلة" if utils.MAINTENANCE_MODE else "متوقفة"
        await query.edit_message_text(
            f"🛠️ تم تغيير وضع الصيانة.\n\nالحالة الحالية: {state}",
            reply_markup=admin_panel_keyboard(utils.MAINTENANCE_MODE),
        )

    elif query.data == "admin_broadcast":
        context.user_data["admin_step"] = "broadcasting"
        await query.edit_message_text(
            "📢 إرسال إعلان\n\nأرسل الآن نص الإعلان.\n\nاضغط /cancel لإلغاء العملية."
        )

    elif query.data == "admin_clean":
        deleted = 0
        prefixes = ("input_", "output_", "custom_", "final_", "cover_", "video_", "extracted_", "audio_", "original_", "converted_", "compressed_")
        protected = {"bot_stats.db", "channel_cover_cached.jpg", ".env"}
        for path in BASE_DIR.iterdir():
            if not path.is_file() or path.name in protected:
                continue
            if path.name.startswith(prefixes) or path.suffix.lower() in {".mp3", ".mp4", ".m4a", ".wav", ".flac", ".jpg", ".jpeg", ".png"}:
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    pass
        await query.edit_message_text(
            f"🧹 التنظيف مكتمل\n\nتم حذف {deleted} ملف مؤقت.",
            reply_markup=admin_panel_keyboard(utils.MAINTENANCE_MODE),
        )

    elif query.data == "close_admin":
        await query.message.delete()


async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID or not update.message:
        return
    step = context.user_data.get("admin_step")
    if step in {"force_add_channel", "force_message"}:
        text = (update.message.text or "").strip()
        if text == "/cancel":
            context.user_data.pop("admin_step", None)
            await update.message.reply_text("✅ تم إلغاء العملية.")
            return
        if step == "force_add_channel":
            parts = text.split("|", 1)
            username = parts[0].strip().lstrip("@").replace("https://t.me/", "").strip("/")
            title = parts[1].strip() if len(parts) > 1 else "📢 " + username
            if not username or " " in username:
                await update.message.reply_text("❌ صيغة غير صحيحة. أرسل @username | اسم الزر")
                return
            channels = utils.get_force_channels()
            if any(str(c.get("username", "")).lower() == username.lower() for c in channels):
                await update.message.reply_text("⚠️ القناة موجودة مسبقًا.")
                return
            channels.append({"username": username, "title": title, "url": "https://t.me/" + username})
            utils.save_force_settings(channels=channels)
            context.user_data.pop("admin_step", None)
            await update.message.reply_text("✅ تمت إضافة القناة. تأكد أن البوت مشرف فيها.")
            return
        utils.save_force_settings(message=text)
        context.user_data.pop("admin_step", None)
        await update.message.reply_text("✅ تم تحديث رسالة الاشتراك الإجباري.")
        return
    if step != "broadcasting":
        return

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("❌ لا يمكن إرسال إعلان فارغ.")
        return
    if text == "/cancel":
        context.user_data.pop("admin_step", None)
        await update.message.reply_text("✅ تم إلغاء الإذاعة.")
        return

    with sqlite3.connect(DB_FILE) as conn:
        users = [row[0] for row in conn.execute("SELECT user_id FROM users")]

    status = await update.message.reply_text("📢 جاري إرسال الإعلان…")
    success = failed = 0
    for user_id in users:
        try:
            await context.bot.send_message(chat_id=user_id, text=f"📢 إعلان من المطور\n\n{text}")
            success += 1
        except Exception:
            failed += 1

    context.user_data.pop("admin_step", None)
    await status.edit_text(f"✅ انتهت الإذاعة\n\n📨 نجح: {success}\n❌ فشل: {failed}")
