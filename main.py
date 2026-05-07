import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
import pytz
import telebot
from telebot import types

# الإعدادات الأساسية
BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8630722565:AAGK-xjCMLvtrvLnzvVbvTGn8vWClxsQh6E")
ADMIN_ID = 122498736
ZAIN_CASH_NUMBER = "07713356493"
DB_FILE = "bot_database.db"

bot = telebot.TeleBot(BOT_TOKEN)

# ── الحالات ─────────────────────────────────────────────────────────────────────
user_states = {}
user_data = {}

STATE_IDLE = "idle"
STATE_WAITING_DEPOSIT_AMOUNT = "waiting_deposit_amount"
STATE_WAITING_DEPOSIT_PHOTO = "waiting_deposit_photo"
STATE_WAITING_PLAN_PHOTO = "waiting_plan_photo"
STATE_WAITING_WITHDRAW_AMOUNT = "waiting_withdraw_amount"
STATE_WAITING_WITHDRAW_PHONE = "waiting_withdraw_phone"

WITHDRAW_METHODS = {
    "withdraw_zaincash":  {"label": "💳 زين كاش",          "short": "زين كاش"},
    "withdraw_asiacell":  {"label": "📱 آسياسيل تحويل",    "short": "آسياسيل"},
}

WELCOME_TEXT = (
    "💎 أهلاً بك في متجر دراهم الرقمي\n"
    "• البوت الأول في العراق لتحويل النقاط إلى أرباح حقيقية 🇮🇶\n\n"
    "اختر من الأزرار أدناه:"
)

# تم حذف باقة النخبة وتعديل الفئات
PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "amount": "10,000",  "raw": 10000,  "days": 15},
    "plan_silver": {"label": "🥈 الباقة الفضية",   "amount": "25,000",  "raw": 25000,  "days": 30},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "amount": "50,000",  "raw": 50000,  "days": 30},
}

PROFIT_RATE = 0.50

TYPE_LABELS = {
    "deposit":      "📥 إيداع",
    "withdrawal":   "📤 سحب",
    "plan_payment": "📊 اشتراك باقة",
    "plan_profit":  "💹 أرباح باقة",
}

STATUS_LABELS = {
    "pending":  "⏳ قيد المراجعة",
    "approved": "✅ مقبول",
    "rejected": "❌ مرفوض",
}

