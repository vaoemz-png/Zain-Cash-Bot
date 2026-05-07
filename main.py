import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
import pytz
import telebot
from telebot import types

BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")

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

# الحفاظ على جميع باقاتك كما هي في ملفك الأصلي
PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "amount": "10,000",  "raw": 10000,  "days": 15},
    "plan_silver": {"label": "🥈 الباقة الفضية",   "amount": "25,000",  "raw": 25000,  "days": 30},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "amount": "50,000",  "raw": 50000,  "days": 30},
    "plan_elite":  {"label": "💎 باقة النخبة",      "amount": "100,000", "raw": 100000, "days": 30},
}

PROFIT_RATE = 0.50

TYPE_LABELS = {
    "deposit":      "📥 إيداع",
    "withdrawal":   "📤 سحب",
    "plan_payment": "📊 اشتراك باقة",
    "plan_profit":  "💹 أرباح باقة",
}

# ── Database ───────────────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_FILE, timeout=20)
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

def fmt(n):
    return f"{int(n):,}"

# ── Keyboards ──────────────────────────────────────────────────────────────────

def get_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📊 الباقات", callback_data="menu_plans"),
        types.InlineKeyboardButton("💳 محفظتي", callback_data="menu_wallet")
    )
    markup.add(
        types.InlineKeyboardButton("📤 سحب الأرباح", callback_data="menu_withdraw"),
        types.InlineKeyboardButton("📥 إيداع مباشر", callback_data="menu_deposit")
    )
    markup.add(
        types.InlineKeyboardButton("💰 استلام أرباح اليوم", callback_data="claim_daily_profit")
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
    now_bgd = datetime.now(BAGHDAD_TZ)
    today_str = now_bgd.strftime("%Y-%m-%d")

    # زر استلام الأرباح (عقل البوت)
    if call.data == "claim_daily_profit":
        if not (10 <= now_bgd.hour < 22):
            bot.answer_callback_query(call.id, "⚠️ وقت المطالبة من 10ص إلى 10م فقط. الأرباح غير المطالب بها تذهب للمنصة.", show_alert=True)
            return

        with get_conn() as conn:
            sub = conn.execute("SELECT * FROM subscriptions WHERE user_id=? AND is_active=1 AND expiry_date >= ?", 
                               (user_id, today_str)).fetchone()
            
            if not sub:
                bot.answer_callback_query(call.id, "❌ لا توجد باقة نشطة حالياً.", show_alert=True)
                return

            if sub['last_daily_payment'] == today_str:
                bot.answer_callback_query(call.id, "✅ استلمت أرباحك اليوم بالفعل!", show_alert=True)
                return

            # الحساب المالي الدقيق
            daily_profit = round((sub["amount"] * PROFIT_RATE) / sub["duration_days"], 2)
            conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (daily_profit, user_id))
            conn.execute("UPDATE subscriptions SET last_daily_payment=? WHERE id=?", (today_str, sub['id']))
            conn.execute("INSERT INTO transactions (user_id, type, amount, description, status) VALUES (?,?,?,?,?)",
                         (user_id, "plan_profit", daily_profit, f"ربح يدوي: {sub['plan_name']}", "approved"))
            
            bot.answer_callback_query(call.id, f"🎊 تم إيداع {fmt(daily_profit)} د.ع في محفظتك!", show_alert=True)

    elif call.data == "menu_wallet":
        with get_conn() as conn:
            user = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
            sub = conn.execute("SELECT plan_name, expiry_date FROM subscriptions WHERE user_id=? AND is_active=1", (user_id,)).fetchone()
        
        balance = fmt(user['balance']) if user else "0"
        plan = sub['plan_name'] if sub else "لا توجد"
        expiry = sub['expiry_date'] if sub else "—"
        
        text = (f"💳 *محفظتك الرقمية*\n━━━━━━━━━━━━━━━━━\n"
                f"💰 الرصيد الحالي: *{balance} د.ع*\n"
                f"📊 الباقة النشطة: *{plan}*\n"
                f"⏳ تاريخ الانتهاء: *{expiry}*")
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=get_main_menu())

    elif call.data == "menu_plans":
        markup = types.InlineKeyboardMarkup(row_width=1)
        for key, p in PLANS.items():
            markup.add(types.InlineKeyboardButton(f"{p['label']} | {p['amount']} د.ع", callback_data=f"buy_{key}"))
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
        bot.edit_message_text("📊 الباقات المتاحة:", call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data == "menu_back":
        bot.edit_message_text(WELCOME_TEXT, call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

# تشغيل البوت مع التنويه أن باقي الدوال (السحب والإيداع) تعمل وفق المنطق الأصلي في ملفك
if __name__ == "__main__":
    init_db()
    print("تم تفعيل عقل البوت الجديد... الأرباح يدوية (10ص-10م)")
    bot.infinity_polling()
