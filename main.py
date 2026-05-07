import os
import pytz
from datetime import datetime
import telebot
from telebot import types
try:
    from pymongo import MongoClient
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError:
    print("Error: Please add pymongo and apscheduler to requirements.txt")

# ── الإعدادات الثابتة ──────────────────────────────────────────────────────────
BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")
BOT_TOKEN = "8630722565:AAGnOFp-37kwIEdCR6GA5j2EmF7zPTrutOY"
MONGO_URI = "mongodb+srv://Drahiim:yLg4%R%Saa5Vu3@@cluster0.8bjkzgv.mongodb.net/?appName=Cluster0"

client = MongoClient(MONGO_URI)
db = client['drahem_bot']
users_col = db['users']

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# ── الباقات ──────────────────────────────────────────────────────────────────
PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "price": 10000, "lock_days": 15},
    "plan_silver": {"label": "🥈 الباقة الفضية",   "price": 25000, "lock_days": 30},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "price": 50000, "lock_days": 30},
    "plan_elite":  {"label": "💎 باقة النخبة",     "price": 100000, "lock_days": 30},
}

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

# ── معالجة استلام الأرباح ──────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "menu_claim")
def handle_claim(call):
    uid = call.from_user.id
    user = users_col.find_one({"user_id": uid})
    
    if not user or user.get("active_plan_price", 0) == 0:
        bot.answer_callback_query(call.id, "⚠️ اشترك في باقة أولاً لتتمكن من جمع الأرباح.", show_alert=True)
        return

    now = datetime.now(BAGHDAD_TZ)
    today_str = now.strftime("%Y-%m-%d")
    
    if user.get("profit_claimed_date") == today_str:
        bot.answer_callback_query(call.id, "❌ استلمت أرباحك لليوم بالفعل.", show_alert=True)
        return

    daily_profit = user["active_plan_price"] * 0.05
    users_col.update_one(
        {"user_id": uid},
        {"$inc": {"locked_profits": daily_profit}, "$set": {"profit_claimed_date": today_str}}
    )
    bot.edit_message_text(f"✅ تم إضافة {daily_profit:,.0f} د.ع لأرباحك المقيدة.", 
                          call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

# ── معالجة السحب (المنطق الجديد) ──────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "menu_withdraw")
def handle_withdraw_menu(call):
    uid = call.from_user.id
    user = users_col.find_one({"user_id": uid})
    
    if not user or user.get("active_plan_price", 0) == 0:
        bot.answer_callback_query(call.id, "⚠️ لا توجد باقة نشطة حالياً.", show_alert=True)
        return

    p_price = user.get("active_plan_price", 0)
    lock_days = 15 if p_price == 10000 else 30
    
    if user.get("deposit_balance", 0) <= 0:
        text = (
            f"📤 *قسم سحب الأرباح*\\n"
            f"━━━━━━━━━━━━━━━━━\\n"
            f"تُفتح أرباحك تلقائياً بعد مرور *{lock_days} يوم* من الاشتراك\\.\\n\\n"
            f"⚠️ رصيدك المتاح للسحب الآن: *0 د\\.ع*\\."
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, 
                              parse_mode="MarkdownV2", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")))
    else:
        bot.send_message(call.message.chat.id, "أرسل المبلغ الذي تود سحبه:")

# ── فحص فتح الأرباح ───────────────────────────────────────────────────────────
def check_unlock_profits():
    now = datetime.now(BAGHDAD_TZ)
    for u in users_col.find({"active_plan_price": {"$gt": 0}}):
        needed = 15 if u["active_plan_price"] == 10000 else 30
        start_date_str = u.get("profit_lock_start")
        if start_date_str:
            try:
                start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
                if (now - BAGHDAD_TZ.localize(start_dt)).days >= needed:
                    total = u.get("locked_profits", 0) + u["active_plan_price"]
                    users_col.update_one({"user_id": u["user_id"]}, 
                        {"$inc": {"deposit_balance": total}, "$set": {"locked_profits": 0, "active_plan_price": 0, "profit_lock_start": None}})
                    bot.send_message(u["user_id"], "🎊 تم فتح رصيدك وإضافته لمحفظتك بنجاح!")
            except: pass

@bot.callback_query_handler(func=lambda c: c.data == "menu_back")
def back(call):
    bot.edit_message_text("💎 القائمة الرئيسية:", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "أهلاً بك في بوت دراهم 🇮🇶", reply_markup=get_main_menu())

# ── التشغيل ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        scheduler = BackgroundScheduler(timezone=BAGHDAD_TZ)
        scheduler.add_job(check_unlock_profits, 'interval', hours=1, id='unlock_job', replace_existing=True)
        scheduler.start()
    except: pass
    
    print("Bot is LIVE...")
    bot.infinity_polling(timeout=60, long_polling_timeout=5)
