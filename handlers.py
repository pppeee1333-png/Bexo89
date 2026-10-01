import asyncio
import logging
import os
import shutil
import sqlite3
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any

from telegram import Update
from telegram.ext import ContextTypes

from keyboards import main_menu_keyboard, subscription_keyboard
from utils import (
    check_subscription,
    is_maintenance,
    DB_FILE,
    OWNER_ID,
    MAX_FILE_SIZE,
    get_channel_cover,
    add_user,
    add_file_record,
)

logger = logging.getLogger(__name__)


# ============================================================
# ⚙️ إعدادات
# ============================================================

MAX_CONCURRENT_PROCESSES = 2

# Local Bot API يسمح بالتعامل مع الملفات الكبيرة.
# لا نستخدم حد 50MB القديم.
TELEGRAM_AUDIO_LIMIT = MAX_FILE_SIZE

_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROCESSES)


# ============================================================
# 📋 الصيغ المدعومة
# ============================================================

SUPPORTED_AUDIO_EXTENSIONS: Tuple[str, ...] = (
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".wav",
    ".flac",
    ".wma",
    ".opus",
    ".aiff",
    ".alac",
    ".ape",
    ".amr",
    ".3gp",
    ".mka",
    ".ac3",
    ".dts",
    ".midi",
)

SUPPORTED_AUDIO_MIME_TYPES: Tuple[str, ...] = (
    "audio/mpeg",
    "audio/mp4",
    "audio/aac",
    "audio/ogg",
    "audio/wav",
    "audio/flac",
    "audio/x-wav",
    "audio/opus",
    "audio/webm",
    "audio/x-m4a",
    "audio/aiff",
    "audio/amr",
    "audio/ac3",
    "audio/midi",
)

IMAGE_EXTENSIONS: Tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
)

AUDIO_QUALITIES: Tuple[Tuple[int, str], ...] = (
    (320, "320k"),
    (256, "256k"),
    (224, "224k"),
    (192, "192k"),
    (160, "160k"),
    (128, "128k"),
    (96, "96k"),
    (64, "64k"),
    (48, "48k"),
    (32, "32k"),
)


# ============================================================
# 🧹 إدارة الملفات
# ============================================================

def safe_remove(file_path: Optional[str]) -> bool:
    if not file_path:
        return False

    try:
        path = Path(file_path)

        if not path.exists():
            return False

        if path.is_file():
            path.unlink()
            logger.debug("🗑️ Deleted: %s", file_path)
            return True

    except OSError as exc:
        logger.warning("⚠️ Failed to delete %s: %s", file_path, exc)

    return False


def safe_remove_many(file_paths: List[Optional[str]]) -> int:
    deleted = 0

    for file_path in file_paths:
        if safe_remove(file_path):
            deleted += 1

    return deleted


def get_unique_filename(
    prefix: str,
    extension: str = ".mp3",
) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}{extension}"


# ============================================================
# 🔍 التحقق من الملفات
# ============================================================

def is_audio_file(
    file_name: Optional[str],
    mime_type: Optional[str],
) -> bool:

    if file_name:
        ext = Path(file_name).suffix.lower()

        if ext in SUPPORTED_AUDIO_EXTENSIONS:
            return True

    if mime_type:
        if (
            mime_type in SUPPORTED_AUDIO_MIME_TYPES
            or mime_type.startswith("audio/")
        ):
            return True

    return False


def is_image_file(
    file_name: Optional[str],
    mime_type: Optional[str],
) -> bool:

    if file_name:
        ext = Path(file_name).suffix.lower()

        if ext in IMAGE_EXTENSIONS:
            return True

    if mime_type and mime_type.startswith("image/"):
        return True

    return False


def get_file_size_mb(size_bytes: int) -> float:
    return size_bytes / (1024 * 1024)


# ============================================================
# 🎬 FFmpeg
# ============================================================

