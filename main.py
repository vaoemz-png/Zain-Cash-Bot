import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
import pytz
import telebot
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler

BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")

# الإعدادات الأصلية
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8630722565:AAGK-xjCMLvtrvLnzvVbvTGn8vWClxsQh6E")
ADMIN_ID = 122498736
ZAIN_CASH_NUMBER = "07713356493"
DB_FILE = "bot_database.db"

bot = telebot.TeleBot(BOT_TOKEN)

# ── States ─────────────────────────────────────────────────────────────────────
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

# تم حذف النخبة وتعديل الأيام (15 للبقية و 30 للباقي)
PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "amount": "10,000",  "raw": 10000,  "days": 15},
    "plan_silver": {"label": "🥈 الباقة الفضية",   "amount": "25,000",  "raw": 25000,  "days": 30},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "amount": "50,000",  "raw": 50000,  "days": 30},
}

PROFIT_RATE = 0.50 

# ── Database (كودك الأصلي للتعامل مع البيانات) ──────────────────────────────────

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
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
                created_at  TEXT    DEFAULT (datetime('now','localtime'))
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
                last_daily_payment TEXT    DEFAULT NULL
            );
        """)

# ── Keyboards (التعديل المطلوب لإعادة زر الأرباح وحذف النخبة) ──────────────────────

def get_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    # السطر الأول (كما في الصورة)
    markup.add(
        types.InlineKeyboardButton("📊 الباقات", callback_data="menu_plans"),
        types.InlineKeyboardButton("💳 محفظتي", callback_data="menu_wallet")
    )
    # السطر الثاني (كما في الصورة)
    markup.add(
        types.InlineKeyboardButton("📤 سحب الأرباح", callback_data="menu_withdraw"),
        types.InlineKeyboardButton("📥 إيداع مباشر", callback_data="menu_deposit")
    )
    # السطر الثالث (إعادة المحرك الرئيسي)
    markup.add(
        types.InlineKeyboardButton("💰 استلام أرباح اليوم", callback_data="daily_profit_check")
    )
    return markup

# ── Handlers ───────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def handle_start(message):
    user_id = message.from_user.id
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id, name, username) VALUES (?, ?, ?)", 
                     (user_id, message.from_user.full_name, message.from_user.username))
    bot.send_message(message.chat.id, WELCOME_TEXT, reply_markup=get_main_menu())

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    
    # المحرك: زر استلام الأرباح
    if call.data == "daily_profit_check":
        with get_conn() as conn:
            sub = conn.execute("SELECT plan_key, plan_name FROM subscriptions WHERE user_id=? AND is_active=1", (user_id,)).fetchone()
        
        if sub:
            days = 15 if sub['plan_key'] == "plan_bronze" else 30
            msg = f"✅ اشتراكك في {sub['plan_name']} نشط.\n⏳ يرجى الانتظار {days} يوم حتى تكتمل دورة الأرباح وتستطيع سحبها."
        else:
            msg = "❌ لا يوجد لديك اشتراك نشط حالياً. يرجى تفعيل باقة لتبدأ بجني الأرباح."
        bot.answer_callback_query(call.id, msg, show_alert=True)

    # محفظتي
    elif call.data == "menu_wallet":
        with get_conn() as conn:
            user = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
            sub = conn.execute("SELECT plan_name, expiry_date FROM subscriptions WHERE user_id=? AND is_active=1", (user_id,)).fetchone()
        
        balance = f"{int(user['balance']):,}" if user else "0"
        plan = sub['plan_name'] if sub else "لا توجد"
        expiry = sub['expiry_date'] if sub else "—"
        
        text = (f"💳 *محفظتك الرقمية*\n━━━━━━━━━━━━━━━━━\n"
                f"💰 الرصيد الحالي: *{balance} د.ع*\n"
                f"📊 الباقة النشطة: *{plan}*\n"
                f"⏳ تاريخ الانتهاء: *{expiry}*")
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=get_main_menu())

    # الباقات
    elif call.data == "menu_plans":
        markup = types.InlineKeyboardMarkup(row_width=1)
        for key, plan in PLANS.items():
            markup.add(types.InlineKeyboardButton(f"{plan['label']} | {plan['amount']} د.ع", callback_data=f"buy_{key}"))
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
        bot.edit_message_text("📊 الباقات المتاحة للاستثمار:", call.message.chat.id, call.message.message_id, reply_markup=markup)

    # الرجوع
    elif call.data == "menu_back":
        bot.edit_message_text(WELCOME_TEXT, call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

# تشغيل البوت
if __name__ == "__main__":
    init_db()
    print("البوت يعمل بالنسخة الكاملة مع زر الأرباح اليومية...")
    bot.infinity_polling()
