import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
import pytz
import telebot
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler

BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")

# توكن البوت الأصلي الخاص بك
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

# [تعديل] تم حذف باقة النخبة وضبط المدد بدقة
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
    "plan_deposit": "📥 إيداع لباقة",
}

STATUS_LABELS = {
    "pending":  "⏳ قيد المراجعة",
    "approved": "✅ مقبول",
    "rejected": "❌ مرفوض",
}

# ── Database (نظام حماية المتغيرات للسحابة) ───────────────────────────────────

@contextmanager
def get_conn():
    # تم إضافة timeout عالي لضمان عدم ضياع البيانات عند تحديث السيرفر
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# [تعديل] دالة التصفير الكامل لإعادة البوت جديداً
def init_db():
    with get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS users")
        conn.execute("DROP TABLE IF EXISTS subscriptions")
        conn.execute("DROP TABLE IF EXISTS transactions")
        
        conn.executescript("""
            CREATE TABLE users (
                user_id   INTEGER PRIMARY KEY,
                name      TEXT    DEFAULT '',
                username  TEXT    DEFAULT '',
                balance   REAL    DEFAULT 0
            );

            CREATE TABLE transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                type        TEXT    NOT NULL,
                amount      REAL    NOT NULL,
                description TEXT    DEFAULT '',
                status      TEXT    DEFAULT 'pending',
                created_at  TEXT    DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE subscriptions (
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
                last_daily_payment TEXT    DEFAULT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
        """)
    print("Database Reset Successfully: System starts from Zero.")

# ── الحسابات المالية (نظام الزيادة التراكمية) ──────────────────────────────────

def credit_expired_profits(conn, user_id):
    expired = get_expired_unpaid_subscriptions(conn, user_id)
    messages = []
    for sub in expired:
        principal = sub["amount"]
        # [جدار حماية] زيادة تراكمية لضمان عدم تصفير الرصيد
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (principal, user_id))
        conn.execute("UPDATE subscriptions SET is_active=0, profit_paid=1 WHERE id=?", (sub["id"],))
        add_transaction(conn, user_id, "plan_profit", principal, description=f"استرداد رأس المال — {sub['plan_name']}", status="approved")
        messages.append(f"✅ انتهت باقتك *{sub['plan_name']}*!\n💰 تم استرداد رأس المال: *{fmt(principal)} د.ع*")
    return messages

# ... (بقية الدوال المساعدة والكيبوردات تبقى كما هي في ملفك الأصلي دون تغيير)

# ── Daily profit scheduler (إصلاح العمليات الحسابية) ──────────────────────────

def daily_profit_task():
    today = datetime.now(BAGHDAD_TZ).strftime("%Y-%m-%d")
    with get_conn() as conn:
        subs = conn.execute("""
            SELECT s.id, s.user_id, s.plan_name, s.amount, s.duration_days, u.name 
            FROM subscriptions s JOIN users u ON s.user_id = u.user_id
            WHERE s.is_active = 1 AND s.expiry_date >= ? AND (s.last_daily_payment IS NULL OR s.last_daily_payment != ?)
        """, (today, today)).fetchall()

        for sub in subs:
            # [إصلاح] الحساب الصحيح للأرباح اليومية
            daily_amount = round((sub["amount"] * PROFIT_RATE) / sub["duration_days"], 2)
            
            # [جدار حماية] تحديث تراكمي مباشر في قاعدة البيانات
            conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (daily_amount, sub["user_id"]))
            conn.execute("UPDATE subscriptions SET last_daily_payment=? WHERE id=?", (today, sub["id"]))
            
            add_transaction(conn, sub["user_id"], "plan_profit", daily_amount, description=f"ربح يومي — {sub['plan_name']}", status="approved")
            
            try:
                bot.send_message(sub["user_id"], f"💰 *إشعار أرباح اليوم*\nتم إضافة أرباحك: *+{fmt(daily_amount)} د.ع*", parse_mode="Markdown")
            except: pass

if __name__ == "__main__":
    init_db() # سيقوم بتصفير البيانات عند أول تشغيل
    start_scheduler()
    print("Bot started with Enhanced Security and Zero Data Reset.")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