class FFmpegManager:

    DEFAULT_TIMEOUT = 1800
    DEFAULT_QUALITY = "192k"
    SAMPLE_RATE = "44100"
    AUDIO_CHANNELS = 2

    @staticmethod
    async def run(
        cmd: List[str],
        timeout: int = DEFAULT_TIMEOUT,
        description: str = "FFmpeg process",
    ) -> Tuple[int, bytes, bytes]:

        process = None

        try:
            async with _semaphore:

                logger.info(
                    "▶️ %s",
                    description,
                )

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )

                if process.returncode == 0:
                    logger.info(
                        "✅ %s completed",
                        description,
                    )
                else:
                    error_text = stderr.decode(
                        "utf-8",
                        errors="ignore",
                    )

                    logger.error(
                        "❌ %s failed: %s",
                        description,
                        error_text[-1000:],
                    )

                return (
                    process.returncode,
                    stdout,
                    stderr,
                )

        except asyncio.TimeoutError:

            if process:
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass

            logger.error(
                "⏰ %s timed out",
                description,
            )

            return (
                -1,
                b"",
                b"Timeout",
            )

        except Exception as exc:

            logger.exception(
                "❌ %s error: %s",
                description,
                exc,
            )

            return (
                -1,
                b"",
                str(exc).encode(),
            )

    # --------------------------------------------------------

    @classmethod
    def build_convert_cmd(
        cls,
        input_path: str,
        output_path: str,
        quality: str = DEFAULT_QUALITY,
    ) -> List[str]:

        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            input_path,
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            quality,
            "-ac",
            str(cls.AUDIO_CHANNELS),
            "-ar",
            cls.SAMPLE_RATE,
            "-y",
            output_path,
        ]

    # --------------------------------------------------------

    @classmethod
    def build_merge_cover_cmd(
        cls,
        audio_path: str,
        cover_path: str,
        output_path: str,
        title: str,
        artist: str,
        quality: str = DEFAULT_QUALITY,
    ) -> List[str]:

        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",

            "-i",
            audio_path,

            "-i",
            cover_path,

            "-map",
            "0:a:0",

            "-map",
            "1:0",

            "-c:a",
            "libmp3lame",

            "-b:a",
            quality,

            "-ar",
            cls.SAMPLE_RATE,

            "-id3v2_version",
            "3",

            "-metadata",
            f"title={title}",

            "-metadata",
            f"artist={artist}",

            "-y",
            output_path,
        ]

    # --------------------------------------------------------

    @classmethod
    def build_extract_audio_cmd(
        cls,
        video_path: str,
        output_path: str,
        quality: str = DEFAULT_QUALITY,
    ) -> List[str]:

        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",

            "-i",
            video_path,

            "-vn",

            "-c:a",
            "libmp3lame",

            "-ac",
            str(cls.AUDIO_CHANNELS),

            "-b:a",
            quality,

            "-ar",
            cls.SAMPLE_RATE,

            "-y",
            output_path,
        ]

    # --------------------------------------------------------

    @classmethod
    async def convert_to_mp3(
        cls,
        input_path: str,
        output_path: str,
        quality: str = DEFAULT_QUALITY,
    ) -> bool:

        cmd = cls.build_convert_cmd(
            input_path,
            output_path,
            quality,
        )

        return_code, _, _ = await cls.run(
            cmd,
            description="تحويل الصوت إلى MP3",
        )

        return (
            return_code == 0
            and os.path.exists(output_path)
            and os.path.getsize(output_path) > 0
        )

    # --------------------------------------------------------

    @classmethod
    async def compress_audio(
        cls,
        input_path: str,
        output_path: str,
        target_size_mb: int = 95,
    ) -> bool:

        if not os.path.exists(input_path):
            return False

        current_size = (
            os.path.getsize(input_path)
            / (1024 * 1024)
        )

        if current_size <= target_size_mb:
            shutil.copy2(
                input_path,
                output_path,
            )
            return True

        duration = get_audio_duration(input_path)

        quality = calculate_quality(
            target_size_mb,
            duration,
        )

        cmd = cls.build_convert_cmd(
            input_path,
            output_path,
            quality,
        )

        return_code, _, _ = await cls.run(
            cmd,
            description="ضغط الصوت",
        )

        if (
            return_code == 0
            and os.path.exists(output_path)
        ):
            new_size = (
                os.path.getsize(output_path)
                / (1024 * 1024)
            )

            if new_size <= target_size_mb:
                return True

        # محاولة أخيرة بجودة منخفضة
        fallback_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            input_path,
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "32k",
            "-ac",
            "1",
            "-ar",
            "22050",
            "-y",
            output_path,
        ]

        return_code, _, _ = await cls.run(
            fallback_cmd,
            description="ضغط الصوت الاحتياطي",
        )

        return (
            return_code == 0
            and os.path.exists(output_path)
        )


# ============================================================
# 📊 معلومات الصوت
# ============================================================

def get_audio_duration(file_path: str) -> float:

    try:

        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode == 0 and result.stdout:
            return float(
                result.stdout.strip()
            )

    except Exception as exc:
        logger.debug(
            "Failed reading duration: %s",
            exc,
        )

    return 0.0


def calculate_quality(
    target_size_mb: int,
    duration_seconds: float,
) -> str:

    if duration_seconds <= 0:
        return "128k"

    target_bitrate = (
        target_size_mb
        * 8
        * 1024
        / duration_seconds
    )

    for bitrate, quality in AUDIO_QUALITIES:

        if target_bitrate >= bitrate:
            return quality

    return "32k"


def get_audio_metadata(
    file_path: str,
) -> Dict[str, Any]:

    metadata = {
        "duration": 0.0,
        "size_mb": 0.0,
        "bitrate": "غير معروف",
        "sample_rate": "غير معروف",
    }

    try:

        metadata["duration"] = get_audio_duration(
            file_path
        )

        metadata["size_mb"] = (
            os.path.getsize(file_path)
            / (1024 * 1024)
        )

        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=bit_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode == 0 and result.stdout:

            try:
                bitrate = int(
                    float(result.stdout.strip())
                )

                metadata["bitrate"] = (
                    f"{bitrate // 1000}k"
                )

            except ValueError:
                pass

    except Exception as exc:

        logger.debug(
            "Metadata error: %s",
            exc,
        )

    return metadata


