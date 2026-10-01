from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

PRIMARY = "primary"
SUCCESS = "success"
DANGER = "danger"


def main_menu_keyboard(is_admin: bool = False):
    keyboard = [
        [
            KeyboardButton(
                "▶️ تشغيل البوت",
                style=SUCCESS,
            )
        ],
        [
            KeyboardButton(
                "🎵 تعديل الأغنية",
                style=PRIMARY,
            ),
            KeyboardButton(
                "🎬 استخراج الصوت",
                style=PRIMARY,
            ),
        ],
        [
            KeyboardButton(
                "🖼️ إنشاء أغنية كاملة (اسم + صورة + صوت)",
                style=PRIMARY,
            )
        ],
        [
            KeyboardButton(
                "📊 إحصائياتي",
                style=PRIMARY,
            )
        ],
    ]

    if is_admin:
        keyboard.append(
            [
                KeyboardButton(
                    "🛠 لوحة التحكم",
                    style=PRIMARY,
                )
            ]
        )

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="اختر خدمة من القائمة…",
    )


def my_song_menu_keyboard():
    keyboard = [
        [
            InlineKeyboardButton(
                "📝 تعديل الاسم والصورة",
                callback_data="mysong_edit",
                style=PRIMARY,
            )
        ],
        [
            InlineKeyboardButton(
                "🎬 فيديو ← صوت + صورة",
                callback_data="mysong_extract",
                style=PRIMARY,
            )
        ],
        [
            InlineKeyboardButton(
                "🆕 صوت جديد + صورة",
                callback_data="mysong_new",
                style=SUCCESS,
            )
        ],
        [
            InlineKeyboardButton(
                "❌ إلغاء",
                callback_data="cancel_action",
                style=DANGER,
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def quality_keyboard(action_type: str):
    keyboard = [
        [
            InlineKeyboardButton(
                "🎵 128 kbps",
                callback_data=f"q_128_{action_type}",
                style=PRIMARY,
            ),
            InlineKeyboardButton(
                "🎵 192 kbps",
                callback_data=f"q_192_{action_type}",
                style=PRIMARY,
            ),
        ],
        [
            InlineKeyboardButton(
                "🎵 256 kbps",
                callback_data=f"q_256_{action_type}",
                style=PRIMARY,
            ),
            InlineKeyboardButton(
                "🎵 320 kbps",
                callback_data=f"q_320_{action_type}",
                style=SUCCESS,
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ إلغاء",
                callback_data="cancel_action",
                style=DANGER,
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def admin_panel_keyboard(maintenance_status: bool):
    if maintenance_status:
        m_text = "🔴 إيقاف الصيانة"
        m_style = DANGER
    else:
        m_text = "🟢 تفعيل الصيانة"
        m_style = SUCCESS

    keyboard = [
        [
            InlineKeyboardButton(
                "📊 إحصائيات البوت",
                callback_data="admin_stats",
                style=PRIMARY,
            )
        ],
        [
            InlineKeyboardButton(
                m_text,
                callback_data="toggle_maintenance",
                style=m_style,
            )
        ],
        [
            InlineKeyboardButton("📣 إدارة الاشتراك الإجباري", callback_data="force_menu", style=PRIMARY)
        ],
        [
            InlineKeyboardButton(
                "📢 إرسال إعلان",
                callback_data="admin_broadcast",
                style=PRIMARY,
            )
        ],
        [
            InlineKeyboardButton(
                "🧹 تنظيف الملفات المؤقتة",
                callback_data="admin_clean",
                style=DANGER,
            )
        ],
        [
            InlineKeyboardButton(
                "⚡ تحسين قاعدة البيانات",
                callback_data="admin_optimize",
                style=SUCCESS,
            )
        ],
        [
            InlineKeyboardButton(
                "❌ إغلاق",
                callback_data="close_admin",
                style=DANGER,
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def cancel_keyboard():
    keyboard = [
        [
            InlineKeyboardButton(
                "❌ إلغاء العملية",
                callback_data="cancel_action",
                style=DANGER,
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


def force_subscription_keyboard(channels):
    keyboard = []
    for index, channel in enumerate(channels):
        keyboard.append([InlineKeyboardButton(f"🗑 حذف {channel.get('title') or channel.get('username')}", callback_data=f"force_del_{index}", style=DANGER)])
    keyboard.extend([
        [InlineKeyboardButton("➕ إضافة قناة", callback_data="force_add", style=SUCCESS)],
        [InlineKeyboardButton("✏️ تعديل رسالة الاشتراك", callback_data="force_message", style=PRIMARY)],
        [InlineKeyboardButton("↩️ رجوع للوحة المطور", callback_data="force_back", style=PRIMARY)],
    ])
    return InlineKeyboardMarkup(keyboard)


def subscription_keyboard(channels):
    keyboard = []
    for channel in channels:
        url = channel.get("url") or ("https://t.me/" + str(channel.get("username", "")).lstrip("@"))
        keyboard.append([InlineKeyboardButton(channel.get("title") or "📢 الاشتراك بالقناة", url=url)])
    keyboard.append([InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="subscription_check", style=SUCCESS)])
    return InlineKeyboardMarkup(keyboard)
