import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
import pytz
import telebot
from telebot import types

# ── إعدادات ─────────────────────────────────────────────
BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TOKEN_HERE")
ADMIN_ID = 122498736
ZAIN_CASH_NUMBER = "07713356493"

DB_FILE = os.path.join(os.getcwd(), "bot_database.db")

bot = telebot.TeleBot(BOT_TOKEN)

# ── States ───────────────────────────────────────────────
user_states = {}
user_data = {}

STATE_IDLE = "idle"
STATE_WAITING_DEPOSIT_AMOUNT = "waiting_deposit_amount"
STATE_WAITING_DEPOSIT_PHOTO = "waiting_deposit_photo"
STATE_WAITING_PLAN_PHOTO = "waiting_plan_photo"
STATE_WAITING_WITHDRAW_AMOUNT = "waiting_withdraw_amount"
STATE_WAITING_WITHDRAW_PHONE = "waiting_withdraw_phone"

# ── الباقات (4 باقات كاملة) ─────────────────────────────
PLANS = {
    "plan_bronze": {"label": "🥉 البرونزية", "amount": "10,000", "raw": 10000, "days": 15},
    "plan_silver": {"label": "🥈 الفضية", "amount": "25,000", "raw": 25000, "days": 30},
    "plan_gold": {"label": "🥇 الذهبية", "amount": "50,000", "raw": 50000, "days": 30},
    "plan_elite": {"label": "💎 النخبة", "amount": "100,000", "raw": 100000, "days": 30},
}

PROFIT_RATE = 0.50

TYPE_LABELS = {
    "deposit": "إيداع",
    "withdrawal": "سحب",
    "plan_payment": "اشتراك",
}

STATUS_LABELS = {
    "pending": "قيد المراجعة",
    "approved": "مقبول",
    "rejected": "مرفوض",
}

WELCOME_TEXT = "💎 أهلاً بك في النظام\nاختر من الأزرار أدناه"

# ── Database ────────────────────────────────────────────
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            amount REAL,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan_name TEXT,
            plan_key TEXT,
            amount REAL,
            duration_days INTEGER,
            start_date TEXT,
            expiry_date TEXT,
            is_active INTEGER DEFAULT 1
        );
        """)


def ensure_user(conn, uid):
    conn.execute("INSERT OR IGNORE INTO users(user_id,balance) VALUES(?,0)", (uid,))


def get_user(conn, uid):
    return conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def add_transaction(conn, uid, ttype, amount):
    conn.execute("INSERT INTO transactions(user_id,type,amount) VALUES(?,?,?)",
                 (uid, ttype, amount))


def add_subscription(conn, uid, key, amount, days):
    now = datetime.now()
    exp = now + timedelta(days=days)
    conn.execute("""
        INSERT INTO subscriptions(user_id,plan_name,plan_key,amount,duration_days,start_date,expiry_date)
        VALUES(?,?,?,?,?,?,?)
    """, (uid, PLANS[key]["label"], key, amount, days,
          now.strftime("%Y-%m-%d"), exp.strftime("%Y-%m-%d")))


def get_active_subscription(conn, uid):
    today = datetime.now().strftime("%Y-%m-%d")
    return conn.execute("""
        SELECT * FROM subscriptions
        WHERE user_id=? AND is_active=1 AND expiry_date>=?
        ORDER BY id DESC LIMIT 1
    """, (uid, today)).fetchone()


# ── واجهة ───────────────────────────────────────────────
def main_menu():
    m = types.InlineKeyboardMarkup()
    m.add(
        types.InlineKeyboardButton("💳 المحفظة", callback_data="wallet"),
        types.InlineKeyboardButton("📊 الباقات", callback_data="plans"),
    )
    m.add(
        types.InlineKeyboardButton("📥 إيداع", callback_data="deposit"),
        types.InlineKeyboardButton("📤 سحب", callback_data="withdraw"),
    )
    return m


# ── Start ───────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def start(m):
    with get_conn() as c:
        ensure_user(c, m.from_user.id)
    bot.send_message(m.chat.id, WELCOME_TEXT, reply_markup=main_menu())


# ── Wallet ──────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "wallet")
def wallet(c):
    with get_conn() as conn:
        u = get_user(conn, c.from_user.id)
    bot.send_message(c.message.chat.id, f"💰 رصيدك: {u['balance']}", reply_markup=main_menu())


# ── Plans ───────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "plans")
def plans(c):
    m = types.InlineKeyboardMarkup()
    for k, p in PLANS.items():
        m.add(types.InlineKeyboardButton(f"{p['label']} - {p['amount']}", callback_data=k))
    bot.send_message(c.message.chat.id, "اختر باقتك:", reply_markup=m)


# ── Buy Plan ────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data in PLANS)
def buy(c):
    plan = PLANS[c.data]
    with get_conn() as conn:
        ensure_user(conn, c.from_user.id)
        u = get_user(conn, c.from_user.id)

        if u["balance"] < plan["raw"]:
            bot.send_message(c.message.chat.id, "❌ رصيد غير كافي")
            return

        conn.execute("UPDATE users SET balance=balance-? WHERE user_id=?",
                     (plan["raw"], c.from_user.id))
        add_transaction(conn, c.from_user.id, "plan_payment", plan["raw"])
        add_subscription(conn, c.from_user.id, c.data, plan["raw"], plan["days"])

    bot.send_message(c.message.chat.id, "✅ تم تفعيل الباقة")


# ── Deposit (مختصر) ─────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "deposit")
def deposit(c):
    bot.send_message(c.message.chat.id, "أرسل المبلغ")


# ── Withdraw (مختصر + تحقق) ─────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "withdraw")
def withdraw(c):
    with get_conn() as conn:
        sub = get_active_subscription(conn, c.from_user.id)

    if not sub:
        bot.send_message(c.message.chat.id, "❌ لا يوجد اشتراك نشط")
        return

    bot.send_message(c.message.chat.id, "أدخل مبلغ السحب")


# ── تشغيل ───────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print("Bot Running...")
    bot.infinity_polling()