def format_duration(
    seconds: float,
) -> str:

    if seconds <= 0:
        return "0ث"

    minutes = int(seconds // 60)
    secs = int(seconds % 60)

    if minutes:
        return f"{minutes}د {secs}ث"

    return f"{secs}ث"


# ============================================================
# 🚀 START
# ============================================================

async def start_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if await is_maintenance(
        update,
        context,
    ):
        return

    user = update.effective_user

    if not user or not update.message:
        return

    if not await check_subscription(
        user.id,
        context,
    ):

        from utils import get_force_channels, get_force_message
        await update.message.reply_text(
            get_force_message(),
            reply_markup=subscription_keyboard(get_force_channels()),
        )

        return

    add_user(
        user.id,
        user.first_name or "مستخدم",
    )

    await update.message.reply_text(
        f"🚀 أهلاً بك {user.first_name} "
        "في بوت الخدمات الصوتية!\n\n"
        "اختر ما تريد فعله من الأزرار أدناه:",
        reply_markup=main_menu_keyboard(
            user.id == OWNER_ID
        ),
    )


# ============================================================
# 🔘 CALLBACK
# ============================================================

async def callback_query_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    if not query or not update.effective_user:
        return

    data = query.data or ""
    user_id = query.from_user.id

    if data == "sub_check":
        if await check_subscription(user_id, context):
            await query.answer("✅ تم التحقق من اشتراكك. أرسل /start للمتابعة.", show_alert=True)
        else:
            await query.answer("❌ لم تشترك في جميع القنوات المطلوبة بعد.", show_alert=True)
        return

    await query.answer()

    # --------------------------------------------------------
    # تعديل أغنية موجودة
    # --------------------------------------------------------

    if data == "mysong_edit":

        context.user_data.clear()

        context.user_data.update(
            {
                "mode": "mysong_edit",
                "step": "waiting_for_audio",
            }
        )

        await query.edit_message_text(
            "🎵 تعديل أغنية موجودة\n\n"
            "📤 أرسل لي الآن الملف الصوتي."
        )

    # --------------------------------------------------------
    # استخراج الصوت
    # --------------------------------------------------------

    elif data == "mysong_extract":

        context.user_data.clear()

        context.user_data.update(
            {
                "mode": "mysong_extract",
                "step": "waiting_for_video",
            }
        )

        await query.edit_message_text(
            "🎬 استخراج صوت من فيديو + إضافة صورة\n\n"
            "📤 أرسل لي الآن ملف الفيديو."
        )

    # --------------------------------------------------------
    # أغنية جديدة
    # --------------------------------------------------------

    elif data == "mysong_new":

        context.user_data.clear()

        context.user_data.update(
            {
                "mode": "mysong_new",
                "step": "waiting_for_audio",
            }
        )

        await query.edit_message_text(
            "🆕 رفع ملف صوتي جديد + صورة\n\n"
            "📤 أرسل لي الآن الملف الصوتي."
        )

    # --------------------------------------------------------
    # الجودة
    # --------------------------------------------------------

    elif data.startswith("q_"):

        parts = data.split("_", 2)

        if len(parts) != 3:
            return

        quality = f"{parts[1]}k"
        action = parts[2]

        if action not in {"edit", "extract"}:
            return

        context.user_data.update(
            {
                "selected_quality": quality,
                "action_type": action,
            }
        )

        if action == "edit":

            text = (
                f"✅ تم اختيار جودة {quality}.\n\n"
                "📤 أرسل الآن الملف الصوتي:"
            )

        else:

            text = (
                f"✅ تم اختيار جودة {quality}.\n\n"
                "📤 أرسل الآن ملف الفيديو:"
            )

        await query.edit_message_text(text)

    # --------------------------------------------------------
    # إلغاء
    # --------------------------------------------------------

    elif data == "cancel_action":

        context.user_data.clear()

        await query.edit_message_text(
            "❌ تم إلغاء العملية."
        )

    # --------------------------------------------------------
    # إحصائيات
    # --------------------------------------------------------

    elif data == "my_stats":

        with sqlite3.connect(DB_FILE) as conn:

            count = conn.execute(
                "SELECT COUNT(*) FROM files "
                "WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]

        await query.edit_message_text(
            "📊 إحصائياتك الشخصية\n\n"
            f"✅ عدد الأغاني المعالجة: {count}"
        )


# ============================================================
# 📁 MEDIA HANDLER
# ============================================================

async def media_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if await is_maintenance(
        update,
        context,
    ):
        return

    if not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id

    mode = context.user_data.get("mode")
    step = context.user_data.get("step")

    temp_files: List[str] = []

    try:

        if mode and step:

            if (
                step == "waiting_for_audio"
                and mode in {
                    "mysong_edit",
                    "mysong_new",
                }
            ):

                await _handle_audio_upload(
                    update,
                    context,
                    user_id,
                    temp_files,
                )

                return

            if (
                step == "waiting_for_video"
                and mode == "mysong_extract"
            ):

                await _handle_video_upload(
                    update,
                    context,
                    user_id,
                    temp_files,
                )

                return

            await update.message.reply_text(
                "❌ الرجاء إرسال الملف المطلوب."
            )

            return

        await _handle_normal_mode(
            update,
            context,
            user_id,
            temp_files,
        )

    except Exception as exc:

        logger.exception(
            "media_handler error for %s: %s",
            user_id,
            exc,
        )

        await update.message.reply_text(
            "⚠️ حدث خطأ أثناء تنفيذ العملية.\n"
            "حاول مرة أخرى."
        )

    finally:

        keep_files = {
            context.user_data.get("audio_path"),
            context.user_data.get("file_path"),
        }

        for file_path in temp_files:

            if file_path not in keep_files:
                safe_remove(file_path)


# ============================================================
# 🎵 رفع الصوت
# ============================================================

async def _handle_audio_upload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    temp_files: List[str],
) -> None:

    message = update.message

    file_obj = None
    file_name = None
    mime_type = None

    if message.audio:

        file_obj = message.audio
        file_name = (
            file_obj.file_name
            or "audio.mp3"
        )
        mime_type = (
            file_obj.mime_type
            or "audio/mpeg"
        )

    elif message.document:

        doc = message.document

        file_name = doc.file_name or ""
        mime_type = doc.mime_type or ""

        if not is_audio_file(
            file_name,
            mime_type,
        ):

            await message.reply_text(
                "❌ الملف المرسل ليس ملفاً صوتياً مدعوماً."
            )

            return

        file_obj = doc

    if not file_obj:

        await message.reply_text(
            "❌ من فضلك أرسل ملفاً صوتياً."
        )

        return

    file_size = file_obj.file_size or 0

    if file_size > MAX_FILE_SIZE:

        await message.reply_text(
            "❌ حجم الملف أكبر من الحد المسموح.\n\n"
            f"📦 الحد الأقصى: "
            f"{MAX_FILE_SIZE / (1024 * 1024):.0f} MB"
        )

        return

    wait_msg = await message.reply_text(
        "⏳ جاري تحميل الملف..."
    )

    tg_file = await file_obj.get_file()

    ext = (
        Path(file_name).suffix.lower()
        or ".mp3"
    )

    original_path = get_unique_filename(
        f"original_{user_id}",
        ext,
    )

    temp_files.append(original_path)

    await tg_file.download_to_drive(
        original_path
    )

    if (
        not os.path.exists(original_path)
        or os.path.getsize(original_path) == 0
    ):

        await wait_msg.edit_text(
            "❌ فشل تحميل الملف."
        )

        return

    metadata = get_audio_metadata(
        original_path
    )

    await wait_msg.edit_text(
        f"📁 {Path(file_name).name}\n"
        f"⏱️ {format_duration(metadata['duration'])}\n"
        f"📦 {metadata['size_mb']:.1f} MB\n\n"
        "✅ تم تحميل الملف."
    )

    audio_path = original_path

    # --------------------------------------------------------
    # تحويل إلى MP3
    # --------------------------------------------------------

    if not original_path.lower().endswith(".mp3"):

        await wait_msg.edit_text(
            "🔄 جاري تحويل الملف إلى MP3..."
        )

        mp3_path = get_unique_filename(
            f"converted_{user_id}"
        )

        temp_files.append(mp3_path)

        quality = context.user_data.get(
            "selected_quality",
            "192k",
        )

        success = await FFmpegManager.convert_to_mp3(
            original_path,
            mp3_path,
            quality,
        )

        if not success:

            await wait_msg.edit_text(
                "❌ فشل تحويل الملف إلى MP3."
            )

            return

        safe_remove(original_path)

        audio_path = mp3_path

    # --------------------------------------------------------
    # إذا تجاوز 100MB نحاول الضغط
    # --------------------------------------------------------

    if (
        os.path.exists(audio_path)
        and os.path.getsize(audio_path)
        > MAX_FILE_SIZE
    ):

        await wait_msg.edit_text(
            "⏳ الملف أكبر من 100MB، جاري ضغطه..."
        )

        compressed_path = get_unique_filename(
            f"compressed_{user_id}"
        )

        temp_files.append(
            compressed_path
        )

        success = await FFmpegManager.compress_audio(
            audio_path,
            compressed_path,
            95,
        )

        if success:

            safe_remove(audio_path)
            audio_path = compressed_path

        else:

            await wait_msg.edit_text(
                "❌ تعذر ضغط الملف إلى الحجم المطلوب."
            )

            return

    context.user_data["audio_path"] = audio_path
    context.user_data["step"] = "waiting_for_title"

    await wait_msg.edit_text(
        "📝 أرسل الآن اسم الأغنية:"
    )


# ============================================================
# 🎬 رفع الفيديو واستخراج الصوت
# ============================================================

async def _handle_video_upload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    temp_files: List[str],
) -> None:

    message = update.message

    file_obj = None
    file_name = "video.mp4"

    if message.video:

        file_obj = message.video

        file_name = (
            file_obj.file_name
            or "video.mp4"
        )

    elif message.document:

        doc = message.document

        name = doc.file_name or ""

        mime = doc.mime_type or ""

        if mime.startswith("video/") or Path(
            name
        ).suffix.lower() in {
            ".mp4",
            ".mkv",
            ".mov",
            ".avi",
            ".webm",
            ".m4v",
            ".3gp",
        }:

            file_obj = doc
            file_name = name or "video.mp4"

    if not file_obj:

        await message.reply_text(
            "❌ من فضلك أرسل ملف فيديو."
        )

        return

    file_size = file_obj.file_size or 0

    if file_size > MAX_FILE_SIZE:

        await message.reply_text(
            "❌ حجم الفيديو أكبر من الحد المسموح.\n\n"
            f"📦 الحد الأقصى: "
            f"{MAX_FILE_SIZE / (1024 * 1024):.0f} MB"
        )

        return

    wait_msg = await message.reply_text(
        "⏳ جاري تحميل الفيديو..."
    )

    tg_file = await file_obj.get_file()

    ext = (
        Path(file_name).suffix.lower()
        or ".mp4"
    )

    video_path = get_unique_filename(
        f"video_{user_id}",
        ext,
    )

    temp_files.append(video_path)

    await tg_file.download_to_drive(
        video_path
    )

    if (
        not os.path.exists(video_path)
        or os.path.getsize(video_path) == 0
    ):

        await wait_msg.edit_text(
            "❌ فشل تحميل الفيديو."
        )

        return

    await wait_msg.edit_text(
        "🎬 جاري استخراج الصوت من الفيديو..."
    )

    audio_path = get_unique_filename(
        f"extracted_{user_id}"
    )

    temp_files.append(audio_path)

    quality = context.user_data.get(
        "selected_quality",
        "192k",
    )

    cmd = FFmpegManager.build_extract_audio_cmd(
        video_path,
        audio_path,
        quality,
    )

    return_code, _, _ = await FFmpegManager.run(
        cmd,
        description="استخراج الصوت من الفيديو",
    )

    safe_remove(video_path)

    if return_code != 0:

        await wait_msg.edit_text(
            "❌ حدث خطأ أثناء استخراج الصوت."
        )

        return

    if (
        not os.path.exists(audio_path)
        or os.path.getsize(audio_path) == 0
    ):

        await wait_msg.edit_text(
            "❌ لم يتم استخراج أي صوت من الفيديو."
        )

        return

    # --------------------------------------------------------
    # ضغط إذا تجاوز 100MB
    # --------------------------------------------------------

    if (
        os.path.getsize(audio_path)
        > MAX_FILE_SIZE
    ):

        await wait_msg.edit_text(
            "⏳ الصوت أكبر من 100MB، جاري ضغطه..."
        )

        compressed_path = get_unique_filename(
            f"compressed_{user_id}"
        )

        temp_files.append(
            compressed_path
        )

        success = await FFmpegManager.compress_audio(
            audio_path,
            compressed_path,
            95,
        )

        if success:

            safe_remove(audio_path)
            audio_path = compressed_path

        else:

            await wait_msg.edit_text(
                "❌ تعذر ضغط الصوت."
            )

            return

    context.user_data["audio_path"] = audio_path
    context.user_data["step"] = "waiting_for_title"

    await wait_msg.edit_text(
        "✅ تم استخراج الصوت بنجاح.\n\n"
        "📝 أرسل الآن اسم الأغنية:"
    )


