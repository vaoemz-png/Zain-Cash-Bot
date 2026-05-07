import os
import sqlite3
import threading
from datetime import datetime, timedelta
import pytz
import telebot
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler

# ── الإعدادات الأساسية ──────────────────────────────────────────────────────────
BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8630722565:AAGK-xjCMLvtrvLnzvVbvTGn8vWClxsQh6E")
ADMIN_ID = 122498736
ZAIN_CASH_NUMBER = "07713356493"
DB_FILE = "bot_database.db"

# ── الباقات المحدثة بالمنطق الجديد ──────────────────────────────────────────────
# باقة الـ 10 (البرونزية) قفل 15 يوم، البقية 30 يوم
PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "amount": "10,000",  "raw": 10000,  "lock_days": 15},
    "plan_silver": {"label": "🥈 الباقة الفضية",   "amount": "25,000",  "raw": 25000,  "lock_days": 30},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "amount": "50,000",  "raw": 50000,  "lock_days": 30},
    "plan_elite":  {"label": "💎 باقة النخبة",      "amount": "100,000", "raw": 100000, "lock_days": 30},
}

PROFIT_MULT = 1.50  # عائد 150%
CLAIM_START_H = 10
CLAIM_END_H = 22

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# ── قاعدة البيانات ────────────────────────────────────────────────────────────
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
                profit_claimed_date TEXT,
                profit_lock_start TEXT,
                active_plan_key TEXT
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT,
                amount REAL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
        """)

# ── الكيبوردات (بعد حذف زر النادي) ──────────────────────────────────────────────
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

# ── منطق السحب المحدث ──────────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "menu_withdraw")
def handle_withdraw_menu(call):
    uid = call.from_user.id
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    
    if not user or user["active_plan_price"] == 0:
        bot.edit_message_text("⚠️ لا توجد باقة نشطة حالياً للسحب.", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())
        return

    plan_key = user["active_plan_key"]
    # الحصول على عدد أيام القفل بناءً على نوع الباقة من القاموس
    lock_days = PLANS.get(plan_key, {"lock_days": 15})["lock_days"]
    
    if user["deposit_balance"] <= 0:
        text = (
            f"⚠️ *رصيدك القابل للسحب صفر*\n\n"
            f"📦 باقتك النشطة: *{PLANS[plan_key]['label']}* ✅\n\n"
            f"لا يوجد رصيد متاح للسحب حالياً. استلم أرباحك يومياً "
            f"وانتظر انتهاء مدة الاستثمار ( {lock_days} يوم ) لتُفتح وتُحوَّل للرصيد."
        )
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)
    else:
        # هنا يكمل إجراءات السحب إذا توفر رصيد
        pass

# ── تعديل التفعيل التلقائي (لضمان حفظ نوع الباقة) ──────────────────────────────
def activate_plan(conn, uid, plan_key):
    plan = PLANS[plan_key]
    today = datetime.now(BAGHDAD_TZ).strftime("%Y-%m-%d")
    conn.execute(
        """UPDATE users SET 
           active_plan_price = ?, 
           active_plan_key = ?, 
           profit_lock_start = ? 
           WHERE user_id = ?""",
        (plan["raw"], plan_key, today, uid)
    )

# ── الجدولة الزمنية لفتح الأرباح بناءً على نوع الباقة ──────────────────────────────
def unlock_profits_task():
    now = datetime.now(BAGHDAD_TZ)
    with get_conn() as conn:
        users = conn.execute("SELECT * FROM users WHERE active_plan_price > 0 AND profit_lock_start IS NOT NULL").fetchall()
        for u in users:
            plan_key = u["active_plan_key"]
            lock_days = PLANS.get(plan_key, {"lock_days": 15})["lock_days"]
            start_date = datetime.strptime(u["profit_lock_start"], "%Y-%m-%d")
            
            # فحص هل انتهت المدة (15 أو 30 يوم)
            if (now - BAGHDAD_TZ.localize(start_date)).days >= lock_days:
                total_unlocked = u["locked_profits"] + u["active_plan_price"]
                conn.execute(
                    "UPDATE users SET deposit_balance = deposit_balance + ?, locked_profits = 0, active_plan_price = 0 WHERE user_id = ?",
                    (total_unlocked, u["user_id"])
                )
                print(f"Unlocked for {u['user_id']}")

# ── التشغيل ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    scheduler = BackgroundScheduler(timezone=BAGHDAD_TZ)
    scheduler.add_job(unlock_profits_task, 'interval', hours=1)
    scheduler.start()
    bot.infinity_polling()
