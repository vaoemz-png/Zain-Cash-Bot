import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
import pytz
import telebot
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler

BAGHDAD_TZ = pytz.timezone("Asia/Baghdad")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8630722565:AAGK-xjCMLvtrvLnzvVbvTGn8vWClxsQh6E")
ADMIN_ID = 122498736
ZAIN_CASH_NUMBER = "07713356493"
DB_FILE = "bot_database.db"

bot = telebot.TeleBot(BOT_TOKEN)

# ── States ─────────────────────────────────────────────────────────────────────
user_states = {}
user_data = {}

STATE_IDLE = "idle"
STATE_WAITING_DEPOSIT_AMOUNT = "waiting_deposit_amount"
STATE_WAITING_DEPOSIT_PHOTO = "waiting_deposit_photo"
STATE_WAITING_PLAN_PHOTO = "waiting_plan_photo"
STATE_WAITING_WITHDRAW_AMOUNT = "waiting_withdraw_amount"
STATE_WAITING_WITHDRAW_PHONE = "waiting_withdraw_phone"
WITHDRAW_METHODS = {
    "withdraw_zaincash":  {"label": "💳 زين كاش",          "short": "زين كاش"},
    "withdraw_asiacell":  {"label": "📱 آسياسيل تحويل",    "short": "آسياسيل"},
}

WELCOME_TEXT = (
    "💎 أهلاً بك في متجر دراهم الرقمي\n"
    "• البوت الأول في العراق لتحويل النقاط إلى أرباح حقيقية 🇮🇶\n\n"
    "اختر من الأزرار أدناه:"
)

PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "amount": "10,000",  "raw": 10000,  "days": 15},
    "plan_silver": {"label": "🥈 الباقة الفضية",   "amount": "25,000",  "raw": 25000,  "days": 30},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "amount": "50,000",  "raw": 50000,  "days": 30},
    "plan_elite":  {"label": "💎 باقة النخبة",      "amount": "100,000", "raw": 100000, "days": 30},
}

PROFIT_RATE = 0.50   # 50%

TYPE_LABELS = {
    "deposit":      "📥 إيداع",
    "withdrawal":   "📤 سحب",
    "plan_payment": "📊 اشتراك باقة",
    "plan_profit":  "💹 أرباح باقة",
    "plan_deposit": "📥 إيداع لباقة",
}

STATUS_LABELS = {
    "pending":  "⏳ قيد المراجعة",
    "approved": "✅ مقبول",
    "rejected": "❌ مرفوض",
}