# ============================================================
# 🎵 الوضع العادي
# ============================================================

async def _handle_normal_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    temp_files: List[str],
) -> None:

    message = update.message

    action_type = context.user_data.get(
        "action_type"
    )

    quality = context.user_data.get(
        "selected_quality",
        "192k",
    )

    if not action_type:
        return

    file_obj = None
    file_name = ""

    # --------------------------------------------------------
    # صوت
    # --------------------------------------------------------

    if action_type == "edit":

        if message.audio:

            file_obj = message.audio

            file_name = (
                file_obj.file_name
                or "audio.mp3"
            )

        elif message.document:

            doc = message.document

            file_name = (
                doc.file_name
                or ""
            )

            if is_audio_file(
                file_name,
                doc.mime_type or "",
            ):

                file_obj = doc

    # --------------------------------------------------------
    # فيديو
    # --------------------------------------------------------

    elif action_type == "extract":

        if message.video:

            file_obj = message.video

            file_name = (
                file_obj.file_name
                or "video.mp4"
            )

        elif message.document:

            doc = message.document

            name = doc.file_name or ""
            mime = doc.mime_type or ""

            if (
                mime.startswith("video/")
                or Path(name).suffix.lower()
                in {
                    ".mp4",
                    ".mkv",
                    ".mov",
                    ".avi",
                    ".webm",
                    ".m4v",
                }
            ):

                file_obj = doc
                file_name = name

    if not file_obj:

        await message.reply_text(
            "❌ الرجاء إرسال الملف المطلوب."
        )

        return

    file_size = file_obj.file_size or 0

    if file_size > MAX_FILE_SIZE:

        await message.reply_text(
            "❌ حجم الملف أكبر من الحد المسموح.\n\n"
            f"📦 الحد الأقصى: "
            f"{MAX_FILE_SIZE / (1024 * 1024):.0f} MB"
        )

        context.user_data.clear()

        return

    wait_msg = await message.reply_text(
        "⏳ جاري تحميل الملف..."
    )

    tg_file = await file_obj.get_file()

    ext = (
        Path(file_name).suffix.lower()
        if file_name
        else ".mp4"
    )

    input_path = get_unique_filename(
        f"input_{user_id}",
        ext,
    )

    output_path = get_unique_filename(
        f"output_{user_id}"
    )

    temp_files.extend(
        [
            input_path,
            output_path,
        ]
    )

    await tg_file.download_to_drive(
        input_path
    )

    if action_type == "extract":

        await wait_msg.edit_text(
            "🎬 جاري استخراج الصوت..."
        )

        cmd = FFmpegManager.build_extract_audio_cmd(
            input_path,
            output_path,
            quality,
        )

    else:

        await wait_msg.edit_text(
            "🎵 جاري تحويل الصوت..."
        )

        cmd = FFmpegManager.build_convert_cmd(
            input_path,
            output_path,
            quality,
        )

    return_code, _, _ = await FFmpegManager.run(
        cmd,
        description="معالجة الملف",
    )

    safe_remove(input_path)

    if return_code != 0:

        await wait_msg.edit_text(
            "❌ حدث خطأ أثناء معالجة الملف."
        )

        return

    if (
        not os.path.exists(output_path)
        or os.path.getsize(output_path) == 0
    ):

        await wait_msg.edit_text(
            "❌ لم يتم إنشاء الملف الناتج."
        )

        return

    # --------------------------------------------------------
    # ضغط الناتج إذا تجاوز 100MB
    # --------------------------------------------------------

    if (
        os.path.getsize(output_path)
        > MAX_FILE_SIZE
    ):

        await wait_msg.edit_text(
            "⏳ جاري ضغط الملف النهائي..."
        )

        compressed_path = get_unique_filename(
            f"compressed_{user_id}"
        )

        temp_files.append(
            compressed_path
        )

        success = await FFmpegManager.compress_audio(
            output_path,
            compressed_path,
            95,
        )

        if success:

            safe_remove(output_path)
            output_path = compressed_path

        else:

            await wait_msg.edit_text(
                "❌ تعذر ضغط الملف النهائي."
            )

            return

    context.user_data["file_path"] = output_path
    context.user_data["step"] = "title"

    await wait_msg.edit_text(
        "📝 أرسل اسم الأغنية:"
    )


