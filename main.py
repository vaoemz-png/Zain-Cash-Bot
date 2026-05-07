import os
import pytz
from datetime import datetime
import telebot
from telebot import types
from pymongo import MongoClient
from apscheduler.schedulers.background import BackgroundScheduler

# --- الإعدادات الأساسية ---
BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")

# التوكن الجديد الذي أرسلته
BOT_TOKEN = "8630722565:AAGnOFp-37kwIEdCR6GA5j2EmF7zPTrutOY"

# رابط MongoDB الخاص بك
MONGO_URI = "mongodb+srv://Drahiim:yLg4%R%Saa5Vu3@@cluster0.8bjkzgv.mongodb.net/?appName=Cluster0"

client = MongoClient(MONGO_URI)
db = client['drahem_bot']
users_col = db['users']

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# --- تعريف الباقات وفحص مدد القفل ---
PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "price": 10000, "lock_days": 15},
    "plan_silver": {"label": "🥈 الباقة الفضية",   "price": 25000, "lock_days": 30},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "price": 50000, "lock_days": 30},
    "plan_elite":  {"label": "💎 باقة النخبة",     "price": 100000, "lock_days": 30},
}

# --- القائمة الرئيسية (تم حذف زر نادي النخبة كما طلبت) ---
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

# --- زر استلام الأرباح ---
@bot.callback_query_handler(func=lambda c: c.data == "menu_claim")
def handle_claim(call):
    uid = call.from_user.id
    user = users_col.find_one({"user_id": uid})
    
    if not user or user.get("active_plan_price", 0) == 0:
        bot.answer_callback_query(call.id, "⚠️ ليس لديك باقة نشطة حالياً.", show_alert=True)
        return

    now = datetime.now(BAGHDAD_TZ)
    today_str = now.strftime("%Y-%m-%d")
    
    if user.get("profit_claimed_date") == today_str:
        bot.answer_callback_query(call.id, "❌ لقد استلمت أرباحك لليوم بالفعل.", show_alert=True)
        return

    daily_profit = user["active_plan_price"] * 0.05
    
    users_col.update_one(
        {"user_id": uid},
        {
            "$inc": {"locked_profits": daily_profit},
            "$set": {"profit_claimed_date": today_str}
        }
    )
    
    bot.edit_message_text(f"✅ تم استلام ربح اليوم: {daily_profit:,.0f} د.ع\nستُضاف للمحفظة فور انتهاء مدة الباقة.", 
                          call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

# --- منطق سحب الأرباح (15 يوم للبرونزية و30 للبقية) ---
@bot.callback_query_handler(func=lambda c: c.data == "menu_withdraw")
def handle_withdraw_menu(call):
    uid = call.from_user.id
    user = users_col.find_one({"user_id": uid})
    
    if not user or user.get("active_plan_price", 0) == 0:
        bot.answer_callback_query(call.id, "⚠️ اشترك في باقة أولاً لتتمكن من السحب.", show_alert=True)
        return

    p_price = user.get("active_plan_price", 0)
    lock_days = 15 if p_price == 10000 else 30
    
    p_key = user.get("active_plan_key", "plan_bronze")
    p_label = PLANS.get(p_key, {"label": "الباقة النشطة"})["label"]

    if user.get("deposit_balance", 0) <= 0:
        text = (
            f"📤 *قسم سحب الأرباح*\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📦 نوع الباقة: *{p_label}*\n\n"
            f"تُفتح أرباحك ورأس مالك تلقائياً بعد مرور *{lock_days} يوم* من الاشتراك\\.\n\n"
            f"⚠️ الرصيد المتاح للسحب الآن: *0 د\\.ع*\\."
        )
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="MarkdownV2", reply_markup=markup)
    else:
        bot.send_message(call.message.chat.id, "أرسل المبلغ الذي تود سحبه:")

# --- فحص انتهاء مدة الباقة تلقائياً ---
def check_unlock_profits():
    now = datetime.now(BAGHDAD_TZ)
    active_users = users_col.find({"active_plan_price": {"$gt": 0}})
    
    for u in active_users:
        needed = 15 if u["active_plan_price"] == 10000 else 30
        start_date_str = u.get("profit_lock_start")
        if start_date_str:
            try:
                start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
                days_passed = (now - BAGHDAD_TZ.localize(start_dt)).days
                
                if days_passed >= needed:
                    total_to_unlock = u.get("locked_profits", 0) + u["active_plan_price"]
                    users_col.update_one(
                        {"user_id": u["user_id"]},
                        {
                            "$inc": {"deposit_balance": total_to_unlock},
                            "$set": {
                                "locked_profits": 0, 
                                "active_plan_price": 0, 
                                "profit_lock_start": None,
                                "active_plan_key": None
                            }
                        }
                    )
                    try: bot.send_message(u["user_id"], "🎊 مبروك! انتهت مدة استثمارك وتم تحويل الرصيد لمحفظتك.")
                    except: pass
            except: pass

@bot.callback_query_handler(func=lambda c: c.data == "menu_back")
def back_home(call):
    bot.edit_message_text("💎 قائمة التحكم الرئيسية:", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

@bot.message_handler(commands=['start'])
def start_cmd(message):
    bot.send_message(message.chat.id, "أهلاً بك في بوت دراهم الرقمي 🇮🇶", reply_markup=get_main_menu())

# --- بدء التشغيل ---
if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone=BAGHDAD_TZ)
    scheduler.add_job(check_unlock_profits, 'interval', hours=1)
    scheduler.start()
    print("Bot is LIVE...")
    bot.infinity_polling()
                