# ── Database ───────────────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                name      TEXT    DEFAULT '',
                username  TEXT    DEFAULT '',
                balance   REAL    DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                type        TEXT    NOT NULL,
                amount      REAL    NOT NULL,
                description TEXT    DEFAULT '',
                status      TEXT    DEFAULT 'pending',
                created_at  TEXT    DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id            INTEGER NOT NULL,
                plan_name          TEXT    NOT NULL,
                plan_key           TEXT    NOT NULL,
                amount             REAL    NOT NULL,
                duration_days      INTEGER NOT NULL,
                start_date         TEXT    NOT NULL,
                expiry_date        TEXT    NOT NULL,
                is_active          INTEGER DEFAULT 1,
                profit_paid        INTEGER DEFAULT 0,
                last_daily_payment TEXT    DEFAULT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
        """)
        # Migrate existing DB: add column if missing
        try:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN last_daily_payment TEXT DEFAULT NULL")
        except Exception:
            pass   # Column already exists
    print("Database initialized.")


def ensure_user(conn, user_id, name="", username=""):
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, name, username, balance) VALUES (?, ?, ?, 0)",
        (user_id, name, username),
    )
    if name:
        conn.execute(
            "UPDATE users SET name=?, username=? WHERE user_id=?",
            (name, username, user_id),
        )


def get_user(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def add_transaction(conn, user_id, txn_type, amount, description="", status="pending"):
    cur = conn.execute(
        "INSERT INTO transactions (user_id, type, amount, description, status) VALUES (?,?,?,?,?)",
        (user_id, txn_type, amount, description, status),
    )
    return cur.lastrowid


def update_transaction_status(conn, txn_id, status):
    conn.execute("UPDATE transactions SET status=? WHERE id=?", (status, txn_id))


def get_transaction(conn, txn_id):
    return conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()


def get_last_transactions(conn, user_id, limit=10):
    return conn.execute(
        "SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()


def add_subscription(conn, user_id, plan_key, amount, duration_days):
    plan = PLANS[plan_key]
    now = datetime.now()
    expiry = now + timedelta(days=duration_days)
    conn.execute(
        """INSERT INTO subscriptions
           (user_id, plan_name, plan_key, amount, duration_days, start_date, expiry_date)
           VALUES (?,?,?,?,?,?,?)""",
        (user_id, plan["label"], plan_key, amount, duration_days,
         now.strftime("%Y-%m-%d"), expiry.strftime("%Y-%m-%d")),
    )


def get_active_subscription(conn, user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    return conn.execute(
        """SELECT * FROM subscriptions
           WHERE user_id=? AND is_active=1 AND expiry_date >= ?
           ORDER BY id DESC LIMIT 1""",
        (user_id, today),
    ).fetchone()


def get_expired_unpaid_subscriptions(conn, user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    return conn.execute(
        """SELECT * FROM subscriptions
           WHERE user_id=? AND is_active=1 AND expiry_date < ? AND profit_paid=0""",
        (user_id, today),
    ).fetchall()


def credit_expired_profits(conn, user_id):
    """Return the original principal for expired subscriptions.
    Daily profits have already been distributed by the scheduler,
    so only the invested amount is returned at expiry."""
    expired = get_expired_unpaid_subscriptions(conn, user_id)
    messages = []
    for sub in expired:
        principal = sub["amount"]
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?",
                     (principal, user_id))
        conn.execute(
            "UPDATE subscriptions SET is_active=0, profit_paid=1 WHERE id=?", (sub["id"],)
        )
        add_transaction(
            conn, user_id, "plan_profit", principal,
            description=f"استرداد رأس المال — {sub['plan_name']}",
            status="approved",
        )
        messages.append(
            f"✅ انتهت باقتك *{sub['plan_name']}*!\n"
            f"💰 تم استرداد رأس المال: *{fmt(principal)} د.ع*\n"
            f"📈 الأرباح اليومية أُضيفت طوال مدة الباقة."
        )
    return messages


# ── Helpers ────────────────────────────────────────────────────────────────────

def fmt(n):
    return f"{int(n):,}"


def expiry_from_now(days):
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


# ── Keyboards ──────────────────────────────────────────────────────────────────

def get_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💳 محفظتي",       callback_data="menu_wallet"),
        types.InlineKeyboardButton("📊 الباقات",       callback_data="menu_plans"),
        types.InlineKeyboardButton("📥 إيداع مباشر",  callback_data="menu_deposit"),
        types.InlineKeyboardButton("📤 سحب الأرباح",  callback_data="menu_withdraw"),
    )
    return markup


def get_wallet_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📜 سجل العمليات", callback_data="wallet_history"),
        types.InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="menu_back"),
    )
    return markup


def get_plans_keyboard(user_balance):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for key, plan in PLANS.items():
        can_afford = user_balance >= plan["raw"]
        label = (
            f"✅ {plan['label']} | {plan['amount']} د.ع"
            if can_afford
            else f"{plan['label']} | {plan['amount']} د.ع"
        )
        markup.add(types.InlineKeyboardButton(label, callback_data=key))
    markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
    return markup


def get_buy_now_keyboard(plan_key):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("✅ شراء الآن من رصيدي", callback_data=f"buy_now_{plan_key}"),
        types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"),
    )
    return markup


def get_withdraw_method_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    for key, method in WITHDRAW_METHODS.items():
        markup.add(types.InlineKeyboardButton(method["label"], callback_data=key))
    markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
    return markup


def get_withdraw_confirm_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("✅ تأكيد طلب السحب", callback_data="withdraw_confirm"),
        types.InlineKeyboardButton("❌ إلغاء", callback_data="menu_back"),
    )
    return markup


def get_no_plan_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📊 عرض الباقات", callback_data="menu_plans"))
    return markup


def get_back_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="menu_back"))
    return markup


def get_admin_keyboard(user_id, txn_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ قبول",  callback_data=f"approve_{user_id}_{txn_id}"),
        types.InlineKeyboardButton("❌ رفض",   callback_data=f"reject_{user_id}_{txn_id}"),
    )
    return markup


def send_main_menu(chat_id, message_id=None):
    if message_id:
        try:
            bot.edit_message_text(WELCOME_TEXT, chat_id=chat_id,
                                  message_id=message_id, reply_markup=get_main_menu())
            return
        except Exception:
            pass
    bot.send_message(chat_id, WELCOME_TEXT, reply_markup=get_main_menu())


def build_wallet_text(user, sub):
    balance = fmt(user["balance"])
    plan_name = sub["plan_name"] if sub else "لا توجد"
    expiry = sub["expiry_date"] if sub else "—"
    remaining = "—"
    if sub:
        delta = datetime.strptime(sub["expiry_date"], "%Y-%m-%d") - datetime.now()
        remaining = f"{max(delta.days, 0)} يوم"
    return (
        "💳 *محفظتك الرقمية*\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"💰 الرصيد الحالي: *{balance} د.ع*\n"
        f"📊 الباقة النشطة: *{plan_name}*\n"
        f"⏳ تاريخ الانتهاء: *{expiry}*\n"
        f"📆 الأيام المتبقية: *{remaining}*\n"
        f"🆔 معرف الحساب: `{user['user_id']}`"
    )


# ── /start ─────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def handle_start(message):
    user_id = message.from_user.id
    user_states[user_id] = STATE_IDLE
    user_data[user_id] = {}
    name = message.from_user.full_name or ""
    username = message.from_user.username or ""
    with get_conn() as conn:
        ensure_user(conn, user_id, name, username)
    bot.send_message(message.chat.id, WELCOME_TEXT, reply_markup=get_main_menu())


# ── Back ───────────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data == "menu_back")
def handle_back(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    user_states[user_id] = STATE_IDLE
    user_data[user_id] = {}
    send_main_menu(call.message.chat.id, call.message.message_id)


# ── 💳 Wallet ──────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data == "menu_wallet")
def handle_wallet(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    user_states[user_id] = STATE_IDLE

    with get_conn() as conn:
        ensure_user(conn, user_id, call.from_user.full_name, call.from_user.username or "")
        # Credit any expired plan profits automatically
        profit_msgs = credit_expired_profits(conn, user_id)
        user = get_user(conn, user_id)
        sub = get_active_subscription(conn, user_id)

    text = build_wallet_text(user, sub)
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode="Markdown", reply_markup=get_wallet_keyboard())
    except Exception:
        bot.send_message(call.message.chat.id, text, parse_mode="Markdown",
                         reply_markup=get_wallet_keyboard())

    # Notify about credited profits
    for msg in profit_msgs:
        bot.send_message(call.message.chat.id, msg, parse_mode="Markdown")


# ── 📜 Transaction history ─────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data == "wallet_history")
def handle_history(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id

    with get_conn() as conn:
        txns = get_last_transactions(conn, user_id, 10)

    if not txns:
        text = "📜 *سجل العمليات*\n\nلا توجد عمليات مسجلة بعد."
    else:
        lines = ["📜 *آخر 10 عمليات*\n━━━━━━━━━━━━━━━━━"]
        for t in txns:
            label = TYPE_LABELS.get(t["type"], t["type"])
            status = STATUS_LABELS.get(t["status"], t["status"])
            lines.append(f"{label}: *{fmt(t['amount'])} د.ع* — {status}\n🗓 {t['created_at'][:10]}")
        text = "\n\n".join(lines)

    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode="Markdown", reply_markup=get_back_keyboard())
    except Exception:
        bot.send_message(call.message.chat.id, text, parse_mode="Markdown",
                         reply_markup=get_back_keyboard())


# ── 📊 Plans ───────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data == "menu_plans")
def handle_plans(call):
    bot.answer_callback_query(call.id)
    user_states[call.from_user.id] = STATE_IDLE

    with get_conn() as conn:
        ensure_user(conn, call.from_user.id)
        user = get_user(conn, call.from_user.id)

    text = (
        "📊 *الباقات المتاحة*\n\n"
        f"💰 رصيدك الحالي: *{fmt(user['balance'])} د.ع*\n\n"
        "• نسبة الأرباح: *50%* على المبلغ المستثمر\n"
        "• الباقة البرونزية: مدة *15 يوم*\n"
        "• باقي الباقات: مدة *30 يوم*\n\n"
        "اختر الباقة المناسبة:"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode="Markdown",
                              reply_markup=get_plans_keyboard(user["balance"]))
    except Exception:
        bot.send_message(call.message.chat.id, text, parse_mode="Markdown",
                         reply_markup=get_plans_keyboard(user["balance"]))


@bot.callback_query_handler(func=lambda call: call.data in PLANS)
def handle_plan_selection(call):
    user_id = call.from_user.id
    plan_key = call.data
    plan = PLANS[plan_key]
    bot.answer_callback_query(call.id)

    with get_conn() as conn:
        ensure_user(conn, user_id, call.from_user.full_name or "", call.from_user.username or "")
        user = get_user(conn, user_id)

    balance  = user["balance"]
    cost     = plan["raw"]
    expected = fmt(int(cost * PROFIT_RATE))

    if balance >= cost:
        # ── Case A: sufficient balance — offer instant purchase ───────────────
        user_states[user_id] = STATE_IDLE
        user_data[user_id] = {
            "plan_key":   plan_key,
            "plan_label": plan["label"],
            "plan_raw":   cost,
            "plan_days":  plan["days"],
        }
        text = (
            f"📦 *{plan['label']}*\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"💰 تكلفة الباقة: *{plan['amount']} د.ع*\n"
            f"💹 الربح المتوقع الإجمالي: *{expected} د.ع*\n"
            f"📅 المدة: *{plan['days']} يوم*\n"
            f"💳 رصيدك الحالي: *{fmt(balance)} د.ع*\n"
            "━━━━━━━━━━━━━━━━━\n"
            "رصيدك كافٍ! اضغط *شراء الآن* لتفعيل باقتك فوراً:"
        )
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id,
                                  message_id=call.message.message_id,
                                  parse_mode="Markdown",
                                  reply_markup=get_buy_now_keyboard(plan_key))
        except Exception:
            bot.send_message(call.message.chat.id, text, parse_mode="Markdown",
                             reply_markup=get_buy_now_keyboard(plan_key))
    else:
        # ── Case B: insufficient balance — deposit-for-plan flow ──────────────
        remaining = cost - balance
        user_states[user_id] = STATE_WAITING_PLAN_PHOTO
        user_data[user_id] = {
            "plan_key":      plan_key,
            "plan_label":    plan["label"],
            "plan_amount":   plan["amount"],
            "plan_raw":      cost,
            "plan_days":     plan["days"],
            "deposit_amount": remaining,
            "is_plan_deposit": True,
        }
        text = (
            f"📦 *{plan['label']}*\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"💰 تكلفة الباقة: *{plan['amount']} د.ع*\n"
            f"💳 رصيدك الحالي: *{fmt(balance)} د.ع*\n"
            f"💸 المبلغ الناقص: *{fmt(remaining)} د.ع*\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"لتفعيل هذه الباقة، أرسل المبلغ الناقص *{fmt(remaining)} د.ع* إلى:\n\n"
            f"📱 زين كاش *(اضغط للنسخ)*: `{ZAIN_CASH_NUMBER}`\n\n"
            "بعد إتمام التحويل، أرسل صورة الوصل هنا وسيتم تفعيل باقتك تلقائياً فور موافقة المشرف. ⚡"
        )
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id,
                                  message_id=call.message.message_id,
                                  parse_mode="Markdown", reply_markup=get_back_keyboard())
        except Exception:
            bot.send_message(call.message.chat.id, text, parse_mode="Markdown",
                             reply_markup=get_back_keyboard())


@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_now_"))
def handle_buy_now(call):
    bot.answer_callback_query(call.id)
    user_id   = call.from_user.id
    plan_key  = call.data[len("buy_now_"):]
    if plan_key not in PLANS:
        bot.send_message(call.message.chat.id, "❌ باقة غير صحيحة.", reply_markup=get_main_menu())
        return

    plan = PLANS[plan_key]
    cost = plan["raw"]
    full_name = call.from_user.full_name or ""
    username  = call.from_user.username  or ""

    with get_conn() as conn:
        ensure_user(conn, user_id, full_name, username)
        user = get_user(conn, user_id)

        # Race-condition guard
        if user is None or user["balance"] < cost:
            bot.send_message(
                call.message.chat.id,
                f"❌ *الرصيد غير كافٍ*\n\n"
                f"💳 رصيدك الحالي: *{fmt(user['balance'] if user else 0)} د.ع*\n"
                f"💰 تكلفة الباقة: *{fmt(cost)} د.ع*\n\n"
                "يرجى الإيداع أولاً ثم المحاولة مجدداً.",
                parse_mode="Markdown", reply_markup=get_main_menu(),
            )
            return

        # Deduct balance, create subscription, log — all in one transaction
        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id=?",
            (cost, user_id),
        )
        add_transaction(
            conn, user_id, "plan_payment", cost,
            description=f"شراء باقة|{plan_key}|{plan['label']}",
            status="approved",
        )
        add_subscription(conn, user_id, plan_key, cost, plan["days"])
        user = get_user(conn, user_id)

    expiry          = expiry_from_now(plan["days"])
    expected_profit = fmt(int(cost * PROFIT_RATE))
    text = (
        "🎉 *تم تفعيل اشتراكك بنجاح!*\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"📦 الباقة: *{plan['label']}*\n"
        f"💰 المبلغ المستثمر: *{fmt(cost)} د.ع*\n"
        f"💹 الربح المتوقع الإجمالي: *{expected_profit} د.ع*\n"
        f"📈 نسبة الأرباح: *50%*\n"
        f"📅 مدة الاشتراك: *{plan['days']} يوم*\n"
        f"🗓 تاريخ الانتهاء: *{expiry}*\n"
        f"💳 رصيدك المتبقي: *{fmt(user['balance'])} د.ع*\n"
        "━━━━━━━━━━━━━━━━━\n"
        "✅ تم تحديث حسابك بنجاح، يمكنك الآن السحب.\n"
        "ستُضاف أرباحك تلقائياً كل يوم الساعة 12 ظهراً. 💎"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode="Markdown")
    except Exception:
        pass
    bot.send_message(call.message.chat.id, "اختر من القائمة أدناه:", reply_markup=get_main_menu())


# ── 📥 Deposit ─────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data == "menu_deposit")
def handle_direct_deposit_menu(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    user_states[user_id] = STATE_WAITING_DEPOSIT_AMOUNT
    user_data[user_id] = {}
    text = (
        "📥 *إيداع مباشر*\n\n"
        "كم تريد أن تودع؟\n"
        "يرجى إدخال المبلغ بالدينار العراقي:"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode="Markdown", reply_markup=get_back_keyboard())
    except Exception:
        bot.send_message(call.message.chat.id, text, parse_mode="Markdown",
                         reply_markup=get_back_keyboard())


# ── 📤 Withdraw ────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data == "menu_withdraw")
def handle_withdraw_menu(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    user_states[user_id] = STATE_IDLE
    user_data[user_id] = {}

    # ── Direct, fresh DB query — no cached state used anywhere ────────────────
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        ensure_user(conn, user_id)
        conn.commit()

        user = conn.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ).fetchone()

        today = datetime.now(BAGHDAD_TZ).strftime("%Y-%m-%d")

        # Primary: active & not expired
        sub = conn.execute(
            """SELECT * FROM subscriptions
               WHERE user_id=? AND is_active=1 AND expiry_date >= ?
               ORDER BY id DESC LIMIT 1""",
            (user_id, today),
        ).fetchone()

        # Fallback: any active subscription regardless of expiry
        if sub is None:
            sub = conn.execute(
                """SELECT * FROM subscriptions
                   WHERE user_id=? AND is_active=1
                   ORDER BY id DESC LIMIT 1""",
                (user_id,),
            ).fetchone()

        # Full diagnostic snapshot
        all_subs = conn.execute(
            "SELECT id, plan_name, is_active, expiry_date FROM subscriptions WHERE user_id=?",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    balance = user["balance"] if user else 0

    # ── Console diagnostic log ────────────────────────────────────────────────
    print(f"[WITHDRAW_CHECK] user_id={user_id} | today={today} | balance={balance}")
    print(f"[WITHDRAW_CHECK] total_subs={len(all_subs)} | active_sub={'✅ '+sub['plan_name'] if sub else '❌ NONE'}")
    for s in all_subs:
        print(f"[WITHDRAW_CHECK]   sub_id={s['id']} | plan={s['plan_name']} "
              f"| is_active={s['is_active']} | expiry={s['expiry_date']}")

    # ── Guard 1: no active plan (access denied) ───────────────────────────────
    if not sub:
        print(f"[WITHDRAW_CHECK] → BLOCKED: no active subscription found for user {user_id}")
        text = (
            "⚠️ *عذراً، لا يمكنك السحب الآن*\n\n"
            "نظام السحب متاح فقط للمشتركين في باقاتنا الرقمية.\n"
            "يرجى الاشتراك في إحدى الباقات لتبدأ بجني الأرباح وسحبها فوراً."
        )
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id,
                                  message_id=call.message.message_id,
                                  parse_mode="Markdown",
                                  reply_markup=get_no_plan_keyboard())
        except Exception:
            bot.send_message(call.message.chat.id, text, parse_mode="Markdown",
                             reply_markup=get_no_plan_keyboard())
        return

    # ── Guard 2: has active plan but zero balance (access granted, no funds) ──
    if balance <= 0:
        print(f"[WITHDRAW_CHECK] → PLAN OK but balance=0 for user {user_id}")
        text = (
            "⚠️ *رصيدك الحالي صفر*\n\n"
            f"باقتك النشطة: *{sub['plan_name']}* ✅\n\n"
            "لا يوجد رصيد متاح للسحب حالياً.\n"
            "انتظر إضافة الأرباح اليومية أو قم بالإيداع أولاً."
        )
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id,
                                  message_id=call.message.message_id,
                                  parse_mode="Markdown",
                                  reply_markup=get_back_keyboard())
        except Exception:
            bot.send_message(call.message.chat.id, text, parse_mode="Markdown",
                             reply_markup=get_back_keyboard())
        return

    # ── Active plan + positive balance → proceed to withdrawal ────────────────
    print(f"[WITHDRAW_CHECK] → ALLOWED: plan={sub['plan_name']} balance={balance}")
    text = (
        "📤 *قسم سحب الأرباح*\n\n"
        f"📦 باقتك النشطة: *{sub['plan_name']}*\n"
        f"💰 رصيدك المتاح: *{fmt(balance)} د.ع*\n\n"
        "اختر طريقة الاستلام:"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode="Markdown",
                              reply_markup=get_withdraw_method_keyboard())
    except Exception:
        bot.send_message(call.message.chat.id, text, parse_mode="Markdown",
                         reply_markup=get_withdraw_method_keyboard())


@bot.callback_query_handler(func=lambda call: call.data in WITHDRAW_METHODS)
def handle_withdraw_method(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    method = WITHDRAW_METHODS[call.data]
    user_states[user_id] = STATE_WAITING_WITHDRAW_AMOUNT
    user_data[user_id] = {
        "method_key": call.data,
        "method_label": method["label"],
        "method_short": method["short"],
    }
    text = (
        f"📤 *سحب عبر {method['label']}*\n\n"
        "1️⃣ أدخل المبلغ الذي تريد سحبه بالدينار العراقي:"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode="Markdown", reply_markup=get_back_keyboard())
    except Exception:
        bot.send_message(call.message.chat.id, text, parse_mode="Markdown",
                         reply_markup=get_back_keyboard())


# ── Text: amounts ──────────────────────────────────────────────────────────────

@bot.message_handler(
    func=lambda m: user_states.get(m.from_user.id) == STATE_WAITING_DEPOSIT_AMOUNT
    and m.text is not None
)
def handle_deposit_amount(message):
    user_id = message.from_user.id
    amount = message.text.strip()
    user_data[user_id]["amount"] = amount
    user_states[user_id] = STATE_WAITING_DEPOSIT_PHOTO
    bot.send_message(
        message.chat.id,
        f"💳 يرجى إرسال *{amount}* دينار إلى رقم زين كاش *(اضغط للنسخ)*:\n\n"
        f"📱 `{ZAIN_CASH_NUMBER}`\n\n"
        f"بعد التحويل، أرسل صورة الوصل هنا.",
        parse_mode="Markdown", reply_markup=get_back_keyboard(),
    )


@bot.message_handler(
    func=lambda m: user_states.get(m.from_user.id) == STATE_WAITING_WITHDRAW_AMOUNT
    and m.text is not None
)
def handle_withdraw_amount(message):
    user_id = message.from_user.id
    amount_str = message.text.strip()
    try:
        requested = int("".join(filter(str.isdigit, amount_str)))
    except Exception:
        requested = 0

    # Balance validation
    with get_conn() as conn:
        ensure_user(conn, user_id)
        user = get_user(conn, user_id)

    if requested <= 0:
        bot.send_message(message.chat.id, "يرجى إدخال مبلغ صحيح للسحب.")
        return

    if requested > user["balance"]:
        bot.send_message(
            message.chat.id,
            f"❌ *الرصيد غير كافٍ*\n\n"
            f"💳 رصيدك الحالي: *{fmt(user['balance'])} د.ع*\n"
            f"💸 المبلغ المطلوب: *{fmt(requested)} د.ع*\n\n"
            f"يرجى إدخال مبلغ أقل من أو يساوي رصيدك.",
            parse_mode="Markdown", reply_markup=get_back_keyboard(),
        )
        return

    user_data[user_id]["amount"] = amount_str
    user_data[user_id]["amount_raw"] = requested
    user_states[user_id] = STATE_WAITING_WITHDRAW_PHONE
    method_label = user_data[user_id].get("method_label", "")
    bot.send_message(
        message.chat.id,
        f"✅ المبلغ: *{fmt(requested)} د.ع*\n\n"
        f"2️⃣ أدخل رقم *{method_label}* الذي تريد استلام المبلغ عليه:",
        parse_mode="Markdown", reply_markup=get_back_keyboard(),
    )


@bot.message_handler(
    func=lambda m: user_states.get(m.from_user.id) == STATE_WAITING_WITHDRAW_PHONE
    and m.text is not None
)
def handle_withdraw_phone(message):
    user_id = message.from_user.id
    phone = message.text.strip()
    user_data[user_id]["phone"] = phone
    user_states[user_id] = STATE_IDLE
    local = user_data[user_id]
    method_label = local.get("method_label", "")
    amount_raw = local.get("amount_raw", 0)
    bot.send_message(
        message.chat.id,
        f"📋 *ملخص طلب السحب*\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💰 المبلغ: *{fmt(amount_raw)} د.ع*\n"
        f"🛠 الطريقة: *{method_label}*\n"
        f"📞 الرقم: `{phone}`\n"
        f"━━━━━━━━━━━━━━━━━\n\n"
        f"هل تريد تأكيد طلب السحب؟",
        parse_mode="Markdown", reply_markup=get_withdraw_confirm_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "withdraw_confirm")
def handle_withdraw_confirm(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    local = user_data.get(user_id, {})
    username = call.from_user.username or "غير محدد"
    full_name = call.from_user.full_name or "غير محدد"

    amount_str = local.get("amount", "0")
    amount_raw = local.get("amount_raw", 0)
    method_short = local.get("method_short", "غير محدد")
    method_label = local.get("method_label", "غير محدد")
    phone = local.get("phone", "غير محدد")

    if not amount_raw or not phone or phone == "غير محدد":
        bot.send_message(call.message.chat.id,
                         "❌ انتهت صلاحية الطلب. يرجى البدء من جديد.",
                         reply_markup=get_main_menu())
        return

    with get_conn() as conn:
        ensure_user(conn, user_id, full_name, username)
        user = get_user(conn, user_id)
        txn_id = add_transaction(
            conn, user_id, "withdrawal", amount_raw,
            description=f"سحب عبر {method_short} — {fmt(amount_raw)} د.ع — {phone}",
        )

    admin_text = (
        "⚠️ *طلب سحب جديد*\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"👤 المستخدم: {full_name}\n"
        f"🔖 المعرف: @{username}\n"
        f"🆔 رقم المستخدم: `{user_id}`\n"
        f"💰 المبلغ: *{fmt(amount_raw)} د.ع*\n"
        f"🛠 الطريقة: *{method_label}*\n"
        f"📞 الرقم المستلم: `{phone}`\n"
        f"💳 الرصيد الحالي: {fmt(user['balance'])} د.ع\n"
        f"🆔 رقم العملية: #{txn_id}"
    )
    try:
        bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown",
                         reply_markup=get_admin_keyboard(user_id, txn_id))
    except Exception as e:
        print(f"فشل إرسال إشعار السحب للمشرف: {e}")

    user_data[user_id] = {}
    try:
        bot.edit_message_text(
            "✅ *تم إرسال طلب السحب بنجاح!*\n\n"
            "سيتم مراجعته وإرسال المبلغ إليك قريباً. 💎",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
        )
    except Exception:
        pass
    bot.send_message(call.message.chat.id,
                     "للعودة إلى القائمة الرئيسية اضغط على الأزرار أدناه.",
                     reply_markup=get_main_menu())


# ── Photos: receipts ───────────────────────────────────────────────────────────

@bot.message_handler(
    content_types=["photo"],
    func=lambda m: user_states.get(m.from_user.id) in [
        STATE_WAITING_DEPOSIT_PHOTO,
        STATE_WAITING_PLAN_PHOTO,
    ],
)
def handle_receipt_photo(message):
    user_id = message.from_user.id
    state = user_states.get(user_id, STATE_IDLE)
    local = user_data.get(user_id, {})
    username = message.from_user.username or "غير محدد"
    full_name = message.from_user.full_name or "غير محدد"

    with get_conn() as conn:
        ensure_user(conn, user_id, full_name, username)
        user = get_user(conn, user_id)

        if state == STATE_WAITING_DEPOSIT_PHOTO:
            amount_str = local.get("amount", "0")
            try:
                raw = int("".join(filter(str.isdigit, amount_str)))
            except Exception:
                raw = 0
            txn_id = add_transaction(conn, user_id, "deposit", raw,
                                     description=f"إيداع مباشر — {amount_str} د.ع")
            caption = (
                "📥 *طلب إيداع جديد*\n"
                "━━━━━━━━━━━━━━━━━\n"
                f"👤 المستخدم: {full_name}\n"
                f"🔖 المعرف: @{username}\n"
                f"🆔 رقم المستخدم: `{user_id}`\n"
                f"💰 المبلغ: *{amount_str}* د.ع\n"
                f"💳 الرصيد الحالي: {fmt(user['balance'])} د.ع\n"
                f"🆔 رقم العملية: #{txn_id}"
            )
            confirm_msg = "✅ تم إرسال وصل الإيداع!\nسيتم مراجعته والرد عليك قريباً."

        elif state == STATE_WAITING_PLAN_PHOTO:
            plan_key        = local.get("plan_key", "")
            plan_label      = local.get("plan_label", "غير محدد")
            plan_amount     = local.get("plan_amount", "0")
            plan_raw        = local.get("plan_raw", 0)
            plan_days       = local.get("plan_days", 30)
            is_plan_deposit = local.get("is_plan_deposit", False)
            deposit_amount  = local.get("deposit_amount", plan_raw)
            expected_profit = fmt(int(plan_raw * PROFIT_RATE))

            if is_plan_deposit:
                # ── Case B: deposit to cover the missing amount, auto-activates on approval ──
                txn_id = add_transaction(
                    conn, user_id, "deposit", deposit_amount,
                    description=f"إيداع لباقة|{plan_key}|{plan_label}",
                )
                caption = (
                    "📥 *إيداع لتفعيل باقة — يتطلب موافقة*\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"👤 المستخدم: {full_name}\n"
                    f"🔖 المعرف: @{username}\n"
                    f"🆔 رقم المستخدم: `{user_id}`\n"
                    f"📦 الباقة المطلوبة: *{plan_label}*\n"
                    f"💰 تكلفة الباقة: *{plan_amount}* د.ع\n"
                    f"💸 المبلغ المُودَع: *{fmt(deposit_amount)}* د.ع\n"
                    f"💹 الربح المتوقع: *{expected_profit}* د.ع\n"
                    f"📅 المدة: *{plan_days} يوم*\n"
                    f"💳 رصيد المستخدم الحالي: {fmt(user['balance'])} د.ع\n"
                    f"🆔 رقم العملية: #{txn_id}\n\n"
                    "⚡ الموافقة ستُفعّل الباقة تلقائياً."
                )
                confirm_msg = (
                    "✅ *تم إرسال وصل الإيداع بنجاح!*\n\n"
                    "سيتم مراجعته وتفعيل باقتك تلقائياً فور موافقة المشرف. ⚡"
                )
            else:
                # ── Legacy: direct plan payment (user had no balance — shouldn't hit now) ──
                txn_id = add_transaction(
                    conn, user_id, "plan_payment", plan_raw,
                    description=f"{plan_label}|{plan_key}|{plan_amount} د.ع",
                )
                caption = (
                    "📊 *طلب اشتراك باقة*\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"👤 المستخدم: {full_name}\n"
                    f"🔖 المعرف: @{username}\n"
                    f"🆔 رقم المستخدم: `{user_id}`\n"
                    f"📦 الباقة: *{plan_label}*\n"
                    f"💰 المبلغ: *{plan_amount}* د.ع\n"
                    f"💹 الربح المتوقع: *{expected_profit}* د.ع\n"
                    f"📅 المدة: *{plan_days} يوم*\n"
                    f"💳 رصيد المستخدم: {fmt(user['balance'])} د.ع\n"
                    f"🆔 رقم العملية: #{txn_id}"
                )
                confirm_msg = "✅ تم إرسال وصل الاشتراك!\nسيتم مراجعته وتفعيل باقتك قريباً."
            user_data[user_id]["txn_id"] = txn_id

    photo_file_id = message.photo[-1].file_id
    try:
        bot.send_photo(ADMIN_ID, photo_file_id, caption=caption,
                       parse_mode="Markdown",
                       reply_markup=get_admin_keyboard(user_id, txn_id))
    except Exception as e:
        print(f"فشل إرسال الإشعار للمشرف: {e}")

    user_states[user_id] = STATE_IDLE
    bot.send_message(message.chat.id, confirm_msg, reply_markup=get_main_menu())


# ── Admin: helpers ─────────────────────────────────────────────────────────────

def _update_admin_msg(call, note: str):
    """Append a decision note to the admin's original message (photo caption or text)."""
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=None)
    except Exception:
        pass
    # Photo messages have caption; text messages have text
    if call.message.content_type == "photo":
        try:
            bot.edit_message_caption(
                caption=(call.message.caption or "") + f"\n\n{note}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"فشل تحديث caption المشرف: {e}")
    else:
        try:
            bot.edit_message_text(
                text=(call.message.text or "") + f"\n\n{note}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"فشل تحديث text المشرف: {e}")


# ── Admin: approve / reject ────────────────────────────────────────────────────

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("approve_") or call.data.startswith("reject_")
)
def handle_admin_decision(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ غير مصرح لك.")
        return

    parts = call.data.split("_", 2)
    action = parts[0]
    target_user_id = int(parts[1])
    txn_id = int(parts[2])

    with get_conn() as conn:
        txn = get_transaction(conn, txn_id)
        if not txn:
            bot.answer_callback_query(call.id, "⚠️ العملية غير موجودة.")
            return
        if txn["status"] != "pending":
            bot.answer_callback_query(call.id, "⚠️ تم معالجة هذه العملية مسبقاً.")
            return

        ensure_user(conn, target_user_id)
        txn_type = txn["type"]
        amount = txn["amount"]

        if action == "approve":
            update_transaction_status(conn, txn_id, "approved")

            if txn_type == "deposit":
                conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?",
                             (amount, target_user_id))

                # ── Check if this deposit is linked to a plan (Case B auto-activation) ──
                linked_plan_key = None
                desc_parts = txn["description"].split("|")
                if len(desc_parts) >= 2 and desc_parts[1] in PLANS:
                    linked_plan_key = desc_parts[1]

                if linked_plan_key:
                    plan      = PLANS[linked_plan_key]
                    plan_cost = plan["raw"]
                    user      = get_user(conn, target_user_id)

                    if user["balance"] >= plan_cost:
                        # ── Auto-activate: deduct cost + create subscription ──
                        conn.execute(
                            "UPDATE users SET balance = balance - ? WHERE user_id=?",
                            (plan_cost, target_user_id),
                        )
                        add_transaction(
                            conn, target_user_id, "plan_payment", plan_cost,
                            description=f"تفعيل تلقائي|{linked_plan_key}|{plan['label']}",
                            status="approved",
                        )
                        add_subscription(conn, target_user_id, linked_plan_key,
                                         plan_cost, plan["days"])
                        user   = get_user(conn, target_user_id)
                        expiry = expiry_from_now(plan["days"])
                        user_msg = (
                            "🎉 *تمت الموافقة على إيداعك وتفعيل باقتك تلقائياً!*\n"
                            "━━━━━━━━━━━━━━━━━\n"
                            f"💰 المبلغ المُضاف: *{fmt(amount)} د.ع*\n"
                            f"📦 الباقة المُفعَّلة: *{plan['label']}*\n"
                            f"💹 الربح المتوقع الإجمالي: *{fmt(int(plan_cost * PROFIT_RATE))} د.ع*\n"
                            f"📅 مدة الاشتراك: *{plan['days']} يوم*\n"
                            f"🗓 تاريخ الانتهاء: *{expiry}*\n"
                            f"💳 رصيدك المتبقي: *{fmt(user['balance'])} د.ع*\n"
                            "━━━━━━━━━━━━━━━━━\n"
                            "✅ تم تحديث حسابك بنجاح، يمكنك الآن السحب أو تفعيل الباقات.\n"
                            "ستُضاف أرباحك تلقائياً كل يوم الساعة 12 ظهراً. 💎"
                        )
                        admin_note = f"✅ قُبل — أُضيف {fmt(amount)} د.ع وفُعِّلت {plan['label']} تلقائياً"
                    else:
                        # Balance still not enough (partial deposit edge case)
                        user_msg = (
                            "🎉 *تمت الموافقة على إيداعك!*\n"
                            "━━━━━━━━━━━━━━━━━\n"
                            f"💰 المبلغ المُضاف: *{fmt(amount)} د.ع*\n"
                            f"💳 رصيدك الحالي: *{fmt(user['balance'])} د.ع*\n"
                            "━━━━━━━━━━━━━━━━━\n"
                            f"⚠️ رصيدك لا يزال غير كافٍ لتفعيل *{plan['label']}* "
                            f"(*{fmt(plan_cost)} د.ع*).\n"
                            "يرجى إيداع المبلغ المتبقي."
                        )
                        admin_note = f"✅ قُبل — أُضيف {fmt(amount)} د.ع (رصيد لا يزال غير كافٍ للباقة)"
                else:
                    # ── Regular wallet deposit ────────────────────────────────────
                    user = get_user(conn, target_user_id)
                    user_msg = (
                        "🎉 *تمت الموافقة على إيداعك!*\n"
                        "━━━━━━━━━━━━━━━━━\n"
                        f"💰 المبلغ المُضاف: *{fmt(amount)} د.ع*\n"
                        f"💳 رصيدك الحالي: *{fmt(user['balance'])} د.ع*\n"
                        "━━━━━━━━━━━━━━━━━\n"
                        "✅ تم تحديث حسابك بنجاح، يمكنك الآن السحب أو تفعيل الباقات. 💎"
                    )
                    admin_note = f"✅ قُبل — أُضيف {fmt(amount)} د.ع"

            elif txn_type == "plan_payment":
                # ── Resolve plan_key (primary: embedded in description, fallbacks) ──
                plan_key = None
                desc_parts = txn["description"].split("|")
                if len(desc_parts) >= 2 and desc_parts[1] in PLANS:
                    plan_key = desc_parts[1]          # e.g. "plan_bronze"
                if not plan_key:
                    for key, p in PLANS.items():
                        if p["label"] in txn["description"]:
                            plan_key = key
                            break
                if not plan_key:
                    for key, p in PLANS.items():
                        if p["raw"] == int(amount):
                            plan_key = key
                            break

                plan_days  = PLANS[plan_key]["days"]  if plan_key else 30
                plan_label = PLANS[plan_key]["label"] if plan_key else "الباقة"
                expected_profit = int(amount * PROFIT_RATE)
                expiry = expiry_from_now(plan_days)

                # ── Guard: prevent negative balance ──────────────────────────────
                user = get_user(conn, target_user_id)
                if user["balance"] < amount:
                    update_transaction_status(conn, txn_id, "rejected")
                    user_msg = (
                        "❌ *تعذّر تفعيل الاشتراك — رصيد غير كافٍ*\n\n"
                        f"💳 رصيدك الحالي: *{fmt(user['balance'])} د.ع*\n"
                        f"💰 تكلفة الباقة: *{fmt(amount)} د.ع*\n\n"
                        "يرجى إيداع المبلغ الناقص أولاً ثم إعادة طلب الاشتراك."
                    )
                    admin_note = f"❌ رُفض تلقائياً — رصيد غير كافٍ ({fmt(user['balance'])} من {fmt(amount)} د.ع)"
                    try:
                        bot.send_message(target_user_id, user_msg,
                                         parse_mode="Markdown", reply_markup=get_main_menu())
                    except Exception as e:
                        print(f"فشل إرسال الرد للمستخدم: {e}")
                    bot.answer_callback_query(call.id, admin_note)
                    _update_admin_msg(call, admin_note)
                    return

                # ── Deduct balance and activate plan (single atomic block) ────────
                conn.execute(
                    "UPDATE users SET balance = balance - ? WHERE user_id=?",
                    (amount, target_user_id),
                )
                add_transaction(
                    conn, target_user_id, "plan_payment", amount,
                    description=f"خصم تكلفة الباقة — {plan_label}",
                    status="approved",
                )
                if plan_key:
                    add_subscription(conn, target_user_id, plan_key, amount, plan_days)

                user = get_user(conn, target_user_id)
                user_msg = (
                    "🎉 *تم تفعيل اشتراكك بنجاح!*\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"📦 الباقة: *{plan_label}*\n"
                    f"💰 المبلغ المستثمر: *{fmt(amount)} د.ع*\n"
                    f"💹 الربح المتوقع الإجمالي: *{fmt(expected_profit)} د.ع*\n"
                    f"📈 نسبة الأرباح: *50%*\n"
                    f"📅 مدة الاشتراك: *{plan_days} يوم*\n"
                    f"🗓 تاريخ الانتهاء: *{expiry}*\n"
                    f"💳 رصيدك المتبقي: *{fmt(user['balance'])} د.ع*\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    "ستُضاف أرباحك تلقائياً كل يوم الساعة 12 ظهراً. 💎"
                )
                admin_note = f"✅ قُبل — باقة {plan_label} مُفعّلة"

            else:  # withdrawal
                conn.execute("UPDATE users SET balance = balance - ? WHERE user_id=?",
                             (amount, target_user_id))
                user = get_user(conn, target_user_id)
                user_msg = (
                    "✅ *تمت الموافقة على طلب السحب!*\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"💸 المبلغ المسحوب: *{fmt(amount)} د.ع*\n"
                    f"💳 رصيدك المتبقي: *{fmt(user['balance'])} د.ع*\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    "سيصلك المبلغ قريباً. 💎"
                )
                admin_note = f"✅ قُبل — سُحب {fmt(amount)} د.ع"

        else:  # reject
            update_transaction_status(conn, txn_id, "rejected")
            type_label = TYPE_LABELS.get(txn_type, "العملية")
            user_msg = (
                f"❌ *تم رفض طلبك ({type_label})*\n\n"
                f"المبلغ: *{fmt(amount)} د.ع*\n\n"
                "يرجى التأكد من صحة الوصل وإعادة المحاولة."
            )
            admin_note = f"❌ مرفوض — {fmt(amount)} د.ع"

    try:
        bot.send_message(target_user_id, user_msg, parse_mode="Markdown",
                         reply_markup=get_main_menu())
    except Exception as e:
        print(f"فشل إرسال الرد للمستخدم: {e}")

    bot.answer_callback_query(call.id, admin_note)
    _update_admin_msg(call, admin_note)


# ── Fallback ───────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: True)
def handle_unknown(message):
    user_id = message.from_user.id
    state = user_states.get(user_id, STATE_IDLE)
    if state == STATE_WAITING_DEPOSIT_AMOUNT:
        bot.send_message(message.chat.id, "يرجى إدخال مبلغ صحيح للإيداع.")
    elif state == STATE_WAITING_WITHDRAW_AMOUNT:
        bot.send_message(message.chat.id, "يرجى إدخال مبلغ صحيح للسحب.")
    elif state == STATE_WAITING_WITHDRAW_PHONE:
        bot.send_message(message.chat.id, "يرجى إدخال رقم الهاتف أو المحفظة لاستلام المبلغ.")
    elif state in [STATE_WAITING_DEPOSIT_PHOTO, STATE_WAITING_PLAN_PHOTO]:
        bot.send_message(message.chat.id, "📸 يرجى إرسال *صورة* الوصل للمتابعة.",
                         parse_mode="Markdown")
    else:
        bot.send_message(message.chat.id, WELCOME_TEXT, reply_markup=get_main_menu())


# ── Daily profit scheduler ─────────────────────────────────────────────────────

def daily_profit_task():
    """Runs every day at 12:00 PM Baghdad time.
    Distributes the daily share of 50% profit to every user with an active plan.
    Uses last_daily_payment to prevent double payment on bot restarts."""
    today = datetime.now(BAGHDAD_TZ).strftime("%Y-%m-%d")
    print(f"[Scheduler] Running daily profit task for {today}")
    count = 0

    with get_conn() as conn:
        subs = conn.execute(
            """SELECT s.id, s.user_id, s.plan_name, s.amount, s.duration_days,
                      s.last_daily_payment, u.name, u.balance
               FROM subscriptions s
               JOIN users u ON s.user_id = u.user_id
               WHERE s.is_active = 1
                 AND s.expiry_date >= ?
                 AND (s.last_daily_payment IS NULL OR s.last_daily_payment != ?)""",
            (today, today),
        ).fetchall()

        for sub in subs:
            daily_amount = round((sub["amount"] * PROFIT_RATE) / sub["duration_days"], 2)

            # Credit balance
            conn.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id=?",
                (daily_amount, sub["user_id"]),
            )
            # Mark payment date to prevent double-pay
            conn.execute(
                "UPDATE subscriptions SET last_daily_payment=? WHERE id=?",
                (today, sub["id"]),
            )
            # Log transaction
            add_transaction(
                conn,
                sub["user_id"],
                "plan_profit",
                daily_amount,
                description=f"ربح يومي — {sub['plan_name']}",
                status="approved",
            )

            # Get updated balance
            user = get_user(conn, sub["user_id"])
            new_balance = fmt(user["balance"])
            user_name = sub["name"] or "عزيزي المستخدم"

            # Push notification
            msg = (
                "💰 *إشعار أرباح اليوم*\n"
                "━━━━━━━━━━━━━━━━━\n"
                f"أهلاً بك *{user_name}*، تم إضافة أرباحك اليومية بنجاح!\n\n"
                f"💵 المبلغ المضاف: *+{fmt(daily_amount)} د.ع*\n"
                f"💳 رصيدك الإجمالي الآن: *{new_balance} د.ع*\n"
                "━━━━━━━━━━━━━━━━━\n"
                "تفقّد محفظتك الآن لمزيد من التفاصيل. 💎"
            )
            try:
                bot.send_message(sub["user_id"], msg,
                                 parse_mode="Markdown", reply_markup=get_main_menu())
                count += 1
            except Exception as e:
                print(f"[Scheduler] Failed to notify user {sub['user_id']}: {e}")

    print(f"[Scheduler] Daily profit sent to {count} users.")


def start_scheduler():
    scheduler = BackgroundScheduler(timezone=BAGHDAD_TZ)
    scheduler.add_job(
        daily_profit_task,
        trigger="cron",
        hour=12,
        minute=0,
        id="daily_profit",
        replace_existing=True,
    )
    scheduler.start()
    print("[Scheduler] Started — daily profit task fires at 12:00 PM Baghdad time.")
    return scheduler


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    start_scheduler()
    print(f"Bot started — token: {BOT_TOKEN[:20]}...")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