# ============================================================
# 🖼️ الصور
# ============================================================

async def photo_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if await is_maintenance(
        update,
        context,
    ):
        return

    if not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id

    mode = context.user_data.get("mode")
    step = context.user_data.get("step")

    if (
        not mode
        or step != "waiting_for_cover"
    ):

        await update.message.reply_text(
            "❌ لست في وضع إضافة صورة."
        )

        return

    temp_files: List[str] = []
    wait_msg = None

    try:

        wait_msg = await update.message.reply_text(
            "🖼️ جاري تحميل الصورة..."
        )

        cover_path = get_unique_filename(
            f"cover_{user_id}",
            ".jpg",
        )

        audio_path = context.user_data.get(
            "audio_path"
        )

        temp_files.append(
            cover_path
        )

        # ----------------------------------------------------
        # صورة Telegram
        # ----------------------------------------------------

        if update.message.photo:

            photo = update.message.photo[-1]

            tg_photo = await photo.get_file()

            await tg_photo.download_to_drive(
                cover_path
            )

        # ----------------------------------------------------
        # صورة كملف
        # ----------------------------------------------------

        elif update.message.document:

            doc = update.message.document

            file_name = (
                doc.file_name
                or ""
            )

            mime_type = (
                doc.mime_type
                or ""
            )

            if not is_image_file(
                file_name,
                mime_type,
            ):

                await wait_msg.edit_text(
                    "❌ الملف المرسل ليس صورة."
                )

                return

            tg_doc = await doc.get_file()

            ext = (
                Path(file_name).suffix.lower()
                or ".jpg"
            )

            new_cover_path = (
                str(
                    Path(cover_path).with_suffix(
                        ext
                    )
                )
            )

            temp_files.remove(
                cover_path
            )

            cover_path = new_cover_path

            temp_files.append(
                cover_path
            )

            await tg_doc.download_to_drive(
                cover_path
            )

        else:

            await wait_msg.edit_text(
                "❌ لم ترسل صورة."
            )

            return

        if (
            not audio_path
            or not os.path.exists(audio_path)
        ):

            await wait_msg.edit_text(
                "❌ الملف الصوتي غير موجود."
            )

            return

        title = context.user_data.get(
            "title",
            "غير معروف",
        )

        artist = context.user_data.get(
            "artist",
            "غير معروف",
        )

        final_path = get_unique_filename(
            f"final_{user_id}"
        )

        temp_files.append(
            final_path
        )

        await wait_msg.edit_text(
            "🎵 جاري دمج الصورة مع الصوت..."
        )

        quality = context.user_data.get(
            "selected_quality",
            "192k",
        )

        cmd = FFmpegManager.build_merge_cover_cmd(
            audio_path,
            cover_path,
            final_path,
            title,
            artist,
            quality,
        )

        return_code, _, _ = await FFmpegManager.run(
            cmd,
            description="دمج الغلاف مع الصوت",
        )

        if return_code != 0:

            await wait_msg.edit_text(
                "❌ حدث خطأ أثناء دمج الصورة."
            )

            return

        if (
            not os.path.exists(final_path)
            or os.path.getsize(final_path) == 0
        ):

            await wait_msg.edit_text(
                "❌ فشل إنشاء الأغنية النهائية."
            )

            return

        # ----------------------------------------------------
        # ضغط إذا تجاوز 100MB
        # ----------------------------------------------------

        if (
            os.path.getsize(final_path)
            > MAX_FILE_SIZE
        ):

            await wait_msg.edit_text(
                "⏳ الملف النهائي أكبر من 100MB، "
                "جاري ضغطه..."
            )

            compressed_path = get_unique_filename(
                f"compressed_final_{user_id}"
            )

            temp_files.append(
                compressed_path
            )

            success = await FFmpegManager.compress_audio(
                final_path,
                compressed_path,
                95,
            )

            if success:

                safe_remove(final_path)

                final_path = compressed_path

            else:

                await wait_msg.edit_text(
                    "❌ تعذر ضغط الملف النهائي."
                )

                return

        # ----------------------------------------------------
        # إرسال الأغنية
        # ----------------------------------------------------

        await wait_msg.edit_text(
            "📤 جاري إرسال الأغنية..."
        )

        with open(
            final_path,
            "rb",
        ) as audio_file:

            await update.message.reply_audio(
                audio=audio_file,
                title=title,
                performer=artist,
                caption="✅ تم إنشاء الأغنية بنجاح!",
                reply_markup=main_menu_keyboard(
                    user_id == OWNER_ID
                ),
            )

        add_file_record(
            user_id,
            title,
            artist,
        )

        await wait_msg.delete()

    except Exception as exc:

        logger.exception(
            "photo_handler error for %s: %s",
            user_id,
            exc,
        )

        if wait_msg:

            try:
                await wait_msg.edit_text(
                    "⚠️ حدث خطأ أثناء إنشاء الأغنية."
                )
            except Exception:
                pass

    finally:

        safe_remove_many(
            temp_files
        )

        context.user_data.clear()


