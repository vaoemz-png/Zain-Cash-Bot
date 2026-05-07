import os
import pytz
from datetime import datetime
import telebot
from telebot import types
from pymongo import MongoClient
from apscheduler.schedulers.background import BackgroundScheduler

# --- الإعدادات الأساسية ---
BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")
# التوكن يتم جلبه من إعدادات السيرفر للأمان
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8630722565:AAGK-xjCMLvtrvLnzvVbvTGn8vWClxsQh6E")

# رابط قاعدة البيانات الخاص بك مدمج هنا مباشرة
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

# --- القائمة الرئيسية (تم حذف زر نادي النخبة) ---
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

# --- إصلاح زر استلام الأرباح (التعرف على الخطة النشطة) ---
@bot.callback_query_handler(func=lambda c: c.data == "menu_claim")
def handle_claim(call):
    uid = call.from_user.id
    user = users_col.find_one({"user_id": uid})
    
    # التحقق هل المستخدم لديه باقة نشطة (سعره أكبر من 0)
    if not user or user.get("active_plan_price", 0) == 0:
        bot.answer_callback_query(call.id, "⚠️ ليس لديك باقة نشطة! اشترك أولاً لتتمكن من جمع الأرباح.", show_alert=True)
        return

    now = datetime.now(BAGHDAD_TZ)
    today_str = now.strftime("%Y-%m-%d")
    
    if user.get("profit_claimed_date") == today_str:
        bot.answer_callback_query(call.id, "❌ لقد استلمت أرباح اليوم بالفعل. عد غداً!", show_alert=True)
        return

    # حساب الربح اليومي (5% من قيمة الباقة)
    daily_profit = user["active_plan_price"] * 0.05
    
    # تحديث البيانات في MongoDB
    users_col.update_one(
        {"user_id": uid},
        {
            "$inc": {"locked_profits": daily_profit},
            "$set": {"profit_claimed_date": today_str}
        }
    )
    
    bot.edit_message_text(f"✅ تم استلام ربح اليوم بقيمة {daily_profit:,.0f} د.ع\nتمت إضافتها إلى أرباحك المقيدة حتى موعد السحب.", 
                          call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

# --- منطق سحب الأرباح المعدل (15 يوم للـ 10 و 30 للبقية) ---
@bot.callback_query_handler(func=lambda c: c.data == "menu_withdraw")
def handle_withdraw_menu(call):
    uid = call.from_user.id
    user = users_col.find_one({"user_id": uid})
    
    if not user or user.get("active_plan_price", 0) == 0:
        bot.answer_callback_query(call.id, "⚠️ لا توجد باقة استثمارية نشطة حالياً.", show_alert=True)
        return

    # فحص الكود للمدة تلقائياً: إذا السعر 10000 -> 15 يوم، غير ذلك -> 30 يوم
    p_price = user.get("active_plan_price", 0)
    lock_days = 15 if p_price == 10000 else 30
    
    p_key = user.get("active_plan_key", "plan_bronze")
    p_label = PLANS.get(p_key, {"label": "باقتك الاستثمارية"})["label"]

    if user.get("deposit_balance", 0) <= 0:
        text = (
            f"📤 *قسم سحب الأرباح*\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📦 نوع الباقة: *{p_label}*\n\n"
            f"تُفتح أرباحك ورأس مالك تلقائياً بعد مرور *{lock_days} يوم* من تاريخ الاشتراك\\.\n\n"
            f"⚠️ رصيدك المتاح للسحب حالياً هو *0 د\\.ع*\\."
        )
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="MarkdownV2", reply_markup=markup)
    else:
        # كود السحب في حال وجود رصيد
        bot.send_message(call.message.chat.id, "أرسل المبلغ الذي تود سحبه:")

# --- وظيفة فتح الأرباح التلقائية (خلفية) ---
def check_unlock_profits():
    now = datetime.now(BAGHDAD_TZ)
    active_users = users_col.find({"active_plan_price": {"$gt": 0}})
    
    for u in active_users:
        needed = 15 if u["active_plan_price"] == 10000 else 30
        start_date_str = u.get("profit_lock
