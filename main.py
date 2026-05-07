import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
import telebot
from telebot import types

# الإعدادات
BOT_TOKEN = "8630722565:AAGK-xjCMLvtrvLnzvVbvTGn8vWClxsQh6E"
DB_FILE = "bot_database.db"

bot = telebot.TeleBot(BOT_TOKEN)

# الباقات مع المدد المحددة
PLANS = {
    "plan_10": {"label": "🥉 باقة الـ 10", "amount": 10000, "days": 15},
    "plan_25": {"label": "🥈 باقة الـ 25", "amount": 25000, "days": 30},
    "plan_50": {"label": "🥇 باقة الـ 50", "amount": 50000, "days": 30},
}

# ── قاعدة بيانات مستقرة (لحل مشكلة ضياع البيانات) ────────────────
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0,
                last_plan TEXT DEFAULT 'none'
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                plan_key TEXT,
                active INTEGER DEFAULT 0,
                start_date TEXT
            )""")
        conn.commit()

@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    try:
        yield conn
        conn.commit() # حفظ إجباري للبيانات
    finally:
        conn.close()

# ── القوائم ──────────────────────────────────────────────────

def main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💰 الأرباح اليومية", callback_data="daily_profit_check"),
        types.InlineKeyboardButton("📊 الباقات", callback_data="show_plans"),
        types.InlineKeyboardButton("💳 محفظتي", callback_data="my_wallet"),
        types.InlineKeyboardButton("📤 سحب الأرباح", callback_data="withdraw_request")
    )
    return markup

# ── المعالجات ────────────────────────────────────────────────

@bot.message_handler(commands=['start'])
def start(message):
    with db_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.from_user.id,))
    bot.send_message(message.chat.id, "💎 أهلاً بك في نظام الأرباح.\nتم تحديث النظام ليعمل بشكل متكامل مع باقاتك.", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    uid = call.from_user.id

    # 1. زر الأرباح اليومية (العقل المدبر للنظام)
    if call.data == "daily_profit_check":
        with db_conn() as conn:
            sub = conn.execute("SELECT plan_key, active FROM subscriptions WHERE user_id=?", (uid,)).fetchone()
            user = conn.execute("SELECT last_plan FROM users WHERE user_id=?", (uid,)).fetchone()

        if sub and sub[1] == 1: # مشترك حالي
            plan_key = sub[0]
            days = 15 if plan_key == "plan_10" else 30
            msg = f"✅ اشتراكك نشط حالياً.\n📦 نوع الباقة: {PLANS[plan_key]['label']}\n⏳ يرجى الانتظار {days} يوم حتى تكتمل دورة الأرباح وتستطيع سحبها."
        elif user and user[0] != 'none': # لديه خطة سابقة انتهت
            msg = f"⚠️ انتهت خطتك السابقة. يمكنك الآن سحب الأرباح أو التجديد لفتح الأرباح اليومية مرة أخرى."
        else: # غير مشترك
            msg = "❌ أنت غير مشترك في أي باقة حالياً. يرجى تفعيل باقة لتبدأ بجني الأرباح اليومية."
        
        bot.answer_callback_query(call.id, msg, show_alert=True)

    # 2. عرض الباقات (بدون النخبة)
    elif call.data == "show_plans":
        markup = types.InlineKeyboardMarkup()
        for key, p in PLANS.items():
            markup.add(types.InlineKeyboardButton(p['label'], callback_data=f"buy_{key}"))
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_home"))
        bot.edit_message_text("اختر الباقة المناسبة لتفعيل الأرباح:", uid, call.message.message_id, reply_markup=markup)

    # 3. منطق الشراء (لتثبيت البيانات)
    elif call.data.startswith("buy_"):
        plan_key = call.data.split("_")[1]
        with db_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO subscriptions (user_id, plan_key, active, start_date) VALUES (?, ?, 1, ?)", 
                         (uid, plan_key, datetime.now().strftime("%Y-%m-%d")))
            conn.execute("UPDATE users SET last_plan=? WHERE user_id=?", (plan_key, uid))
        bot.answer_callback_query(call.id, "✅ تم تفعيل الباقة بنجاح!", show_alert=True)
        bot.edit_message_text("تم ربط حسابك بالنظام بنجاح.", uid, call.message.message_id, reply_markup=main_menu())

    # 4. زر السحب (مرتبط بمدة الباقة)
    elif call.data == "withdraw_request":
        with db_conn() as conn:
            row = conn.execute("SELECT last_plan FROM users WHERE user_id=?", (uid,)).fetchone()
        
        if not row or row[0] == 'none':
            msg = "⚠️ يجب أن يكون لديك اشتراك سابق أو حالي لتتمكن من السحب."
        else:
            days = 15 if row[0] == "plan_10" else 30
            msg = f"📤 طلب سحب أرباح:\nنظام باقتك يتطلب مرور {days} يوم.\nيرجى الانتظار حتى انتهاء المدة."
        
        bot.edit_message_text(msg, uid, call.message.message_id, reply_markup=main_menu())

    elif call.data == "my_wallet":
        with db_conn() as conn:
            row = conn.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        bot.answer_callback_query(call.id, f"رصيدك: {row[0] if row else 0:,} د.ع", show_alert=True)

    elif call.data == "back_home":
        bot.edit_message_text("القائمة الرئيسية:", uid, call.message.message_id, reply_markup=main_menu())

if __name__ == "__main__":
    init_db()
    print("البوت يعمل بنظام 'زر الأرباح' المتكامل...")
    bot.infinity_polling()