# ============================================================
# 📝 TEXT HANDLER
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if (
        not update.message
        or not update.message.text
        or not update.effective_user
    ):
        return

    user_text = update.message.text.strip()

    user_id = update.effective_user.id

    try:

        # ====================================================
        # 📢 Broadcast
        # ====================================================

        if (
            context.user_data.get(
                "admin_step"
            )
            == "broadcasting"
        ):

            if user_id != OWNER_ID:

                context.user_data[
                    "admin_step"
                ] = None

                return

            with sqlite3.connect(DB_FILE) as conn:

                users = conn.execute(
                    "SELECT user_id FROM users"
                ).fetchall()

            success = 0

            for row in users:

                try:

                    await context.bot.send_message(
                        chat_id=row[0],
                        text=(
                            "📢 إذاعة من المطور\n\n"
                            f"{user_text}"
                        ),
                    )

                    success += 1

                except Exception:
                    pass

            context.user_data[
                "admin_step"
            ] = None

            await update.message.reply_text(
                f"✅ تمت الإذاعة لـ {success} مستخدم."
            )

            return

        # ====================================================
        # 🔘 الأزرار الرئيسية
        # ====================================================

        if user_text == "▶️ تشغيل البوت":

            await start_handler(
                update,
                context,
            )

            return

        # ----------------------------------------------------
        # 🎵 تعديل الأغنية
        # ----------------------------------------------------

        if user_text == "🎵 تعديل الأغنية":

            from keyboards import quality_keyboard

            context.user_data.clear()

            await update.message.reply_text(
                "🎵 تعديل الأغنية\n\n"
                "اختر جودة الصوت:",
                reply_markup=quality_keyboard(
                    "edit"
                ),
            )

            return

        # ----------------------------------------------------
        # 🎬 استخراج الصوت
        # ----------------------------------------------------

        if user_text == "🎬 استخراج الصوت":

            from keyboards import quality_keyboard

            context.user_data.clear()

            await update.message.reply_text(
                "🎬 استخراج الصوت من فيديو\n\n"
                "اختر جودة الصوت:",
                reply_markup=quality_keyboard(
                    "extract"
                ),
            )

            return

