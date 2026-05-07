import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
import pytz
import telebot
from telebot import types
from pymongo import MongoClient
from apscheduler.schedulers.background import BackgroundScheduler

# ── الإعدادات الأساسية ──────────────────────────────────────────────────────────
BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")

# التوكن الجديد الذي أرسلته
BOT_TOKEN = "8630722565:AAGnOFp-37kwIEdCR6GA5j2EmF7zPTrutOY"

# رابط MongoDB الخاص بك
MONGO_URI = "mongodb+srv://Drahiim:yLg4%R%Saa5Vu3@@cluster0.8bjkzgv.mongodb.net/?appName=Cluster0"

# الاتصال بقاعدة البيانات
client = MongoClient(MONGO_URI)
db = client['drahem_bot']
users_col = db['users']

bot = telebot.TeleBot(BOT_TOKEN)

# ── الإعدادات الإضافية من كودك الأصلي ──────────────────────────────────────────
ADMIN_ID = 122498736
ZAIN_CASH_NUMBER = "07713356493"

# ── القائمة الرئيسية (تم حذف زر نادي النخبة) ──────────────────────────────────────
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

# ── تعديل منطق سحب الأرباح (15 يوم للبرونزية و30 للبقية) ────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "menu_withdraw")
def handle_withdraw_menu(call):
    uid = call.from_user.id
    user = users_col.find_one({"user_id": uid})
    
    if not user or user.get("active_plan_price", 0) == 0:
        bot.answer_callback_query(call.id, "⚠️ لا توجد باقة نشطة حالياً.", show_alert=True)
        return

    # فحص سعر الباقة لتحديد المدة
    p_price = user.get("active_plan_price", 0)
    lock_days = 15 if p_price == 10000 else 30
    
    if user.get("deposit_balance", 0) <= 0:
        text = (
            f"📤 *قسم سحب الأرباح*\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"سيتم فتح رأس مالك وأرباحك تلقائياً بعد مرور *{lock_days} يوم* من تاريخ اشتراكك\\.\n\n"
            f"⚠️ رصيدك القابل للسحب حالياً: *0 د\\.ع*\\."
        )
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, 
                              parse_mode="MarkdownV2", reply_markup=markup)
    else:
        # هنا يكمل كود السحب الأصلي الخاص بك...
        bot.send_message(call.message.chat.id, "أرسل المبلغ الذي تود سحبه:")

# ── استلام الأرباح (يتعرف على المشتركين) ──────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "menu_claim")
def handle_claim(call):
    uid = call.from_user.id
    user = users_col.find_one({"user_id": uid})
    
    if not user or user.get("active_plan_price", 0) == 0:
        bot.answer_callback_query(call.id, "⚠️ يجب أن يكون لديك باقة نشطة لاستلام الأرباح.", show_alert=True)
        return

    now = datetime.now(BAGHDAD_TZ)
    today_str = now.strftime("%Y-%m-%d")
    
    if user.get("profit_claimed_date") == today_str:
        bot.answer_callback_query(call.id, "❌ لقد استلمت أرباح اليوم بالفعل.", show_alert=True)
        return

    # حساب الربح (5%)
    daily_profit = user["active_plan_price"] * 0.05
    users_col.update_one(
        {"user_id": uid},
        {"$inc": {"locked_profits": daily_profit}, "$set": {"profit_claimed_date": today_str}}
    )
    
    bot.edit_message_text(f"✅ تم استلام ربح اليوم: {daily_profit:,.0f} د.ع\nستضاف لمحفظتك عند فتح السحب.", 
                          call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

# ── دالة فحص فتح الأرباح التلقائي ─────────────────────────────────────────────
def check_unlock_profits():
    now = datetime.now(BAGHDAD_TZ)
    active_users = users_col.find({"active_plan_price": {"$gt": 0}})
    for u in active_users:
        needed = 15 if u["active_plan_price"] == 10000 else 30
        start_date_str = u.get("profit_lock_start")
        if start_date_str:
            try:
                start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
                if (now - BAGHDAD_TZ.localize(start_dt)).days >= needed:
                    total = u.get("locked_profits", 0) + u["active_plan_price"]
                    users_col.update_one(
                        {"user_id": u["user_id"]},
                        {"$inc": {"deposit_balance": total}, 
                         "$set": {"locked_profits": 0, "active_plan_price": 0, "profit_lock_start": None}}
                    )
                    try: bot.send_message(u["user_id"], "🎊 خبر سار! انتهت مدة الاستثمار وتم فتح رصيدك بالكامل.")
                    except: pass
            except: pass

# ── باقي الدوال الأصلية من ملفك (تم الحفاظ عليها) ──────────────────────────────
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "أهلاً بك في بوت دراهم الرقمي 🇮🇶", reply_markup=get_main_menu())

@bot.callback_query_handler(func=lambda c: c.data == "menu_back")
def back(call):
    bot.edit_message_text("💎 قائمة التحكم الرئيسية:", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

# ── تشغيل البوت ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # تشغيل الجدولة لفتح الأرباح
    scheduler = BackgroundScheduler(timezone=BAGHDAD_TZ)
    scheduler.add_job(check_unlock_profits, 'interval', hours=1, id='unlock_task', replace_existing=True)
    scheduler.start()
    
    print("Bot is working...")
    bot.infinity_polling()
