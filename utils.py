import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from telegram.ext import ContextTypes

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = str(BASE_DIR / "bot_stats.db")
COVER_CACHE = str(BASE_DIR / "channel_cover_cached.jpg")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "BEXO50,xppx56")

def required_channels():
    """قائمة قنوات الاشتراك الإجباري من متغير CHANNEL_USERNAME مفصولة بفواصل."""
    return [item.strip().lstrip("@") for item in CHANNEL_USERNAME.split(",") if item.strip()]
OWNER_ID = int(os.getenv("OWNER_ID", "8798182716"))
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", "100")) * 1024 * 1024
MAINTENANCE_MODE = False


def init_db() -> None:
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            join_date TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            artist TEXT,
            date TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_user_id ON files(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_date ON files(date)")
        conn.commit()
        logging.info("✅ Database initialized")
    except Exception:
        logging.exception("❌ Database initialization failed")
        raise
    finally:
        if conn is not None:
            conn.close()


async def is_maintenance(update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not MAINTENANCE_MODE:
        return False
    if update.effective_user and update.effective_user.id == OWNER_ID:
        return False
    if update.effective_message:
        await update.effective_message.reply_text(
            "🛠️ البوت في وضع الصيانة\n\nسنعود للعمل قريباً. شكراً لصبرك."
        )
    return True


async def auto_clear_cache() -> None:
    """حذف الملفات المؤقتة الأقدم من ساعة، دون لمس ملفات المشروع الثابتة."""
    deleted = 0
    prefixes = ("input_", "output_", "custom_", "final_", "cover_", "video_", "extracted_", "audio_", "original_", "converted_", "compressed_")
    extensions = (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac", ".opus", ".mp4", ".mkv", ".webm", ".jpg", ".jpeg", ".png")
    cutoff = datetime.now().timestamp() - 3600
    try:
        for path in BASE_DIR.iterdir():
            if not path.is_file() or path.name in {"bot_stats.db", "channel_cover_cached.jpg"}:
                continue
            if not (path.name.startswith(prefixes) or path.suffix.lower() in extensions):
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    deleted += 1
            except OSError as exc:
                logging.warning("فشل حذف %s: %s", path, exc)
        if deleted:
            logging.info("🧹 Deleted %s temporary files", deleted)
    except Exception:
        logging.exception("❌ Cache cleanup failed")


async def check_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if user_id == OWNER_ID:
        return True
    for channel in required_channels():
        try:
            member = await context.bot.get_chat_member(f"@{channel}", user_id)
            if member.status in {"left", "kicked"}:
                return False
        except Exception:
            logging.exception("Subscription check failed for channel @%s / user %s", channel, user_id)
            return False
    return True


async def get_channel_cover(context: ContextTypes.DEFAULT_TYPE):
    try:
        cover = Path(COVER_CACHE)
        if cover.exists() and cover.stat().st_size > 0 and datetime.now().timestamp() - cover.stat().st_mtime < 86400:
            return str(cover)
        chat = await context.bot.get_chat(f"@{CHANNEL_USERNAME}")
        if not chat.photo:
            return str(cover) if cover.exists() and cover.stat().st_size > 0 else None
        photo_file = await context.bot.get_file(chat.photo.big_file_id)
        await photo_file.download_to_drive(str(cover))
        return str(cover) if cover.exists() and cover.stat().st_size > 0 else None
    except Exception:
        logging.exception("❌ Failed to fetch channel cover")
        cover = Path(COVER_CACHE)
        return str(cover) if cover.exists() and cover.stat().st_size > 0 else None


def add_user(user_id: int, first_name: str) -> None:
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "INSERT INTO users(user_id, first_name, join_date) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name",
            (user_id, first_name or "مستخدم", datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()
    except Exception:
        logging.exception("❌ Failed to add user %s", user_id)
    finally:
        if conn is not None:
            conn.close()


def add_file_record(user_id: int, title: str, artist: str) -> bool:
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "INSERT INTO files (user_id, title, artist, date) VALUES (?, ?, ?, ?)",
            (user_id, title, artist, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()
        return True
    except Exception:
        logging.exception("❌ Failed to record file")
        return False
    finally:
        if conn is not None:
            conn.close()


def get_user_stats(user_id: int):
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        files_count = conn.execute("SELECT COUNT(*) FROM files WHERE user_id=?", (user_id,)).fetchone()[0]
        last_activity = conn.execute("SELECT MAX(date) FROM files WHERE user_id=?", (user_id,)).fetchone()[0]
        return {"files_count": files_count, "last_activity": last_activity or "لا يوجد"}
    except Exception:
        logging.exception("❌ Failed to read user stats")
        return {"files_count": 0, "last_activity": "غير متاح"}
    finally:
        if conn is not None:
            conn.close()


def get_total_stats():
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        today = datetime.now().strftime("%Y-%m-%d")
        active_today = conn.execute("SELECT COUNT(DISTINCT user_id) FROM files WHERE date LIKE ?", (f"{today}%",)).fetchone()[0]
        return {"total_users": total_users, "total_files": total_files, "active_today": active_today}
    except Exception:
        logging.exception("❌ Failed to read total stats")
        return {"total_users": 0, "total_files": 0, "active_today": 0}
    finally:
        if conn is not None:
            conn.close()



FORCE_SETTINGS_FILE = BASE_DIR / "force_settings.json"


def get_force_settings():
    import json

    if FORCE_SETTINGS_FILE.exists():
        try:
            with open(FORCE_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (OSError, json.JSONDecodeError):
            logging.exception("Failed to load force settings")

    channels = [
        {
            "username": item.strip().lstrip("@"),
            "title": "📢 اشترك بالقناة",
            "url": "https://t.me/" + item.strip().lstrip("@"),
        }
        for item in os.getenv("CHANNEL_USERNAME", "BEXO50,xppx56").split(",")
        if item.strip()
    ]

    return {
        "channels": channels,
        "message": "🔒 الاشتراك إجباري لاستخدام البوت.",
    }


def save_force_settings(channels=None, message=None):
    import json

    settings = get_force_settings()

    if channels is not None:
        settings["channels"] = channels

    if message is not None:
        settings["message"] = message

    temp_file = FORCE_SETTINGS_FILE.with_suffix(".tmp")

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

    temp_file.replace(FORCE_SETTINGS_FILE)


def get_force_channels():
    return get_force_settings().get("channels", [])


def get_force_message():
    return get_force_settings().get(
        "message", "🔒 الاشتراك إجباري لاستخدام البوت."
    )


def required_channels():
    return [
        str(channel.get("username", "")).strip().lstrip("@")
        for channel in get_force_channels()
        if channel.get("username")
    ]