# ----------------------------------------------------
# 🖼️ إنشاء أغنية كاملة
# ----------------------------------------------------
# ----------------------------------------------------
# 🖼️ إنشاء أغنية كاملة
# ----------------------------------------------------

        # ----------------------------------------------------
        # 🖼️ إنشاء أغنية كاملة
        # ----------------------------------------------------

        if user_text == "🖼️ إنشاء أغنية كاملة (اسم + صورة + صوت)":

            from keyboards import my_song_menu_keyboard

            context.user_data.clear()

            await update.message.reply_text(
                "🖼️ إنشاء أغنية كاملة\n\n"
                "اختر ما تريد:",
                reply_markup=my_song_menu_keyboard(),
            )

            return

        if user_text == "📊 إحصائياتي":

            with sqlite3.connect(DB_FILE) as conn:

                count = conn.execute(
                    "SELECT COUNT(*) FROM files "
                    "WHERE user_id = ?",
                    (user_id,),
                ).fetchone()[0]

            await update.message.reply_text(
                "📊 إحصائياتك الشخصية\n\n"
                f"✅ عدد الأغاني المعالجة: {count}"
            )

            return

        # ----------------------------------------------------
        # ❓ المساعدة
        # ----------------------------------------------------

        if user_text == "❓ المساعدة":

            await update.message.reply_text(
                "ℹ️ طريقة استخدام البوت\n\n"

                "🎵 تعديل الأغنية\n"
                "رفع ملف صوتي ثم إدخال الاسم والفنان.\n\n"

                "🎬 استخراج الصوت\n"
                "رفع فيديو لاستخراج الصوت منه.\n\n"

                "🖼️ إنشاء أغنية كاملة\n"
                "صوت + اسم + فنان + صورة.\n\n"

                "📊 إحصائياتي\n"
                "عرض عدد الأغاني التي عالجتها.\n\n"

                f"📦 الحد الأقصى للملف: "
                f"{MAX_FILE_SIZE / (1024 * 1024):.0f} MB"
            )

            return

        # ----------------------------------------------------
        # 🛠 لوحة التحكم
        # ----------------------------------------------------

        if user_text == "🛠 لوحة التحكم":

            if user_id != OWNER_ID:

                await update.message.reply_text(
                    "❌ هذه الخاصية متاحة للمطور فقط."
                )

                return

            from admin_panel import panel_handler

            await panel_handler(
                update,
                context,
            )

            return

        # ====================================================
        # 📝 وضع إنشاء الأغنية
        # ====================================================

        if context.user_data.get("mode"):

            await _handle_mysong_text(
                update,
                context,
            )

            return

        # ====================================================
        # 📝 وضع التعديل
        # ====================================================

        if "file_path" in context.user_data:

            await _handle_edit_text(
                update,
                context,
            )

            return

        await update.message.reply_text(
            "❓ عذراً، لم أفهم طلبك.\n"
            "الرجاء استخدام الأزرار."
        )

    except Exception as exc:

        logger.exception(
            "text_handler error for %s: %s",
            user_id,
            exc,
        )

        await update.message.reply_text(
            "⚠️ حدث خطأ أثناء تنفيذ العملية."
        )


# ============================================================
# 📝 نصوص إنشاء الأغنية
# ============================================================

