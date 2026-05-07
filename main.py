import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
import pytz
import telebot
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler

BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")

# التوكن الجديد الذي أرسلته
BOT_TOKEN = "8630722565:AAGnOFp-37kwIEdCR6GA5j2EmF7zPTrutOY"
ADMIN_ID = 122498736
ZAIN_CASH_NUMBER = "07713356493"
DB_FILE = "bot_database.db"

bot = telebot.TeleBot(BOT_TOKEN)

# ── [التعديل المطلوب] حذف النخبة وضبط المدد ──────────────────────────────────
PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "amount": "10,000",  "raw": 10000,  "days": 15},
    "plan_silver": {"label": "🥈 الباقة الفضية",   "amount": "25,000",  "raw": 25000,  "days": 30},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "amount": "50,000",  "raw": 50000,  "days": 30},
}
# ─────────────────────────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# دالة التصفير (للتأكد من أن التغييرات ظهرت في قاعدة البيانات)
def init_db():
    with get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS users")
        conn.execute("DROP TABLE IF EXISTS subscriptions")
        conn.execute("DROP TABLE IF EXISTS transactions")
        conn.executescript("""
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY, 
                name TEXT, 
                username TEXT, 
                balance REAL DEFAULT 0
            );
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                plan_name TEXT,
                plan_key TEXT,
                amount REAL,
                duration_days INTEGER,
                start_date TEXT,
                expiry_date TEXT,
                is_active INTEGER DEFAULT 1,
                last_daily_payment TEXT
            );
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                user_id INTEGER, 
                type TEXT, 
                amount REAL, 
                status TEXT DEFAULT 'pending'
            );
        """)

# ... (هنا ضع بقية كود ريبلت الأصلي الخاص بك)

if __name__ == "__main__":
    init_db() # تصفير لمرة واحدة عند التشغيل في Runway
    print(f"تم التشغيل بالتوكن الجديد: {BOT_TOKEN[:15]}...")
    bot.infinity_polling()
