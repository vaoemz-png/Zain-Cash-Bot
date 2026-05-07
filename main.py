import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
import pytz
import telebot
from telebot import types

# ── الإعدادات الأساسية ────────────────────────────────────────────────────────
BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")
BOT_TOKEN = "8630722565:AAGK-xjCMLvtrvLnzvVbvTGn8vWClxsQh6E"
ADMIN_ID = 122498736
ZAIN_CASH_NUMBER = "07713356493"
DB_FILE = "bot_database.db"

bot = telebot.TeleBot(BOT_TOKEN)

# الباقات (تم حذف النخبة وتعديل المدد)
PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "amount": "10,000",  "raw": 10000,  "days": 15},
    "plan_silver": {"label": "🥈 الباقة الفضية",   "amount": "25,000",  "raw": 25000,  "days": 30},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "amount": "50,000",  "raw": 50000,  "days": 30},
}

# ── قاعدة البيانات ───────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0,
                last_plan TEXT DEFAULT 'none'
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                plan_key TEXT,
                is_active INTEGER DEFAULT 1,
                expiry_date TEXT
            );
        """)

# ── الكيبورد (نفس تصميمك المفضل في الصور) ──────────────────────────────────────

def get_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    # السطر الأول
    markup.add(
        types.InlineKeyboardButton("📊 الباقات", callback_data="menu_plans"),
        types.InlineKeyboardButton("💳 محفظتي", callback_data="menu_wallet")
    )
    # السطر الثاني
    markup.add(
        types.InlineKeyboardButton("📥 إيداع مباشر", callback_data="menu_deposit"),
        types.InlineKeyboardButton("📤 سحب الأرباح", callback_data="menu_withdraw")
    )
    # السطر الثالث (العقل المدبر للبوت)
    markup.add(
        types.InlineKeyboardButton("💰 استلام أرباح اليوم", callback_data="daily_profit_check")
    )
    return markup

# ── المعالجات (Handlers) ─────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def handle_start(message):
    uid = message.from_user.id
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    bot.send_message(message.chat.id, "💎 أهلاً بك في متجر دراهم الرقمي\nاختر من الأزرار أدناه:", reply_markup=get_main_menu())

@bot.callback_query_handler(func=lambda call: True)
def handle_all_callbacks(call):
    uid = call.from_user.id
    
    # 1. زر الأرباح اليومية (التعديل المطلوب)
    if call.data == "daily_profit_check":
        with get_conn() as conn:
            sub = conn.execute("SELECT plan_key FROM subscriptions WHERE user_id=? AND is_active=1", (uid,)).fetchone()
        
        if sub:
            plan_key = sub['plan_key']
            days = PLANS[plan_key]['days']
            msg = f"✅ اشتراكك نشط حالياً.\n⏳ يرجى الانتظار {days} يوم لاكتمال الدورة وسحب الرصيد."
        else:
            msg = "❌ لا يوجد اشتراك نشط. يرجى تفعيل باقة أولاً."
        bot.answer_callback_query(call.id, msg, show_alert=True)

    # 2. قسم المحفظة
    elif call.data == "menu_wallet":
        with get_conn() as conn:
            user = conn.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        bal = user['balance'] if user else 0
        bot.edit_message_text(f"💳 *محفظتك الرقمية*\n\n💰 الرصيد الحالي: *{bal:,} د.ع*", 
                             call.message.chat.id, call.message.message_id, 
                             parse_mode="Markdown", reply_markup=get_main_menu())

    # 3. عرض الباقات
    elif call.data == "menu_plans":
        markup = types.InlineKeyboardMarkup(row_width=1)
        for key, p in PLANS.items():
            markup.add(types.InlineKeyboardButton(f"{p['label']} | {p['amount']} د.ع", callback_data=f"buy_{key}"))
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
        bot.edit_message_text("📊 الباقات المتاحة للاستثمار:", call.message.chat.id, call.message.message_id, reply_markup=markup)

    # 4. منطق الشراء (تثبيت البيانات)
    elif call.data.startswith("buy_"):
        plan_key = call.data.split("_")[1]
        with get_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO subscriptions (user_id, plan_key, is_active) VALUES (?, ?, 1)", (uid, plan_key))
        bot.answer_callback_query(call.id, "✅ تم اختيار الباقة. أرسل الوصل للمسؤول لتفعيلها.", show_alert=True)

    # 5. زر السحب
    elif call.data == "menu_withdraw":
        with get_conn() as conn:
            sub = conn.execute("SELECT plan_key FROM subscriptions WHERE user_id=? AND is_active=1", (uid,)).fetchone()
        
        if not sub:
            bot.edit_message_text("⚠️ عذراً، نظام السحب مغلق حالياً لغير المشتركين.", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())
        else:
            bot.edit_message_text("📤 اختر طريقة السحب:", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu()) # يمكن إضافة كيبورد السحب هنا

    elif call.data == "menu_back":
        bot.edit_message_text("💎 أهلاً بك في متجر دراهم الرقمي", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

# ── تشغيل ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print("البوت يعمل الآن بالنسخة المصححة...")
    bot.infinity_polling()
