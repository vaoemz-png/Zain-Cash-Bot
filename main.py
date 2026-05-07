import os
import sqlite3
from datetime import datetime
import pytz
import telebot
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler

# --- الإعدادات الأساسية ---
BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")
# تأكد من وضع التوكن الخاص بك هنا أو في Secret Variables على جيت هوب
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8630722565:AAGK-xjCMLvtrvLnzvVbvTGn8vWClxsQh6E")
DB_FILE = "bot_database.db"

# --- منطق الباقات المحدث (الفحص يتم هنا) ---
PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "amount": "10,000",  "raw": 10000,  "lock_days": 15},
    "plan_silver": {"label": "🥈 الباقة الفضية",   "amount": "25,000",  "raw": 25000,  "lock_days": 30},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "amount": "50,000",  "raw": 50000,  "lock_days": 30},
    "plan_elite":  {"label": "💎 باقة النخبة",      "amount": "100,000", "raw": 100000, "lock_days": 30},
}

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# --- إدارة قاعدة البيانات ---
def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                username TEXT,
                deposit_balance REAL DEFAULT 0,
                locked_profits REAL DEFAULT 0,
                active_plan_price REAL DEFAULT 0,
                profit_lock_start TEXT,
                active_plan_key TEXT
            );
        """)
        # إضافة عمود نوع الباقة إذا كان مفقوداً في النسخة القديمة
        try:
            conn.execute("ALTER TABLE users ADD COLUMN active_plan_key TEXT")
        except:
            pass

# --- الكيبورد الرئيسي (بدون زر النخبة) ---
def get_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💳 محفظتي", callback_data="menu_wallet"),
        types.InlineKeyboardButton("📊 الباقات", callback_data="menu_plans"),
        types.InlineKeyboardButton("📥 إيداع مباشر", callback_data="menu_deposit"),
        types.InlineKeyboardButton("📤 سحب الأرباح", callback_data="menu_withdraw"),
        types.InlineKeyboardButton("💰 استلام أرباح اليوم", callback_data="menu_claim")
    )
    return markup

# --- معالج سحب الأرباح (المنطق الجديد) ---
@bot.callback_query_handler(func=lambda c: c.data == "menu_withdraw")
def handle_withdraw_menu(call):
    uid = call.from_user.id
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    
    if not user or user["active_plan_price"] == 0:
        bot.answer_callback_query(call.id, "⚠️ لا تملك باقة نشطة حالياً")
        return

    # فحص الكود لنوع الباقة والمدة
    p_key = user["active_plan_key"]
    # إذا لم يكن هناك مفتاح مخزن، نفترض أنها باقة 10 آلاف القديمة (15 يوم) كأمان
    lock_duration = PLANS.get(p_key, {"lock_days": 15})["lock_days"]
    plan_name = PLANS.get(p_key, {"label": "باقة نشطة"})["label"]

    if user["deposit_balance"] <= 0:
        # رسالة السحب الجديدة (تلقائية بالكامل)
        text = (
            f"📤 *قسم سحب الأرباح*\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📦 باقتك الحالية: *{plan_name}*\n\n"
            f"تُفتح الأرباح ورأس المال تلقائياً في رصيدك القابل للسحب بعد مرور *{lock_duration} يوم* من تاريخ اشتراكك\\.\n\n"
            f"💡 يمكنك الآن استلام الأرباح اليومية لتضاف إلى الرصيد المقيد حتى انتهاء المدة\\."
        )
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="MarkdownV2", reply_markup=markup)
    else:
        # منطق السحب الفعلي في حال وجود رصيد
        bot.send_message(call.message.chat.id, "ارسل المبلغ الذي تريد سحبه:")

# --- وظيفة فتح الأرباح التلقائية (الخلفية) ---
def unlock_profits_logic():
    now = datetime.now(BAGHDAD_TZ)
    with get_conn() as conn:
        users = conn.execute("SELECT * FROM users WHERE active_plan_price > 0 AND profit_lock_start IS NOT NULL").fetchall()
        for u in users:
            p_key = u["active_plan_key"]
            # هنا الكود "يعرف" أن الـ 10 تفتح بـ 15 والبقية بـ 30
            days_to_wait = PLANS.get(p_key, {"lock_days": 30})["lock_days"]
            
            start_date = datetime.strptime(u["profit_lock_start"], "%Y-%m-%d")
            elapsed_days = (now - BAGHDAD_TZ.localize(start_date)).days
            
            if elapsed_days >= days_to_wait:
                total_amount = u["locked_profits"] + u["active_plan_price"]
                conn.execute("""UPDATE users SET 
                                deposit_balance = deposit_balance + ?, 
                                locked_profits = 0, 
                                active_plan_price = 0, 
                                profit_lock_start = NULL,
                                active_plan_key = NULL 
                                WHERE user_id = ?""", (total_amount, u["user_id"]))
                try:
                    bot.send_message(u["user_id"], f"✅ تم فتح رصيد باقة ({PLANS.get(p_key)['label']}) بنجاح! الرصيد متاح الآن في محفظتك.")
                except: pass

@bot.callback_query_handler(func=lambda c: c.data == "menu_back")
def back_to_main(call):
    bot.edit_message_text("💎 قائمة التحكم الرئيسية:", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

# --- التشغيل ---
if __name__ == "__main__":
    init_db()
    scheduler = BackgroundScheduler(timezone=BAGHDAD_TZ)
    scheduler.add_job(unlock_profits_logic, 'interval', hours=2) # فحص كل ساعتين
    scheduler.start()
    print("Bot is Live on GitHub/Server...")
    bot.infinity_polling()