async def _handle_mysong_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    step = context.user_data.get(
        "step"
    )

    user_text = update.message.text.strip()

    # --------------------------------------------------------
    # اسم الأغنية
    # --------------------------------------------------------

    if step == "waiting_for_title":

        if not user_text or len(user_text) > 100:

            await update.message.reply_text(
                "❌ اسم الأغنية يجب أن يكون "
                "بين 1 و100 حرف."
            )

            return

        context.user_data["title"] = user_text

        context.user_data[
            "step"
        ] = "waiting_for_artist"

        await update.message.reply_text(
            "🎤 أرسل اسم الفنان:"
        )

        return

    # --------------------------------------------------------
    # الفنان
    # --------------------------------------------------------

    if step == "waiting_for_artist":

        if not user_text or len(user_text) > 100:

            await update.message.reply_text(
                "❌ اسم الفنان يجب أن يكون "
                "بين 1 و100 حرف."
            )

            return

        context.user_data["artist"] = user_text

        context.user_data[
            "step"
        ] = "waiting_for_cover"

        await update.message.reply_text(
            "🖼️ أرسل صورة الغلاف:"
        )

        return

    # --------------------------------------------------------
    # الصورة
    # --------------------------------------------------------

    if step == "waiting_for_cover":

        await update.message.reply_text(
            "🖼️ أنا في انتظار صورة الغلاف."
        )


# ============================================================
# 📝 نصوص تعديل الأغنية
# ============================================================

async def _handle_edit_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    step = context.user_data.get(
        "step"
    )

    file_path = context.user_data.get(
        "file_path"
    )

    user_id = update.effective_user.id

    user_text = update.message.text.strip()

    if not file_path or not os.path.exists(
        file_path
    ):

        context.user_data.clear()

        await update.message.reply_text(
            "❌ الملف لم يعد موجوداً. "
            "ابدأ العملية من جديد."
        )

        return

    # --------------------------------------------------------
    # العنوان
    # --------------------------------------------------------

    if step == "title":

        if not user_text or len(user_text) > 100:

            await update.message.reply_text(
                "❌ اسم الأغنية يجب أن يكون "
                "بين 1 و100 حرف."
            )

            return

        context.user_data[
            "title"
        ] = user_text

        context.user_data[
            "step"
        ] = "artist"

        await update.message.reply_text(
            "🎤 أرسل اسم الفنان:"
        )

        return

    # --------------------------------------------------------
    # الفنان
    # --------------------------------------------------------

    if step != "artist":
        return

    artist = user_text

    if not artist or len(artist) > 100:

        await update.message.reply_text(
            "❌ اسم الفنان يجب أن يكون "
            "بين 1 و100 حرف."
        )

        return

    title = context.user_data.get(
        "title",
        "غير معروف",
    )

    temp_files: List[str] = []

    try:

        final_path = get_unique_filename(
            f"final_{user_id}"
        )

        temp_files.append(
            final_path
        )

        await update.message.reply_text(
            "🎵 جاري معالجة الأغنية..."
        )

        cover = await get_channel_cover(
            context
        )

        quality = context.user_data.get(
            "selected_quality",
            "192k",
        )

        if cover and os.path.exists(
            cover
        ):

            cmd = FFmpegManager.build_merge_cover_cmd(
                file_path,
                cover,
                final_path,
                title,
                artist,
                quality,
            )

        else:

            cmd = FFmpegManager.build_convert_cmd(
                file_path,
                final_path,
                quality,
            )

            cmd.extend(
                [
                    "-metadata",
                    f"title={title}",
                    "-metadata",
                    f"artist={artist}",
                ]
            )

        return_code, _, _ = await FFmpegManager.run(
            cmd,
            description="تعديل الأغنية",
        )

        if return_code != 0:

            await update.message.reply_text(
                "❌ حدث خطأ أثناء معالجة الأغنية."
            )

            return

        if (
            not os.path.exists(final_path)
            or os.path.getsize(final_path) == 0
        ):

            await update.message.reply_text(
                "❌ فشل إنشاء الملف النهائي."
            )

            return

        # ----------------------------------------------------
        # ضغط إلى أقل من 100MB
        # ----------------------------------------------------

        if (
            os.path.getsize(final_path)
            > MAX_FILE_SIZE
        ):

            await update.message.reply_text(
                "⏳ الملف النهائي أكبر من 100MB، "
                "جاري ضغطه..."
            )

            compressed_path = get_unique_filename(
                f"compressed_final_{user_id}"
            )

            temp_files.append(
                compressed_path
            )

            success = await FFmpegManager.compress_audio(
                final_path,
                compressed_path,
                95,
            )

            if success:

                safe_remove(final_path)

                final_path = compressed_path

            else:

                await update.message.reply_text(
                    "❌ تعذر ضغط الملف."
                )

                return

        await update.message.reply_text(
            "📤 جاري إرسال الأغنية..."
        )

        with open(
            final_path,
            "rb",
        ) as audio_file:

            await update.message.reply_audio(
                audio=audio_file,
                title=title,
                performer=artist,
                caption="✅ تم تعديل الأغنية بنجاح!",
                reply_markup=main_menu_keyboard(
                    user_id == OWNER_ID
                ),
            )

        add_file_record(
            user_id,
            title,
            artist,
        )

    except Exception as exc:

        logger.exception(
            "_handle_edit_text error: %s",
            exc,
        )

        await update.message.reply_text(
            "⚠️ حدث خطأ أثناء تنفيذ العملية."
        )

    finally:

        safe_remove_many(
            temp_files
        )

        safe_remove(
            file_path
        )

        context.user_data.clear()
