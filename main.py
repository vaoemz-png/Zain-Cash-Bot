import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
import pytz
import telebot
from telebot import types

# الإعدادات الأصلية (لا تغيير)
BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8630722565:AAGK-xjCMLvtrvLnzvVbvTGn8vWClxsQh6E")
ADMIN_ID = 122498736
ZAIN_CASH_NUMBER = "07713356493"
DB_FILE = "bot_database.db"

bot = telebot.TeleBot(BOT_TOKEN)

# ── جدار الحماية وقاعدة البيانات ────────────────────────────────────────────────
@contextmanager
def get_conn():
    # إضافة timeout لضمان عدم توقف قاعدة البيانات في السحابة
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# باقاتك الأصلية كما هي في ملفك
PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "amount": "10,000",  "raw": 10000,  "days": 15},
    "plan_silver": {"label": "🥈 الباقة الفضية",   "amount": "25,000",  "raw": 25000,  "days": 30},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "amount": "50,000",  "raw": 50000,  "days": 30},
    "plan_elite":  {"label": "💎 باقة النخبة",      "amount": "100,000", "raw": 100000, "days": 30},
}
PROFIT_RATE = 0.50

def fmt(n):
    return f"{int(n):,}"

# ── القائمة الرئيسية (إضافة زر الأرباح مع الحفاظ على أزرارك) ────────────────────────
def get_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💳 محفظتي", callback_data="menu_wallet"),
        types.InlineKeyboardButton("📊 الباقات", callback_data="menu_plans")
    )
    markup.add(
        types.InlineKeyboardButton("📥 إيداع مباشر", callback_data="menu_deposit"),
        types.InlineKeyboardButton("📤 سحب الأرباح", callback_data="menu_withdraw")
    )
    # الزر الجديد (عقل البوت)
    markup.add(
        types.InlineKeyboardButton("💰 استلام أرباح اليوم", callback_data="claim_daily_profit")
    )
    return markup

# ── معالج الـ Callbacks (إصلاح شامل للأزرار) ───────────────────────────────────
@bot.callback_query_handler(func=lambda call: True)
def handle_all_callbacks(call):
    user_id = call.from_user.id
    now_bgd = datetime.now(BAGHDAD_TZ)
    today_str = now_bgd.strftime("%Y-%m-%d")

    # 1. منطق زر الأرباح (عقل البوت)
    if call.data == "claim_daily_profit":
        if not (10 <= now_bgd.hour < 22):
            bot.answer_callback_query(call.id, "⚠️ الوقت غير مسموح! الاستلام متاح من 10ص إلى 10م فقط.", show_alert=True)
            return

        with get_conn() as conn:
            # البحث عن باقة نشطة (باستخدام استعلامك الأصلي)
            sub = conn.execute(
                "SELECT * FROM subscriptions WHERE user_id=? AND is_active=1 AND expiry_date >= ? ORDER BY id DESC LIMIT 1",
                (user_id, today_str)
            ).fetchone()

            if not sub:
                bot.answer_callback_query(call.id, "❌ ليس لديك باقة نشطة حالياً.", show_alert=True)
                return
            
            if sub['last_daily_payment'] == today_str:
                bot.answer_callback_query(call.id, "✅ استلمت أرباحك لليوم بالفعل!", show_alert=True)
                return

            # الحساب والزيادة (جدار حماية: زيادة مباشرة دون قراءة سابقة لتجنب التصفير)
            daily_profit = round((sub["amount"] * PROFIT_RATE) / sub["duration_days"], 2)
            conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (daily_profit, user_id))
            conn.execute("UPDATE subscriptions SET last_daily_payment=? WHERE id=?", (today_str, sub['id']))
            
            bot.answer_callback_query(call.id, f"🎊 تم إضافة {fmt(daily_profit)} د.ع لمحفظتك!", show_alert=True)

    # 2. إعادة توجيه بقية الأزرار لوظائفك الأصلية (لضمان عمل السحب والإيداع والباقات)
    elif call.data == "menu_plans":
        # عرض الباقات بنفس طريقتك الأصلية
        from main import handle_plans
        handle_plans(call)
    
    elif call.data == "menu_deposit":
        from main import handle_direct_deposit_menu
        handle_direct_deposit_menu(call)
        
    elif call.data == "menu_withdraw":
        from main import handle_withdraw_menu
        handle_withdraw_menu(call)
        
    elif call.data == "menu_wallet":
        from main import handle_wallet
        handle_wallet(call)

    elif call.data == "menu_back":
        bot.edit_message_text(WELCOME_TEXT, call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

# ── تشغيل البوت ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # ملاحظة: تم إيقاف start_scheduler() لأنك طلبت النظام اليدوي (عقل البوت)
    print("البوت يعمل الآن.. نظام الحماية مفعّل.")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
