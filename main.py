import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
import pytz
import telebot
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler

# الإعدادات الأساسية
BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")

# يتم جلب التوكن من إعدادات Runway (Secrets) لضمان العمل
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8630722565:AAGnOFp-37kwIEdCR6GA5j2EmF7zPTrutOY")
ADMIN_ID = 122498736
ZAIN_CASH_NUMBER = "07713356493"
DB_FILE = "bot_database.db"

bot = telebot.TeleBot(BOT_TOKEN)

# ── [تعديل] الباقات (حذف النخبة وتثبيت المدد) ──────────────────────────────────
PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "amount": "10,000",  "raw": 10000,  "days": 15},
    "plan_silver": {"label": "🥈 الباقة الفضية",   "amount": "25,000",  "raw": 25000,  "days": 30},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "amount": "50,000",  "raw": 50000,  "days": 30},
}
PROFIT_RATE = 0.50

# ── جدار الحماية (قاعدة البيانات للسحابة) ───────────────────────────────────────
@contextmanager
def get_conn():
    # ضبط timeout عالي لمنع تعليق قاعدة البيانات في Runway
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

# ── [تعديل] تصفير النظام (البداية من الصفر) ──────────────────────────────────────
def init_db():
    with get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS users")
        conn.execute("DROP TABLE IF EXISTS subscriptions")
        conn.execute("DROP TABLE IF EXISTS transactions")
        conn.executescript("""
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                balance REAL DEFAULT 0
            );
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_name TEXT NOT NULL,
                plan_key TEXT NOT NULL,
                amount REAL NOT NULL,
                duration_days INTEGER NOT NULL,
                expiry_date TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                last_daily_payment TEXT DEFAULT NULL
            );
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT,
                amount REAL,
                status TEXT DEFAULT 'pending'
            );
        """)
    print("تم تصفير النظام بنجاح - البوت جاهز للعمل.")

# ── عقل البوت (نظام الأرباح 10ص - 10م) ──────────────────────────────────────────
def get_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📊 الباقات", callback_data="menu_plans"),
        types.InlineKeyboardButton("💳 محفظتي", callback_data="menu_wallet")
    )
    markup.add(
        types.InlineKeyboardButton("📥 إيداع مباشر", callback_data="menu_deposit"),
        types.InlineKeyboardButton("📤 سحب الأرباح", callback_data="menu_withdraw")
    )
    markup.add(
        types.InlineKeyboardButton("💰 استلام أرباح اليوم", callback_data="claim_daily_profit")
    )
    return markup

@bot.callback_query_handler(func=lambda call: True)
def handle_all_callbacks(call):
    user_id = call.from_user.id
    now = datetime.now(BAGHDAD_TZ)
    today = now.strftime("%Y-%m-%d")

    if call.data == "claim_daily_profit":
        # جدار حماية وقت الاستلام
        if not (10 <= now.hour < 22):
            bot.answer_callback_query(call.id, "⚠️ الاستلام متاح فقط من 10ص حتى 10م بتوقيت بغداد.", show_alert=True)
            return

        with get_conn() as conn:
            sub = conn.execute("SELECT * FROM subscriptions WHERE user_id=? AND is_active=1", (user_id,)).fetchone()
            if not sub:
                bot.answer_callback_query(call.id, "❌ لا تملك اشتراكاً نشطاً.", show_alert=True)
                return
            if sub['last_daily_payment'] == today:
                bot.answer_callback_query(call.id, "✅ استلمت أرباح اليوم بالفعل.", show_alert=True)
                return

            # الحساب المالي الدقيق والزيادة التراكمية (جدار حماية السحابة)
            profit = round((sub["amount"] * PROFIT_RATE) / sub["duration_days"], 2)
            conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (profit, user_id))
            conn.execute("UPDATE subscriptions SET last_daily_payment=? WHERE id=?", (today, sub['id']))
            bot.answer_callback_query(call.id, f"🎊 مبروك! تم إضافة {int(profit):,} د.ع لمحفظتك.", show_alert=True)

    # بقية الـ Callbacks الأصلية (سحب، إيداع، باقات) تبقى كما هي في كودك
    elif call.data == "menu_plans":
        markup = types.InlineKeyboardMarkup(row_width=1)
        for key, p in PLANS.items():
            markup.add(types.InlineKeyboardButton(f"{p['label']} | {p['amount']} د.ع", callback_data=f"buy_{key}"))
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
        bot.edit_message_text("📊 اختر باقة الاستثمار:", call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data == "menu_back":
        bot.edit_message_text("💎 أهلاً بك في متجر دراهم الرقمي", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

# ── تشغيل البوت ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db() # تصفير عند التشغيل الأول في Runway
    print("Runway Deploy: البوت يعمل بنظام الحماية المالي والتصفير الكامل.")
    bot.infinity_polling()
