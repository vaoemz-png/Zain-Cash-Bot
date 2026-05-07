import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
import pytz
import telebot
from telebot import types

# الإعدادات
BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")

# دالة ذكية لإيجاد التوكن حتى لو وُضع في خانة الاسم في Railway
def discover_token():
    # يبحث في جميع المتغيرات عن نص يبدأ بـ 8630 وهو توكن بوتك
    for key in os.environ.keys():
        if key.startswith("863072"):
            return key
    return "8630722565:AAGK-xjCMLvtrvLnzvVbvTGn8vWClxsQh6E"

BOT_TOKEN = discover_token()
DB_FILE = "bot_database.db"

bot = telebot.TeleBot(BOT_TOKEN)

# ── [تعهد] الباقات (بدون نخبة - مدد 15 و30 يوم) ──────────────────────────
PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "amount": "10,000", "raw": 10000, "days": 15},
    "plan_silver": {"label": "🥈 الباقة الفضية", "amount": "25,000", "raw": 25000, "days": 30},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "amount": "50,000", "raw": 50000, "days": 30},
}
PROFIT_RATE = 0.50

@contextmanager
def get_conn():
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

# تصفير النظام لمرة واحدة (بداية من الصفر)
def init_db():
    with get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS users")
        conn.execute("DROP TABLE IF EXISTS subscriptions")
        conn.executescript("""
            CREATE TABLE users (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0);
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                plan_name TEXT,
                amount REAL,
                duration_days INTEGER,
                last_daily_payment TEXT,
                is_active INTEGER DEFAULT 1
            );
        """)

# القائمة الرئيسية
def get_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📊 الباقات", callback_data="menu_plans"),
        types.InlineKeyboardButton("💳 محفظتي", callback_data="menu_wallet")
    )
    markup.add(
        types.InlineKeyboardButton("💰 استلام أرباح اليوم", callback_data="claim_daily_profit")
    )
    return markup

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "💎 أهلاً بك في نظام دراهم الجديد والمستقر", reply_markup=get_main_menu())

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    if call.data == "menu_plans":
        markup = types.InlineKeyboardMarkup(row_width=1)
        for key, p in PLANS.items():
            markup.add(types.InlineKeyboardButton(f"{p['label']} | {p['amount']} د.ع", callback_data=f"buy_{key}"))
        bot.edit_message_text("📊 اختر باقة (15 أو 30 يوم):", call.message.chat.id, call.message.message_id, reply_markup=markup)

if __name__ == "__main__":
    init_db()
    print("تم الاتصال بنجاح.. البوت يعمل الآن.")
    bot.infinity_polling()