# ── قاعدة البيانات (تم تحسين الاستقرار) ───────────────────────────────────────────

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Database Error: {e}")
        raise
    finally:
        conn.close()

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                name      TEXT    DEFAULT '',
                username  TEXT    DEFAULT '',
                balance   REAL    DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                type        TEXT    NOT NULL,
                amount      REAL    NOT NULL,
                description TEXT    DEFAULT '',
                status      TEXT    DEFAULT 'pending',
                created_at  TEXT    DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id            INTEGER NOT NULL,
                plan_name          TEXT    NOT NULL,
                plan_key           TEXT    NOT NULL,
                amount             REAL    NOT NULL,
                duration_days      INTEGER NOT NULL,
                start_date         TEXT    NOT NULL,
                expiry_date        TEXT    NOT NULL,
                is_active          INTEGER DEFAULT 1,
                profit_paid        INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
        """)
    print("Database initialized & persistent.")

# دالة لجلب معلومات المستخدم والاشتراك دفعة واحدة
def get_user_context(user_id):
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        active_sub = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND is_active=1 ORDER BY id DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        last_sub = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        return user, active_sub, last_sub

# ── دوال مساعدة ─────────────────────────────────────────────────────────────────

def fmt(n):
    return f"{int(n):,}"

def ensure_user(conn, user_id, name="", username=""):
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, name, username, balance) VALUES (?, ?, ?, 0)",
        (user_id, name, username),
    )

def add_transaction(conn, user_id, txn_type, amount, description="", status="pending"):
    cur = conn.execute(
        "INSERT INTO transactions (user_id, type, amount, description, status) VALUES (?,?,?,?,?)",
        (user_id, txn_type, amount, description, status),
    )
    return cur.lastrowid

# ── القوائم ─────────────────────────────────────────────────────────────────────

def get_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💳 محفظتي",       callback_data="menu_wallet"),
        types.InlineKeyboardButton("📊 الباقات",       callback_data="menu_plans"),
        types.InlineKeyboardButton("📥 إيداع مباشر",  callback_data="menu_deposit"),
        types.InlineKeyboardButton("📤 سحب الأرباح",  callback_data="menu_withdraw"),
    )
    return markup

# ── المعالجات ──────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def handle_start(message):
    user_id = message.from_user.id
    with get_conn() as conn:
        ensure_user(conn, user_id, message.from_user.full_name, message.from_user.username)
    bot.send_message(message.chat.id, WELCOME_TEXT, reply_markup=get_main_menu())

@bot.callback_query_handler(func=lambda call: call.data == "menu_withdraw")
def handle_withdraw_menu(call):
    user_id = call.from_user.id
    user, active_sub, last_sub = get_user_context(user_id)

    # 1. حالة المستخدم الذي لم يسبق له الاشتراك أبداً
    if not last_sub:
        text = "⚠️ *عذراً، نظام السحب مغلق حالياً*\n\nيجب عليك تفعيل إحدى الباقات أولاً لتتمكن من فتح ميزة السحب."
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, 
                              parse_mode="Markdown", reply_markup=get_main_menu())
        return

    # 2. حالة وجود اشتراك (نشط أو منتهي)
    # نحدد المدة بناءً على نوع الباقة (15 أو 30)
    days_limit = last_sub["duration_days"]
    
    if active_sub:
        # إذا كان الاشتراك نشطاً، نتحقق من مرور المدة المطلوبة لسحب "الأرباح"
        # ملاحظة: في هذا النظام الأرباح تضاف عند الانتهاء أو يدوياً، لذا نوجه المستخدم للمدة
        text = (
            f"📤 *قسم سحب الأرباح*\n\n"
            f"📦 باقتك الحالية: *{active_sub['plan_name']}*\n"
            f"⏳ يرجى الانتظار مدة *{days_limit} يوم* حتى نهاية الباقة لاستلام الأرباح كاملة.\n\n"
            f"💰 رصيدك المتاح حالياً: *{fmt(user['balance'])} د.ع*"
        )
        if user['balance'] > 0:
            markup = types.InlineKeyboardMarkup(row_width=1)
            for key, method in WITHDRAW_METHODS.items():
                markup.add(types.InlineKeyboardButton(method["label"], callback_data=key))
            markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, 
                                  parse_mode="Markdown", reply_markup=markup)
        else:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, 
                                  parse_mode="Markdown", reply_markup=get_main_menu())
    else:
        # اشتراك سابق منتهي
        text = f"✅ انتهت مدة اشتراكك السابق ({days_limit} يوم). يمكنك الآن سحب رصيدك أو إعادة الاستثمار."
        # إظهار خيارات السحب... (نفس المنطق أعلاه)
        if user['balance'] > 0:
            markup = types.InlineKeyboardMarkup(row_width=1)
            for key, method in WITHDRAW_METHODS.items():
                markup.add(types.InlineKeyboardButton(method["label"], callback_data=key))
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, 
                                  parse_mode="Markdown", reply_markup=markup)
        else:
            bot.edit_message_text("⚠️ رصيدك الحالي 0. قم بالإيداع أو تفعيل باقة جديدة.", 
                                  call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

# بقية الكود (إيداع، شراء باقات، إلخ) تبقى كما هي مع استبدال get_conn المحسن
# ... (يمكنك إدراج بقية الوظائف من الملف الأصلي هنا مع التأكد من حذف أي إشارة لـ scheduler)

@bot.callback_query_handler(func=lambda call: call.data == "menu_back")
def handle_back(call):
    bot.edit_message_text(WELCOME_TEXT, call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

# تشغيل البوت
if __name__ == "__main__":
    init_db()
    print("Bot started without Daily Scheduler.")
    bot.infinity_polling()
