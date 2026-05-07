import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
import pytz
import telebot
from telebot import types

# الإعدادات الأصلية
BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8630722565:AAGK-xjCMLvtrvLnzvVbvTGn8vWClxsQh6E")
ADMIN_ID = 122498736
DB_FILE = "bot_database.db"

bot = telebot.TeleBot(BOT_TOKEN)

# ── جدار الحماية المالي (للسحابة الخارجية) ────────────────────────────────────────
@contextmanager
def get_conn():
    # استخدام timeout عالي لضمان عدم ضياع البيانات عند ضغط السحابة
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

# الباقات المتبقية بعد حذف النخبة (البرونزية 15 يوم والبقية 30 يوم)
PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "amount": "10,000",  "raw": 10000,  "days": 15},
    "plan_silver": {"label": "🥈 الباقة الفضية",   "amount": "25,000",  "raw": 25000,  "days": 30},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "amount": "50,000",  "raw": 50000,  "days": 30},
}
PROFIT_RATE = 0.50

def fmt(n):
    return f"{int(n):,}"

# ── القائمة الرئيسية ──────────────────────────────────────────────────────────
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

# ── معالج الأزرار الموحد (عقل البوت) ──────────────────────────────────────────
@bot.callback_query_handler(func=lambda call: True)
def handle_all_callbacks(call):
    user_id = call.from_user.id
    now_bgd = datetime.now(BAGHDAD_TZ)
    today_str = now_bgd.strftime("%Y-%m-%d")

    # زر استلام الأرباح (المحرك الأساسي)
    if call.data == "claim_daily_profit":
        # شرط الوقت (10ص - 10م)
        if not (10 <= now_bgd.hour < 22):
            bot.answer_callback_query(call.id, "⚠️ الوقت غير مسموح (10ص - 10م). أرباحك اليوم ذهبت للشركة.", show_alert=True)
            return

        with get_conn() as conn:
            sub = conn.execute("SELECT * FROM subscriptions WHERE user_id=? AND is_active=1 AND expiry_date >= ?", 
                               (user_id, today_str)).fetchone()
            
            if not sub:
                bot.answer_callback_query(call.id, "❌ لا يوجد اشتراك نشط حالياً.", show_alert=True)
                return
            
            if sub['last_daily_payment'] == today_str:
                bot.answer_callback_query(call.id, "✅ استلمت أرباح اليوم بالفعل!", show_alert=True)
                return

            # الحساب المالي الدقيق والزيادة التراكمية (جدار الحماية)
            daily_profit = round((sub["amount"] * PROFIT_RATE) / sub["duration_days"], 2)
            
            # تنفيذ العملية المالية مباشرة في DB لمنع التصفير
            conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (daily_profit, user_id))
            conn.execute("UPDATE subscriptions SET last_daily_payment=? WHERE id=?", (today_str, sub['id']))
            
            bot.answer_callback_query(call.id, f"🎊 تم إضافة أرباح اليوم: {fmt(daily_profit)} د.ع", show_alert=True)

    elif call.data == "menu_plans":
        markup = types.InlineKeyboardMarkup(row_width=1)
        for key, p in PLANS.items():
            markup.add(types.InlineKeyboardButton(f"{p['label']} | {p['amount']} د.ع", callback_data=f"buy_{key}"))
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
        bot.edit_message_text("📊 اختر باقة الاستثمار:", call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data == "menu_wallet":
        with get_conn() as conn:
            user = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        bot.answer_callback_query(call.id, f"رصيدك الحالي: {fmt(user['balance'] if user else 0)} د.ع", show_alert=True)

    elif call.data == "menu_back":
        bot.edit_message_text("💎 أهلاً بك في متجر دراهم الرقمي", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

# ── إعادة تهيئة النظام (تصفير كامل) ───────────────────────────────────────────
def init_db():
    with get_conn() as conn:
        # حذف الجداول القديمة تماماً للبدء من الصفر
        conn.execute("DROP TABLE IF EXISTS users")
        conn.execute("DROP TABLE IF EXISTS subscriptions")
        conn.execute("DROP TABLE IF EXISTS transactions")
        
        # إنشاء الجداول الجديدة بنظام الحماية
        conn.executescript("""
            CREATE TABLE users (
                user_id   INTEGER PRIMARY KEY,
                name      TEXT,
                username  TEXT,
                balance   REAL DEFAULT 0
            );
            CREATE TABLE subscriptions (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id            INTEGER NOT NULL,
                plan_name          TEXT NOT NULL,
                amount             REAL NOT NULL,
                duration_days      INTEGER NOT NULL,
                expiry_date        TEXT NOT NULL,
                is_active          INTEGER DEFAULT 1,
                last_daily_payment TEXT DEFAULT NULL
            );
            CREATE TABLE transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                type        TEXT,
                amount      REAL,
                status      TEXT DEFAULT 'pending'
            );
        """)

if __name__ == "__main__":
    init_db() # سيقوم بتصفير كل شيء عند أول تشغيل
    print("تم تصفير النظام بنجاح.. البوت يعمل الآن بنظام الحماية المالي.")
    bot.infinity_polling()
