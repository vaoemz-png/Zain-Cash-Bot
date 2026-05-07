import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytz
import telebot
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler

try:
    import requests
    from bs4 import BeautifulSoup
    SCRAPER_OK = True
except ImportError:
    SCRAPER_OK = False
    print("[Warning] requests/bs4 not installed — scraper disabled.")

# ── Constants ──────────────────────────────────────────────────────────────────

BAGHDAD_TZ       = pytz.timezone("Asia/Baghdad")
def _extract_token(raw: str) -> str:
    raw = raw.strip()
    if raw.count(":") == 1:
        return raw
    parts = re.split(r'(?=\d{8,12}:)', raw)
    for p in reversed(parts):
        p = p.strip()
        if re.match(r'^\d{8,12}:[A-Za-z0-9_-]{35,}$', p):
            return p
    return raw

BOT_TOKEN        = _extract_token(
    os.environ.get("TELEGRAM_BOT_TOKEN", "8630722565:AAGK-xjCMLvtrvLnzvVbvTGn8vWClxsQh6E")
)
ADMIN_ID         = 122498736
ZAIN_CASH_NUMBER = "07713356493"
DB_FILE          = "bot_database.db"

ELITE_COST       = 2000
ELITE_DAYS       = 15
PLAN_LOCK_DAYS   = 15          # all plans lock for 15 days
PROFIT_MULT      = 1.50        # 150 % total return over lock period
CLAIM_START_H    = 10          # 10:00 AM Baghdad
CLAIM_END_H      = 22          # 10:00 PM Baghdad
LINK_DELETE_SECS = 600         # auto-delete radar messages after 10 min

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en;q=0.9",
}

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# ── States ─────────────────────────────────────────────────────────────────────

user_states: dict = {}
user_data:   dict = {}

STATE_IDLE           = "idle"
STATE_DEP_AMOUNT     = "dep_amount"
STATE_DEP_PHOTO      = "dep_photo"
STATE_PLAN_PHOTO     = "plan_photo"
STATE_WD_AMOUNT      = "wd_amount"
STATE_WD_PHONE       = "wd_phone"

WITHDRAW_METHODS = {
    "withdraw_zaincash": {"label": "💳 زين كاش",       "short": "زين كاش"},
    "withdraw_asiacell": {"label": "📱 آسياسيل تحويل", "short": "آسياسيل"},
}

PLANS = {
    "plan_bronze": {"label": "🥉 الباقة البرونزية", "amount": "10,000",  "raw": 10_000,  "days": PLAN_LOCK_DAYS},
    "plan_silver": {"label": "🥈 الباقة الفضية",   "amount": "25,000",  "raw": 25_000,  "days": PLAN_LOCK_DAYS},
    "plan_gold":   {"label": "🥇 الباقة الذهبية",  "amount": "50,000",  "raw": 50_000,  "days": PLAN_LOCK_DAYS},
    "plan_elite":  {"label": "💎 باقة النخبة",      "amount": "100,000", "raw": 100_000, "days": PLAN_LOCK_DAYS},
}

TYPE_LABELS = {
    "deposit":       "📥 إيداع",
    "withdrawal":    "📤 سحب",
    "plan_payment":  "📊 اشتراك باقة",
    "plan_profit":   "💰 أرباح يومية",
    "plan_deposit":  "📥 إيداع لباقة",
    "elite_payment": "💎 نادي النخبة",
    "profit_unlock": "🔓 أرباح مفتوحة",
}

STATUS_LABELS = {
    "pending":  "⏳ قيد المراجعة",
    "approved": "✅ مقبول",
    "rejected": "❌ مرفوض",
}

WELCOME_TEXT = (
    "💎 *أهلاً بك في متجر دراهم الرقمي*\n"
    "━━━━━━━━━━━━━━━━━\n"
    "البوت الأول في العراق لتحويل النقاط إلى أرباح حقيقية 🇮🇶\n\n"
    "اختر من الأزرار أدناه:"
)

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
                user_id             INTEGER PRIMARY KEY,
                name                TEXT    DEFAULT '',
                username            TEXT    DEFAULT '',
                deposit_balance     REAL    DEFAULT 0,
                locked_profits      REAL    DEFAULT 0,
                active_plan_price   REAL    DEFAULT 0,
                profit_claimed_date TEXT    DEFAULT NULL,
                profit_lock_start   TEXT    DEFAULT NULL
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

            CREATE TABLE IF NOT EXISTS elite_subscriptions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                start_date  TEXT    NOT NULL,
                expiry_date TEXT    NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS price_cache (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                source     TEXT NOT NULL,
                title      TEXT NOT NULL,
                price_text TEXT NOT NULL,
                url        TEXT DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );
        """)

        # Migrations for existing databases
        for sql in [
            "ALTER TABLE users ADD COLUMN deposit_balance REAL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN locked_profits REAL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN active_plan_price REAL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN profit_claimed_date TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN profit_lock_start TEXT DEFAULT NULL",
            "ALTER TABLE subscriptions ADD COLUMN last_daily_payment TEXT DEFAULT NULL",
        ]:
            try:
                conn.execute(sql)
            except Exception:
                pass

        # Copy old 'balance' → 'deposit_balance' for existing users
        try:
            conn.execute(
                "UPDATE users SET deposit_balance = balance WHERE deposit_balance = 0 AND balance > 0"
            )
        except Exception:
            pass  # No 'balance' column on fresh install

    print("Database initialized.")


def ensure_user(conn, user_id, name="", username=""):
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, name, username) VALUES (?,?,?)",
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
    plan   = PLANS[plan_key]
    now    = datetime.now(BAGHDAD_TZ)
    expiry = now + timedelta(days=duration_days)
    conn.execute(
        """INSERT INTO subscriptions
           (user_id, plan_name, plan_key, amount, duration_days, start_date, expiry_date)
           VALUES (?,?,?,?,?,?,?)""",
        (user_id, plan["label"], plan_key, amount, duration_days,
         now.strftime("%Y-%m-%d"), expiry.strftime("%Y-%m-%d")),
    )


def get_active_subscription(conn, user_id):
    today = datetime.now(BAGHDAD_TZ).strftime("%Y-%m-%d")
    sub = conn.execute(
        """SELECT * FROM subscriptions
           WHERE user_id=? AND is_active=1 AND expiry_date >= ?
           ORDER BY id DESC LIMIT 1""",
        (user_id, today),
    ).fetchone()
    if sub is None:
        sub = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND is_active=1 ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return sub


def get_elite_sub(conn, user_id):
    today = datetime.now(BAGHDAD_TZ).strftime("%Y-%m-%d")
    return conn.execute(
        "SELECT * FROM elite_subscriptions WHERE user_id=? AND expiry_date >= ? ORDER BY id DESC LIMIT 1",
        (user_id, today),
    ).fetchone()


# ── Helpers ────────────────────────────────────────────────────────────────────

def fmt(n):
    return f"{int(n):,}"


def expiry_from_now(days):
    return (datetime.now(BAGHDAD_TZ) + timedelta(days=days)).strftime("%Y-%m-%d")


def days_until_unlock(profit_lock_start, plan_days=30):
        return 0
    try:
        lock_dt   = datetime.strptime(profit_lock_start, "%Y-%m-%d")
        unlock_dt = lock_dt + timedelta(days=plan_days)
        today     = datetime.now()
        return max((unlock_dt - today).days, 0)
    except Exception:
        return 0


def safe_delete_message(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


# ── Keyboards ──────────────────────────────────────────────────────────────────

def get_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💳 محفظتي",               callback_data="menu_wallet"),
        types.InlineKeyboardButton("📊 الباقات",               callback_data="menu_plans"),
        types.InlineKeyboardButton("📥 إيداع مباشر",          callback_data="menu_deposit"),
        types.InlineKeyboardButton("📤 سحب الأرباح",          callback_data="menu_withdraw"),
        types.InlineKeyboardButton("💰 استلام أرباح اليوم",   callback_data="menu_claim"),
        types.InlineKeyboardButton("💎 | نادي النخبة الرقمي", callback_data="menu_elite"),
    )
    return markup


def get_back_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="menu_back"))
    return markup


def get_wallet_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📜 سجل العمليات", callback_data="wallet_history"),
        types.InlineKeyboardButton("🔙 رجوع",         callback_data="menu_back"),
    )
    return markup


def get_plans_keyboard(deposit_balance):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for key, plan in PLANS.items():
        can = deposit_balance >= plan["raw"]
        lbl = (f"✅ {plan['label']} | {plan['amount']} د.ع"
               if can else f"{plan['label']} | {plan['amount']} د.ع")
        markup.add(types.InlineKeyboardButton(lbl, callback_data=key))
    markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
    return markup


def get_buy_now_keyboard(plan_key):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("✅ شراء الآن من رصيدي", callback_data=f"buy_now_{plan_key}"),
        types.InlineKeyboardButton("🔙 رجوع",               callback_data="menu_back"),
    )
    return markup


def get_withdraw_method_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    for key, m in WITHDRAW_METHODS.items():
        markup.add(types.InlineKeyboardButton(m["label"], callback_data=key))
    markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"))
    return markup


def get_withdraw_confirm_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("✅ تأكيد طلب السحب", callback_data="withdraw_confirm"),
        types.InlineKeyboardButton("❌ إلغاء",           callback_data="menu_back"),
    )
    return markup


def get_admin_keyboard(user_id, txn_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ قبول", callback_data=f"approve_{user_id}_{txn_id}"),
        types.InlineKeyboardButton("❌ رفض",  callback_data=f"reject_{user_id}_{txn_id}"),
    )
    return markup


def get_elite_radar_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🇮🇶 | رادار العروض العراقية",  callback_data="radar_iraq"),
        types.InlineKeyboardButton("🌍 | أرشيف المصادر العالمية",  callback_data="radar_world"),
        types.InlineKeyboardButton("🔙 رجوع",                      callback_data="menu_back"),
    )
    return markup


def send_main_menu(chat_id, message_id=None):
    if message_id:
        try:
            bot.edit_message_text(WELCOME_TEXT, chat_id=chat_id,
                                  message_id=message_id,
                                  parse_mode="Markdown",
                                  reply_markup=get_main_menu())
            return
        except Exception:
            pass
    bot.send_message(chat_id, WELCOME_TEXT, parse_mode="Markdown", reply_markup=get_main_menu())


def build_wallet_text(user, sub):
    dep  = fmt(user["deposit_balance"])
    lock = fmt(user["locked_profits"])
    inv  = fmt(user["active_plan_price"])
        # القاعدة: باقة الـ 10 آلاف تأخذ 15 يوم، وأي باقة أخرى تأخذ 30 يوم
    if sub and sub.get('plan_price') == 10000:
        p_days = 15
    else:
        p_days = 30
    
    ud = days_until_unlock(user["profit_lock_start"], plan_days=p_days)

    
    lock_note = f" *(تفتح بعد {ud} يوم)*" if ud > 0 else ""
    plan_name = sub["plan_name"] if sub else "لا توجد باقة نشطة"
    expiry    = sub["expiry_date"] if sub else "-"
    
    warning_msg = ""
    if user['deposit_balance'] == 0:
        warning_msg = f"⚠️ رصيدك القابل للسحب صفر\nلا يوجد رصيد متاح للسحب حالياً. استلم أرباحك يومياً وانتظر {p_days} يوم لتفتح وتتحول للرصيد.\n\n"

        return (
        f"{warning_msg}💳 *محفظتك الرقمية*\n"
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
        f"💵 رصيد قابل للسحب:  *{dep}* د.ع\n"
        f"🔒 أرباح مقفلة:       *{lock}* د.ع {lock_note}\n"
        f"📦 مبلغ الخطة النشطة: *{inv}* د.ع\n"
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
        f"📊 الباقة النشطة: *{plan_name}*\n"
        f"🗓️ تاريخ الانتهاء: *{expiry}*\n"
        f"🆔 معرف الحساب:  `{user['user_id']}`"
    )




def _update_admin_msg(call, note: str):
    try:
        bot.edit_message_reply_markup(call.message.chat.id,
                                      call.message.message_id, reply_markup=None)
    except Exception:
        pass
    if call.message.content_type == "photo":
        try:
            bot.edit_message_caption(
                caption=(call.message.caption or "") + f"\n\n{note}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"caption update failed: {e}")
    else:
        try:
            bot.edit_message_text(
                text=(call.message.text or "") + f"\n\n{note}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"text update failed: {e}")


# ── /start ─────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def handle_start(message):
    uid = message.from_user.id
    user_states[uid] = STATE_IDLE
    user_data[uid]   = {}
    with get_conn() as conn:
        ensure_user(conn, uid,
                    message.from_user.full_name or "",
                    message.from_user.username  or "")
    bot.send_message(message.chat.id, WELCOME_TEXT,
                     parse_mode="Markdown", reply_markup=get_main_menu())


# ── Back ───────────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "menu_back")
def handle_back(call):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    user_states[uid] = STATE_IDLE
    user_data[uid]   = {}
    send_main_menu(call.message.chat.id, call.message.message_id)


# ── 💳 Wallet ──────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "menu_wallet")
def handle_wallet(call):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    with get_conn() as conn:
        ensure_user(conn, uid,
                    call.from_user.full_name or "",
                    call.from_user.username  or "")
        user = get_user(conn, uid)
        sub  = get_active_subscription(conn, uid)
    text = build_wallet_text(user, sub)
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode="Markdown", reply_markup=get_wallet_keyboard())
    except Exception:
        bot.send_message(call.message.chat.id, text,
                         parse_mode="Markdown", reply_markup=get_wallet_keyboard())


@bot.callback_query_handler(func=lambda c: c.data == "wallet_history")
def handle_history(call):
    bot.answer_callback_query(call.id)
    with get_conn() as conn:
        txns = get_last_transactions(conn, call.from_user.id, 10)
    if not txns:
        text = "📜 *سجل العمليات*\n\nلا توجد عمليات مسجلة بعد."
    else:
        lines = ["📜 *آخر 10 عمليات*\n━━━━━━━━━━━━━━━━━"]
        for t in txns:
            lbl    = TYPE_LABELS.get(t["type"], t["type"])
            status = STATUS_LABELS.get(t["status"], t["status"])
            lines.append(f"{lbl}: *{fmt(t['amount'])} د.ع* — {status}\n🗓 {t['created_at'][:10]}")
        text = "\n\n".join(lines)
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode="Markdown", reply_markup=get_back_keyboard())
    except Exception:
        bot.send_message(call.message.chat.id, text,
                         parse_mode="Markdown", reply_markup=get_back_keyboard())


# ── 📊 Plans ───────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "menu_plans")
def handle_plans(call):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    user_states[uid] = STATE_IDLE
    with get_conn() as conn:
        ensure_user(conn, uid)
        user = get_user(conn, uid)
        sub  = get_active_subscription(conn, uid)
    active_note = (f"⚠️ لديك باقة نشطة: *{sub['plan_name']}*\n\n" if sub else "")
    text = (
        "📊 *الباقات المتاحة*\n\n"
        + active_note +
        f"💸 رصيدك القابل للسحب: *{fmt(user['deposit_balance'])} د.ع*\n\n"
        "• الربح الإجمالي: *150%* خلال 15 يوم\n"
        "• استلام الأرباح يومياً: *10 ص — 10 م*\n"
        "• بعد 15 يوم تُفتح الأرباح تلقائياً\n\n"
        "اختر الباقة المناسبة:"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode="Markdown",
                              reply_markup=get_plans_keyboard(user["deposit_balance"]))
    except Exception:
        bot.send_message(call.message.chat.id, text, parse_mode="Markdown",
                         reply_markup=get_plans_keyboard(user["deposit_balance"]))


@bot.callback_query_handler(func=lambda c: c.data in PLANS)
def handle_plan_selection(call):
    uid      = call.from_user.id
    plan_key = call.data
    plan     = PLANS[plan_key]
    bot.answer_callback_query(call.id)
    with get_conn() as conn:
        ensure_user(conn, uid,
                    call.from_user.full_name or "",
                    call.from_user.username  or "")
        user = get_user(conn, uid)
    balance      = user["deposit_balance"]
    cost         = plan["raw"]
    daily_profit = fmt(int((cost * PROFIT_MULT) / PLAN_LOCK_DAYS))
    total_profit = fmt(int(cost * PROFIT_MULT))

    if balance >= cost:
        user_states[uid] = STATE_IDLE
        user_data[uid]   = {"plan_key": plan_key, "plan_label": plan["label"],
                             "plan_raw": cost, "plan_days": plan["days"]}
        text = (
            f"📦 *{plan['label']}*\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"💰 تكلفة الاستثمار: *{plan['amount']} د.ع*\n"
            f"💹 الربح الإجمالي: *{total_profit} د.ع* (150%)\n"
            f"📈 الربح اليومي: *{daily_profit} د.ع*\n"
            f"📅 مدة القفل: *{plan['days']} يوم*\n"
            f"💸 رصيدك القابل للسحب: *{fmt(balance)} د.ع*\n"
            "━━━━━━━━━━━━━━━━━\n"
            "رصيدك كافٍ! اضغط *شراء الآن* لتفعيل باقتك:"
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
        remaining = cost - balance
        user_states[uid] = STATE_PLAN_PHOTO
        user_data[uid]   = {
            "plan_key": plan_key, "plan_label": plan["label"],
            "plan_amount": plan["amount"], "plan_raw": cost,
            "plan_days": plan["days"], "deposit_amount": remaining,
            "is_plan_deposit": True,
        }
        text = (
            f"📦 *{plan['label']}*\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"💰 تكلفة الاستثمار: *{plan['amount']} د.ع*\n"
            f"💸 رصيدك القابل للسحب: *{fmt(balance)} د.ع*\n"
            f"💔 المبلغ الناقص: *{fmt(remaining)} د.ع*\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"أرسل *{fmt(remaining)} د.ع* إلى زين كاش:\n\n"
            f"📱 `{ZAIN_CASH_NUMBER}`\n\n"
            "بعد التحويل أرسل صورة الوصل وسيتم التفعيل تلقائياً. ⚡"
        )
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id,
                                  message_id=call.message.message_id,
                                  parse_mode="Markdown", reply_markup=get_back_keyboard())
        except Exception:
            bot.send_message(call.message.chat.id, text, parse_mode="Markdown",
                             reply_markup=get_back_keyboard())


@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_now_"))
def handle_buy_now(call):
    bot.answer_callback_query(call.id)
    uid      = call.from_user.id
    plan_key = call.data[len("buy_now_"):]
    if plan_key not in PLANS:
        bot.send_message(call.message.chat.id, "❌ باقة غير صحيحة.", reply_markup=get_main_menu())
        return
    plan = PLANS[plan_key]
    cost = plan["raw"]
    with get_conn() as conn:
        ensure_user(conn, uid,
                    call.from_user.full_name or "",
                    call.from_user.username  or "")
        user = get_user(conn, uid)
        if user is None or user["deposit_balance"] < cost:
            bot.send_message(
                call.message.chat.id,
                f"❌ *الرصيد غير كافٍ*\n\n"
                f"💸 رصيدك القابل للسحب: *{fmt(user['deposit_balance'] if user else 0)} د.ع*\n"
                f"💰 تكلفة الباقة: *{fmt(cost)} د.ع*",
                parse_mode="Markdown", reply_markup=get_main_menu(),
            )
            return
        today = datetime.now(BAGHDAD_TZ).strftime("%Y-%m-%d")
        conn.execute(
            """UPDATE users
               SET deposit_balance   = deposit_balance - ?,
                   active_plan_price = active_plan_price + ?,
                   profit_lock_start = COALESCE(profit_lock_start, ?)
               WHERE user_id=?""",
            (cost, cost, today, uid),
        )
        add_transaction(conn, uid, "plan_payment", cost,
                        description=f"شراء باقة|{plan_key}|{plan['label']}",
                        status="approved")
        add_subscription(conn, uid, plan_key, cost, plan["days"])
        user = get_user(conn, uid)
    expiry      = expiry_from_now(plan["days"])
    daily_prof  = fmt(int((cost * PROFIT_MULT) / PLAN_LOCK_DAYS))
    total_prof  = fmt(int(cost * PROFIT_MULT))
    text = (
        "🎉 *تم تفعيل اشتراكك بنجاح!*\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"📦 الباقة: *{plan['label']}*\n"
        f"💰 المبلغ المستثمر: *{fmt(cost)} د.ع*\n"
        f"💹 الربح الإجمالي المتوقع: *{total_prof} د.ع*\n"
        f"📈 الربح اليومي: *{daily_prof} د.ع*\n"
        f"📅 مدة القفل: *{plan['days']} يوم*\n"
        f"🗓 تاريخ الانتهاء: *{expiry}*\n"
        "━━━━━━━━━━━━━━━━━\n"
        "💰 استلم أرباحك يومياً بين الساعة *10 ص — 10 م*\n"
        "🔓 تُفتح جميع الأرباح تلقائياً بعد 15 يوم 💎"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id, parse_mode="Markdown")
    except Exception:
        pass
    bot.send_message(call.message.chat.id, "اختر من القائمة أدناه:", reply_markup=get_main_menu())


# ── 💰 Daily Claim ─────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "menu_claim")
def handle_daily_claim(call):
    bot.answer_callback_query(call.id)
    uid      = call.from_user.id
    now_bagh = datetime.now(BAGHDAD_TZ)
    hour     = now_bagh.hour
    today    = now_bagh.strftime("%Y-%m-%d")

    if hour < CLAIM_START_H or hour >= CLAIM_END_H:
        text = (
            "⏰ *انتهى وقت المطالبة اليوم*\n\n"
            "وقت استلام الأرباح: *10 صباحاً — 10 مساءً*\n"
            "يرجى العودة غداً الساعة 10 صباحاً."
        )
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id,
                                  message_id=call.message.message_id,
                                  parse_mode="Markdown", reply_markup=get_back_keyboard())
        except Exception:
            bot.send_message(call.message.chat.id, text,
                             parse_mode="Markdown", reply_markup=get_back_keyboard())
        return

    with get_conn() as conn:
        ensure_user(conn, uid)
        user = get_user(conn, uid)

        if not user or user["active_plan_price"] <= 0:
            no_plan_kb = types.InlineKeyboardMarkup()
            no_plan_kb.add(
                types.InlineKeyboardButton("📊 الباقات",  callback_data="menu_plans"),
                types.InlineKeyboardButton("🔙 رجوع",    callback_data="menu_back"),
            )
            text = (
                "⚠️ *ليس لديك خطة استثمار نشطة*\n\n"
                "اشترك في إحدى الباقات لتبدأ بجني الأرباح."
            )
            try:
                bot.edit_message_text(text, chat_id=call.message.chat.id,
                                      message_id=call.message.message_id,
                                      parse_mode="Markdown", reply_markup=no_plan_kb)
            except Exception:
                bot.send_message(call.message.chat.id, text,
                                 parse_mode="Markdown", reply_markup=no_plan_kb)
            return

        if user["profit_claimed_date"] == today:
            text = (
                "✅ *لقد استلمت أرباحك اليوم بالفعل*\n\n"
                "📅 عُد غداً بين *10 ص — 10 م* لاستلام أرباح يوم جديد."
            )
            try:
                bot.edit_message_text(text, chat_id=call.message.chat.id,
                                      message_id=call.message.message_id,
                                      parse_mode="Markdown", reply_markup=get_back_keyboard())
            except Exception:
                bot.send_message(call.message.chat.id, text,
                                 parse_mode="Markdown", reply_markup=get_back_keyboard())
            return

        daily_amount = round((user["active_plan_price"] * PROFIT_MULT) / PLAN_LOCK_DAYS, 2)
        conn.execute(
            """UPDATE users
               SET locked_profits      = locked_profits + ?,
                   profit_claimed_date = ?,
                   profit_lock_start   = COALESCE(profit_lock_start, ?)
               WHERE user_id=?""",
            (daily_amount, today, today, uid),
        )
        add_transaction(conn, uid, "plan_profit", daily_amount,
                        description="مطالبة يومية بالأرباح", status="approved")
        user = get_user(conn, uid)

    unlock_remaining = days_until_unlock(user["profit_lock_start"])
    text = (
        "💰 *تم استلام أرباح اليوم بنجاح!*\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"💵 المبلغ المضاف اليوم: *+{fmt(daily_amount)} د.ع*\n"
        f"🔒 إجمالي الأرباح المقفلة: *{fmt(user['locked_profits'])} د.ع*\n"
        f"🔓 تُفتح بعد: *{unlock_remaining} يوم*\n"
        "━━━━━━━━━━━━━━━━━\n"
        "عُد غداً لاستلام أرباح يوم جديد! 💎"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode="Markdown", reply_markup=get_back_keyboard())
    except Exception:
        bot.send_message(call.message.chat.id, text,
                         parse_mode="Markdown", reply_markup=get_back_keyboard())


# ── 📥 Deposit ─────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "menu_deposit")
def handle_deposit_menu(call):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    user_states[uid] = STATE_DEP_AMOUNT
    user_data[uid]   = {}
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
        bot.send_message(call.message.chat.id, text,
                         parse_mode="Markdown", reply_markup=get_back_keyboard())


# ── 📤 Withdraw ────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "menu_withdraw")
def handle_withdraw_menu(call):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    user_states[uid] = STATE_IDLE
    user_data[uid]   = {}

    # Direct raw connection — guaranteed fresh read
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        ensure_user(conn, uid)
        conn.commit()
        user     = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        sub      = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND is_active=1 ORDER BY id DESC LIMIT 1",
            (uid,),
        ).fetchone()
        all_subs = conn.execute(
            "SELECT id, plan_name, is_active, expiry_date FROM subscriptions WHERE user_id=?",
            (uid,),
        ).fetchall()
    finally:
        conn.close()

    dep_balance = user["deposit_balance"] if user else 0
    print(f"[WITHDRAW] uid={uid} deposit_balance={dep_balance} sub={'YES:'+sub['plan_name'] if sub else 'NO'}")
    for s in all_subs:
        print(f"[WITHDRAW]   sub_id={s['id']} plan={s['plan_name']} active={s['is_active']} expiry={s['expiry_date']}")

    if not sub:
        no_plan_kb = types.InlineKeyboardMarkup(row_width=2)
        no_plan_kb.add(
            types.InlineKeyboardButton("📊 عرض الباقات", callback_data="menu_plans"),
            types.InlineKeyboardButton("🔙 رجوع",        callback_data="menu_back"),
        )
        text = (
            "⚠️ *عذراً، لا يمكنك السحب الآن*\n\n"
            "نظام السحب متاح فقط للمشتركين في باقاتنا الرقمية.\n"
            "اشترك في إحدى الباقات لتبدأ بجني الأرباح وسحبها."
        )
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id,
                                  message_id=call.message.message_id,
                                  parse_mode="Markdown", reply_markup=no_plan_kb)
        except Exception:
            bot.send_message(call.message.chat.id, text,
                             parse_mode="Markdown", reply_markup=no_plan_kb)
        return

    if dep_balance <= 0:
        text = (
            "⚠️ *رصيدك القابل للسحب صفر*\n\n"
            f"📦 باقتك النشطة: *{sub['plan_name']}* ✅\n\n"
            "لا يوجد رصيد متاح للسحب حالياً.\n"
            "استلم أرباحك يومياً وانتظر 15 يوم لتُفتح وتُحوَّل للرصيد."
        )
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id,
                                  message_id=call.message.message_id,
                                  parse_mode="Markdown", reply_markup=get_back_keyboard())
        except Exception:
            bot.send_message(call.message.chat.id, text,
                             parse_mode="Markdown", reply_markup=get_back_keyboard())
        return

    text = (
        "📤 *قسم سحب الأرباح*\n\n"
        f"📦 باقتك النشطة: *{sub['plan_name']}* ✅\n"
        f"💸 رصيدك القابل للسحب: *{fmt(dep_balance)} د.ع*\n\n"
        "اختر طريقة الاستلام:"
    )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode="Markdown", reply_markup=get_withdraw_method_keyboard())
    except Exception:
        bot.send_message(call.message.chat.id, text,
                         parse_mode="Markdown", reply_markup=get_withdraw_method_keyboard())


@bot.callback_query_handler(func=lambda c: c.data in WITHDRAW_METHODS)
def handle_withdraw_method(call):
    bot.answer_callback_query(call.id)
    uid    = call.from_user.id
    method = WITHDRAW_METHODS[call.data]
    user_states[uid] = STATE_WD_AMOUNT
    user_data[uid]   = {
        "method_key":   call.data,
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
        bot.send_message(call.message.chat.id, text,
                         parse_mode="Markdown", reply_markup=get_back_keyboard())


# ── 💎 Elite Club ──────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "menu_elite")
def handle_elite_menu(call):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    with get_conn() as conn:
        ensure_user(conn, uid)
        elite = get_elite_sub(conn, uid)
    if elite:
        text = (
            "💎 *نادي النخبة الرقمي*\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"✅ اشتراكك نشط حتى: *{elite['expiry_date']}*\n\n"
            "🎯 اختر الرادار الذي تريد الوصول إليه:"
        )
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id,
                                  message_id=call.message.message_id,
                                  parse_mode="Markdown", reply_markup=get_elite_radar_keyboard())
        except Exception:
            bot.send_message(call.message.chat.id, text,
                             parse_mode="Markdown", reply_markup=get_elite_radar_keyboard())
    else:
        text = (
            "💎 *نادي النخبة الرقمي*\n"
            "━━━━━━━━━━━━━━━━━\n"
            "🔒 هذا القسم حصري للأعضاء المميزين\n\n"
            "بـ *2,000 د.ع فقط* لمدة 15 يوم، تحصل على:\n\n"
            "🇮🇶 *رادار العروض العراقية*\n"
            "   ← أحدث عروض kds1.com ومنصة Midasbuy العراقية\n"
            "   ← تحديث تلقائي كل 30 دقيقة\n\n"
            "🌍 *أرشيف المصادر العالمية*\n"
            "   ← مراقبة أسعار Jamsmm و SMM-Main\n"
            "   ← تنبيه فوري عند انخفاض الأسعار\n\n"
            "📢 الرادارات تتحدث كل 30 دقيقة تلقائياً!"
        )
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("💳 اشترك الآن وتفعيل الكل", callback_data="elite_subscribe"),
            types.InlineKeyboardButton("🔙 رجوع",                   callback_data="menu_back"),
        )
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id,
                                  message_id=call.message.message_id,
                                  parse_mode="Markdown", reply_markup=markup)
        except Exception:
            bot.send_message(call.message.chat.id, text,
                             parse_mode="Markdown", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data == "elite_subscribe")
def handle_elite_subscribe(call):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    with get_conn() as conn:
        ensure_user(conn, uid)
        user  = get_user(conn, uid)
        elite = get_elite_sub(conn, uid)
        if elite:
            bot.send_message(call.message.chat.id, "✅ اشتراكك في النادي نشط بالفعل!",
                             reply_markup=get_main_menu())
            return
        if user["deposit_balance"] >= ELITE_COST:
            now_bagh = datetime.now(BAGHDAD_TZ)
            expiry   = (now_bagh + timedelta(days=ELITE_DAYS)).strftime("%Y-%m-%d")
            conn.execute(
                "UPDATE users SET deposit_balance = deposit_balance - ? WHERE user_id=?",
                (ELITE_COST, uid),
            )
            conn.execute(
                "INSERT INTO elite_subscriptions (user_id, start_date, expiry_date) VALUES (?,?,?)",
                (uid, now_bagh.strftime("%Y-%m-%d"), expiry),
            )
            add_transaction(conn, uid, "elite_payment", ELITE_COST,
                            description="اشتراك نادي النخبة الرقمي", status="approved")
            user = get_user(conn, uid)
            text = (
                "🎉 *مرحباً بك في نادي النخبة الرقمي!*\n"
                "━━━━━━━━━━━━━━━━━\n"
                f"✅ تم تفعيل اشتراكك حتى: *{expiry}*\n"
                f"💳 المبلغ المخصوم: *{fmt(ELITE_COST)} د.ع*\n"
                f"💸 رصيدك المتبقي: *{fmt(user['deposit_balance'])} د.ع*\n"
                "━━━━━━━━━━━━━━━━━\n"
                "يمكنك الآن الوصول إلى الرادارات! 🚀"
            )
        else:
            remaining = ELITE_COST - user["deposit_balance"]
            text = (
                "💔 *رصيدك غير كافٍ*\n\n"
                f"💸 رصيدك القابل للسحب: *{fmt(user['deposit_balance'])} د.ع*\n"
                f"💰 تكلفة الاشتراك: *{fmt(ELITE_COST)} د.ع*\n"
                f"⚠️ تحتاج إلى: *{fmt(remaining)} د.ع* إضافية.\n\n"
                "أودع المبلغ الناقص ثم حاول مجدداً."
            )
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode="Markdown", reply_markup=get_main_menu())
    except Exception:
        bot.send_message(call.message.chat.id, text,
                         parse_mode="Markdown", reply_markup=get_main_menu())


@bot.callback_query_handler(func=lambda c: c.data == "radar_iraq")
def handle_radar_iraq(call):
    bot.answer_callback_query(call.id, "⏳ جارٍ تحميل الرادار...")
    uid = call.from_user.id
    with get_conn() as conn:
        elite = get_elite_sub(conn, uid)
    if not elite:
        bot.send_message(call.message.chat.id, "⛔ هذه الخدمة حصرية لأعضاء نادي النخبة.")
        return
    with get_conn() as conn:
        items = conn.execute(
            "SELECT title, price_text, updated_at FROM price_cache "
            "WHERE source='iraq' ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()
    if not items:
        text = (
            "🇮🇶 *رادار العروض العراقية*\n\n"
            "⏳ جارٍ جمع البيانات...\n"
            "يتم تحديث الرادار كل 30 دقيقة.\n"
            "يرجى المحاولة بعد قليل."
        )
    else:
        updated = items[0]["updated_at"][:16]
        lines   = [f"🇮🇶 *رادار العروض العراقية*\n🕐 آخر تحديث: {updated}\n━━━━━━━━━━━━━━━━━"]
        for item in items:
            lines.append(f"🔹 *{item['title']}*\n   💰 {item['price_text']}")
        text = "\n\n".join(lines)
    sent = bot.send_message(call.message.chat.id, text,
                            parse_mode="Markdown", protect_content=True)
    threading.Timer(LINK_DELETE_SECS, safe_delete_message,
                    args=(call.message.chat.id, sent.message_id)).start()


@bot.callback_query_handler(func=lambda c: c.data == "radar_world")
def handle_radar_world(call):
    bot.answer_callback_query(call.id, "⏳ جارٍ تحميل الأرشيف...")
    uid = call.from_user.id
    with get_conn() as conn:
        elite = get_elite_sub(conn, uid)
    if not elite:
        bot.send_message(call.message.chat.id, "⛔ هذه الخدمة حصرية لأعضاء نادي النخبة.")
        return
    with get_conn() as conn:
        items = conn.execute(
            "SELECT title, price_text, updated_at FROM price_cache "
            "WHERE source='world' ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()
    if not items:
        text = (
            "🌍 *أرشيف المصادر العالمية*\n\n"
            "⏳ جارٍ جمع البيانات...\n"
            "يتم تحديث الأرشيف كل 30 دقيقة.\n"
            "يرجى المحاولة بعد قليل."
        )
    else:
        updated = items[0]["updated_at"][:16]
        lines   = [f"🌍 *أرشيف المصادر العالمية*\n🕐 آخر تحديث: {updated}\n━━━━━━━━━━━━━━━━━"]
        for item in items:
            lines.append(f"🔹 *{item['title']}*\n   💰 {item['price_text']}")
        text = "\n\n".join(lines)
    sent = bot.send_message(call.message.chat.id, text,
                            parse_mode="Markdown", protect_content=True)
    threading.Timer(LINK_DELETE_SECS, safe_delete_message,
                    args=(call.message.chat.id, sent.message_id)).start()


# ── Text: amounts ──────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: user_states.get(m.from_user.id) == STATE_DEP_AMOUNT and m.text)
def handle_deposit_amount(message):
    uid    = message.from_user.id
    amount = message.text.strip()
    user_data[uid]["amount"] = amount
    user_states[uid] = STATE_DEP_PHOTO
    bot.send_message(
        message.chat.id,
        f"💳 يرجى إرسال *{amount}* دينار إلى رقم زين كاش:\n\n"
        f"📱 `{ZAIN_CASH_NUMBER}`\n\n"
        "بعد التحويل أرسل صورة الوصل هنا.",
        parse_mode="Markdown", reply_markup=get_back_keyboard(),
    )


@bot.message_handler(func=lambda m: user_states.get(m.from_user.id) == STATE_WD_AMOUNT and m.text)
def handle_withdraw_amount(message):
    uid        = message.from_user.id
    amount_str = message.text.strip()
    try:
        requested = int("".join(filter(str.isdigit, amount_str)))
    except Exception:
        requested = 0
    with get_conn() as conn:
        ensure_user(conn, uid)
        user = get_user(conn, uid)
    if requested <= 0:
        bot.send_message(message.chat.id, "⚠️ يرجى إدخال مبلغ صحيح.")
        return
    if requested > user["deposit_balance"]:
        bot.send_message(
            message.chat.id,
            f"❌ *الرصيد غير كافٍ*\n\n"
            f"💸 رصيدك القابل للسحب: *{fmt(user['deposit_balance'])} د.ع*\n"
            f"💰 المبلغ المطلوب: *{fmt(requested)} د.ع*\n\n"
            "يرجى إدخال مبلغ أقل أو يساوي رصيدك القابل للسحب.",
            parse_mode="Markdown", reply_markup=get_back_keyboard(),
        )
        return
    user_data[uid]["amount"]     = amount_str
    user_data[uid]["amount_raw"] = requested
    user_states[uid] = STATE_WD_PHONE
    method_label = user_data[uid].get("method_label", "")
    bot.send_message(
        message.chat.id,
        f"✅ المبلغ: *{fmt(requested)} د.ع*\n\n"
        f"2️⃣ أدخل رقم *{method_label}* العراقي (11 رقم) لاستلام المبلغ:",
        parse_mode="Markdown", reply_markup=get_back_keyboard(),
    )


@bot.message_handler(func=lambda m: user_states.get(m.from_user.id) == STATE_WD_PHONE and m.text)
def handle_withdraw_phone(message):
    uid    = message.from_user.id
    phone  = message.text.strip()
    digits = re.sub(r"\D", "", phone)

    # ── Iraqi phone validation: must be exactly 11 digits ──
    if len(digits) != 11:
        bot.send_message(
            message.chat.id,
            "⚠️ الرقم غير صحيح! يجب أن يتكون رقم الهاتف العراقي من 11 رقماً "
            "(مثل 077xxxxxxxx). يرجى إعادة المحاولة.",
            reply_markup=get_back_keyboard(),
        )
        return  # Stay in STATE_WD_PHONE

    user_data[uid]["phone"] = phone
    user_states[uid] = STATE_IDLE
    local        = user_data[uid]
    method_label = local.get("method_label", "")
    amount_raw   = local.get("amount_raw", 0)
    bot.send_message(
        message.chat.id,
        f"📋 *ملخص طلب السحب*\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💰 المبلغ: *{fmt(amount_raw)} د.ع*\n"
        f"🛠 الطريقة: *{method_label}*\n"
        f"📞 الرقم: `{phone}`\n"
        f"━━━━━━━━━━━━━━━━━\n\n"
        "هل تريد تأكيد طلب السحب؟",
        parse_mode="Markdown", reply_markup=get_withdraw_confirm_keyboard(),
    )


@bot.callback_query_handler(func=lambda c: c.data == "withdraw_confirm")
def handle_withdraw_confirm(call):
    bot.answer_callback_query(call.id)
    uid          = call.from_user.id
    local        = user_data.get(uid, {})
    full_name    = call.from_user.full_name or "غير محدد"
    username     = call.from_user.username  or "غير محدد"
    amount_raw   = local.get("amount_raw", 0)
    method_short = local.get("method_short", "غير محدد")
    method_label = local.get("method_label", "غير محدد")
    phone        = local.get("phone", "غير محدد")

    if not amount_raw or phone == "غير محدد":
        bot.send_message(call.message.chat.id,
                         "❌ انتهت صلاحية الطلب. يرجى البدء من جديد.",
                         reply_markup=get_main_menu())
        return

    with get_conn() as conn:
        ensure_user(conn, uid, full_name, username)
        user   = get_user(conn, uid)
        txn_id = add_transaction(
            conn, uid, "withdrawal", amount_raw,
            description=f"سحب عبر {method_short} — {fmt(amount_raw)} د.ع — {phone}",
        )

    admin_text = (
        "⚠️ *طلب سحب جديد*\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"👤 المستخدم: {full_name}\n"
        f"🔖 المعرف: @{username}\n"
        f"🆔 رقم المستخدم: `{uid}`\n"
        f"💰 المبلغ: *{fmt(amount_raw)} د.ع*\n"
        f"🛠 الطريقة: *{method_label}*\n"
        f"📞 الرقم المستلم: `{phone}`\n"
        f"💸 رصيد قابل للسحب: {fmt(user['deposit_balance'])} د.ع\n"
        f"🆔 رقم العملية: #{txn_id}"
    )
    try:
        bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown",
                         reply_markup=get_admin_keyboard(uid, txn_id))
    except Exception as e:
        print(f"فشل إرسال إشعار السحب للمشرف: {e}")

    user_data[uid] = {}
    try:
        bot.edit_message_text(
            "✅ *تم إرسال طلب السحب بنجاح!*\n\nسيتم مراجعته وإرسال المبلغ إليك قريباً. 💎",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
        )
    except Exception:
        pass
    bot.send_message(call.message.chat.id, "للعودة للقائمة الرئيسية:",
                     reply_markup=get_main_menu())


# ── Photos: receipts ───────────────────────────────────────────────────────────

@bot.message_handler(
    content_types=["photo"],
    func=lambda m: user_states.get(m.from_user.id) in [STATE_DEP_PHOTO, STATE_PLAN_PHOTO],
)
def handle_receipt_photo(message):
    uid       = message.from_user.id
    state     = user_states.get(uid, STATE_IDLE)
    local     = user_data.get(uid, {})
    username  = message.from_user.username  or "غير محدد"
    full_name = message.from_user.full_name or "غير محدد"

    with get_conn() as conn:
        ensure_user(conn, uid, full_name, username)
        user = get_user(conn, uid)

        if state == STATE_DEP_PHOTO:
            amount_str = local.get("amount", "0")
            try:
                raw = int("".join(filter(str.isdigit, amount_str)))
            except Exception:
                raw = 0
            txn_id = add_transaction(conn, uid, "deposit", raw,
                                     description=f"إيداع مباشر — {amount_str} د.ع")
            caption = (
                "📥 *طلب إيداع جديد*\n"
                "━━━━━━━━━━━━━━━━━\n"
                f"👤 المستخدم: {full_name}\n"
                f"🔖 المعرف: @{username}\n"
                f"🆔 رقم المستخدم: `{uid}`\n"
                f"💰 المبلغ: *{amount_str}* د.ع\n"
                f"💸 رصيد قابل للسحب: {fmt(user['deposit_balance'])} د.ع\n"
                f"🆔 رقم العملية: #{txn_id}"
            )
            confirm_msg = "✅ تم إرسال وصل الإيداع!\nسيتم مراجعته والرد عليك قريباً."

        else:  # STATE_PLAN_PHOTO
            plan_key        = local.get("plan_key", "")
            plan_label      = local.get("plan_label", "غير محدد")
            plan_amount     = local.get("plan_amount", "0")
            plan_raw        = local.get("plan_raw", 0)
            plan_days       = local.get("plan_days", PLAN_LOCK_DAYS)
            is_plan_deposit = local.get("is_plan_deposit", False)
            deposit_amount  = local.get("deposit_amount", plan_raw)
            total_profit    = fmt(int(plan_raw * PROFIT_MULT))

            if is_plan_deposit:
                txn_id = add_transaction(conn, uid, "deposit", deposit_amount,
                                         description=f"إيداع لباقة|{plan_key}|{plan_label}")
                caption = (
                    "📥 *إيداع لتفعيل باقة*\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"👤 المستخدم: {full_name}\n"
                    f"🔖 المعرف: @{username}\n"
                    f"🆔 رقم المستخدم: `{uid}`\n"
                    f"📦 الباقة المطلوبة: *{plan_label}*\n"
                    f"💰 تكلفة الباقة: *{plan_amount}* د.ع\n"
                    f"💸 المبلغ المُودَع: *{fmt(deposit_amount)}* د.ع\n"
                    f"💹 الربح المتوقع الإجمالي: *{total_profit}* د.ع\n"
                    f"📅 مدة القفل: *{plan_days} يوم*\n"
                    f"💸 رصيد المستخدم الحالي: {fmt(user['deposit_balance'])} د.ع\n"
                    f"🆔 رقم العملية: #{txn_id}\n\n"
                    "⚡ الموافقة ستُفعّل الباقة تلقائياً."
                )
                confirm_msg = (
                    "✅ *تم إرسال وصل الإيداع بنجاح!*\n\n"
                    "سيتم مراجعته وتفعيل باقتك تلقائياً فور موافقة المشرف. ⚡"
                )
            else:
                txn_id = add_transaction(conn, uid, "plan_payment", plan_raw,
                                         description=f"{plan_label}|{plan_key}|{plan_amount} د.ع")
                caption = (
                    "📊 *طلب اشتراك باقة*\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"👤 المستخدم: {full_name}\n"
                    f"🔖 المعرف: @{username}\n"
                    f"🆔 رقم المستخدم: `{uid}`\n"
                    f"📦 الباقة: *{plan_label}*\n"
                    f"💰 المبلغ: *{plan_amount}* د.ع\n"
                    f"🆔 رقم العملية: #{txn_id}"
                )
                confirm_msg = "✅ تم إرسال وصل الاشتراك!\nسيتم مراجعته وتفعيل باقتك قريباً."
            user_data[uid]["txn_id"] = txn_id

    photo_file_id = message.photo[-1].file_id
    try:
        bot.send_photo(ADMIN_ID, photo_file_id, caption=caption,
                       parse_mode="Markdown",
                       reply_markup=get_admin_keyboard(uid, txn_id))
    except Exception as e:
        print(f"فشل إرسال الإشعار للمشرف: {e}")

    user_states[uid] = STATE_IDLE
    bot.send_message(message.chat.id, confirm_msg,
                     parse_mode="Markdown", reply_markup=get_main_menu())


# ── Admin: approve / reject ────────────────────────────────────────────────────

@bot.callback_query_handler(
    func=lambda c: c.data.startswith("approve_") or c.data.startswith("reject_")
)
def handle_admin_decision(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ غير مصرح لك.")
        return

    parts          = call.data.split("_", 2)
    action         = parts[0]
    target_uid     = int(parts[1])
    txn_id         = int(parts[2])

    with get_conn() as conn:
        txn = get_transaction(conn, txn_id)
        if not txn:
            bot.answer_callback_query(call.id, "⚠️ العملية غير موجودة.")
            return
        if txn["status"] != "pending":
            bot.answer_callback_query(call.id, "⚠️ تم معالجة هذه العملية مسبقاً.")
            return

        ensure_user(conn, target_uid)
        txn_type   = txn["type"]
        amount     = txn["amount"]

        if action == "approve":
            update_transaction_status(conn, txn_id, "approved")

            # ── Deposit ───────────────────────────────────────────────────────
            if txn_type == "deposit":
                conn.execute(
                    "UPDATE users SET deposit_balance = deposit_balance + ? WHERE user_id=?",
                    (amount, target_uid),
                )
                # Linked to a plan? (Case B auto-activation)
                linked_plan_key = None
                desc_parts = txn["description"].split("|")
                if len(desc_parts) >= 2 and desc_parts[1] in PLANS:
                    linked_plan_key = desc_parts[1]

                if linked_plan_key:
                    plan      = PLANS[linked_plan_key]
                    plan_cost = plan["raw"]
                    user      = get_user(conn, target_uid)
                    if user["deposit_balance"] >= plan_cost:
                        today = datetime.now(BAGHDAD_TZ).strftime("%Y-%m-%d")
                        conn.execute(
                            """UPDATE users
                               SET deposit_balance   = deposit_balance - ?,
                                   active_plan_price = active_plan_price + ?,
                                   profit_lock_start = COALESCE(profit_lock_start, ?)
                               WHERE user_id=?""",
                            (plan_cost, plan_cost, today, target_uid),
                        )
                        add_transaction(conn, target_uid, "plan_payment", plan_cost,
                                        description=f"تفعيل تلقائي|{linked_plan_key}|{plan['label']}",
                                        status="approved")
                        add_subscription(conn, target_uid, linked_plan_key, plan_cost, plan["days"])
                        user        = get_user(conn, target_uid)
                        expiry      = expiry_from_now(plan["days"])
                        daily_prof  = fmt(int((plan_cost * PROFIT_MULT) / PLAN_LOCK_DAYS))
                        user_msg = (
                            "🎉 *تمت الموافقة وتفعيل باقتك تلقائياً!*\n"
                            "━━━━━━━━━━━━━━━━━\n"
                            f"💰 المبلغ المُضاف: *{fmt(amount)} د.ع*\n"
                            f"📦 الباقة: *{plan['label']}*\n"
                            f"📈 الربح اليومي: *{daily_prof} د.ع*\n"
                            f"📅 مدة القفل: *{plan['days']} يوم*\n"
                            f"🗓 تاريخ الانتهاء: *{expiry}*\n"
                            "━━━━━━━━━━━━━━━━━\n"
                            "💰 استلم أرباحك يومياً بين *10 ص — 10 م* ✅"
                        )
                        admin_note = f"✅ قُبل — أُضيف {fmt(amount)} د.ع وفُعِّلت {plan['label']} تلقائياً"
                    else:
                        user = get_user(conn, target_uid)
                        user_msg = (
                            "🎉 *تمت الموافقة على إيداعك!*\n"
                            "━━━━━━━━━━━━━━━━━\n"
                            f"💰 المبلغ المُضاف: *{fmt(amount)} د.ع*\n"
                            f"💸 رصيدك القابل للسحب: *{fmt(user['deposit_balance'])} د.ع*\n"
                            "━━━━━━━━━━━━━━━━━\n"
                            f"⚠️ رصيدك لا يزال غير كافٍ لتفعيل *{plan['label']}*."
                        )
                        admin_note = f"✅ قُبل — أُضيف {fmt(amount)} د.ع (رصيد غير كافٍ للباقة)"
                else:
                    user = get_user(conn, target_uid)
                    user_msg = (
                        "🎉 *تمت الموافقة على إيداعك!*\n"
                        "━━━━━━━━━━━━━━━━━\n"
                        f"💰 المبلغ المُضاف: *{fmt(amount)} د.ع*\n"
                        f"💸 رصيدك القابل للسحب: *{fmt(user['deposit_balance'])} د.ع*\n"
                        "━━━━━━━━━━━━━━━━━\n"
                        "✅ تم تحديث حسابك بنجاح. 💎"
                    )
                    admin_note = f"✅ قُبل — أُضيف {fmt(amount)} د.ع"

            # ── Plan payment ──────────────────────────────────────────────────
            elif txn_type == "plan_payment":
                plan_key = None
                desc_parts = txn["description"].split("|")
                if len(desc_parts) >= 2 and desc_parts[1] in PLANS:
                    plan_key = desc_parts[1]
                if not plan_key:
                    for k, p in PLANS.items():
                        if p["label"] in txn["description"]:
                            plan_key = k; break
                if not plan_key:
                    for k, p in PLANS.items():
                        if p["raw"] == int(amount):
                            plan_key = k; break
                plan_days  = PLANS[plan_key]["days"]  if plan_key else PLAN_LOCK_DAYS
                plan_label = PLANS[plan_key]["label"] if plan_key else "الباقة"
                expiry     = expiry_from_now(plan_days)
                user       = get_user(conn, target_uid)

                if user["deposit_balance"] < amount:
                    update_transaction_status(conn, txn_id, "rejected")
                    user_msg   = (
                        "❌ *تعذّر تفعيل الاشتراك — رصيد غير كافٍ*\n\n"
                        f"💸 رصيدك القابل للسحب: *{fmt(user['deposit_balance'])} د.ع*\n"
                        f"💰 تكلفة الباقة: *{fmt(amount)} د.ع*"
                    )
                    admin_note = "❌ رُفض تلقائياً — رصيد غير كافٍ"
                    try:
                        bot.send_message(target_uid, user_msg, parse_mode="Markdown",
                                         reply_markup=get_main_menu())
                    except Exception:
                        pass
                    bot.answer_callback_query(call.id, admin_note)
                    _update_admin_msg(call, admin_note)
                    return

                today = datetime.now(BAGHDAD_TZ).strftime("%Y-%m-%d")
                conn.execute(
                    """UPDATE users
                       SET deposit_balance   = deposit_balance - ?,
                           active_plan_price = active_plan_price + ?,
                           profit_lock_start = COALESCE(profit_lock_start, ?)
                       WHERE user_id=?""",
                    (amount, amount, today, target_uid),
                )
                if plan_key:
                    add_subscription(conn, target_uid, plan_key, amount, plan_days)
                user        = get_user(conn, target_uid)
                daily_prof  = fmt(int((amount * PROFIT_MULT) / PLAN_LOCK_DAYS))
                user_msg = (
                    "🎉 *تم تفعيل اشتراكك بنجاح!*\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"📦 الباقة: *{plan_label}*\n"
                    f"💰 المبلغ المستثمر: *{fmt(amount)} د.ع*\n"
                    f"📈 الربح اليومي: *{daily_prof} د.ع*\n"
                    f"📅 مدة القفل: *{plan_days} يوم*\n"
                    f"🗓 تاريخ الانتهاء: *{expiry}*\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    "💰 استلم أرباحك يومياً بين *10 ص — 10 م* ✅"
                )
                admin_note = f"✅ قُبل — باقة {plan_label} مُفعّلة"

            # ── Withdrawal ────────────────────────────────────────────────────
            else:
                user = get_user(conn, target_uid)
                if user["deposit_balance"] < amount:
                    update_transaction_status(conn, txn_id, "rejected")
                    user_msg   = "❌ تعذّر تنفيذ السحب — رصيد غير كافٍ."
                    admin_note = "❌ رُفض — رصيد غير كافٍ"
                    try:
                        bot.send_message(target_uid, user_msg, reply_markup=get_main_menu())
                    except Exception:
                        pass
                    bot.answer_callback_query(call.id, admin_note)
                    _update_admin_msg(call, admin_note)
                    return
                conn.execute(
                    "UPDATE users SET deposit_balance = deposit_balance - ? WHERE user_id=?",
                    (amount, target_uid),
                )
                user = get_user(conn, target_uid)
                user_msg = (
                    "✅ *تمت الموافقة على طلب السحب!*\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"💸 المبلغ المسحوب: *{fmt(amount)} د.ع*\n"
                    f"💸 رصيدك المتبقي: *{fmt(user['deposit_balance'])} د.ع*\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    "سيصلك المبلغ قريباً. 💎"
                )
                admin_note = f"✅ قُبل — سُحب {fmt(amount)} د.ع"

        else:  # reject
            update_transaction_status(conn, txn_id, "rejected")
            type_label = TYPE_LABELS.get(txn_type, "العملية")
            user_msg   = (
                f"❌ *تم رفض طلبك ({type_label})*\n\n"
                f"المبلغ: *{fmt(amount)} د.ع*\n\n"
                "يرجى التأكد من صحة الوصل وإعادة المحاولة."
            )
            admin_note = f"❌ مرفوض — {fmt(amount)} د.ع"

    try:
        bot.send_message(target_uid, user_msg, parse_mode="Markdown",
                         reply_markup=get_main_menu())
    except Exception as e:
        print(f"فشل إرسال الرد للمستخدم: {e}")

    bot.answer_callback_query(call.id, admin_note)
    _update_admin_msg(call, admin_note)


# ── Fallback ───────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: True)
def handle_unknown(message):
    uid   = message.from_user.id
    state = user_states.get(uid, STATE_IDLE)
    if state == STATE_DEP_AMOUNT:
        bot.send_message(message.chat.id, "يرجى إدخال مبلغ صحيح للإيداع.")
    elif state == STATE_WD_AMOUNT:
        bot.send_message(message.chat.id, "يرجى إدخال مبلغ صحيح للسحب.")
    elif state == STATE_WD_PHONE:
        bot.send_message(
            message.chat.id,
            "⚠️ الرقم غير صحيح! يجب أن يتكون رقم الهاتف العراقي من 11 رقماً "
            "(مثل 077xxxxxxxx). يرجى إعادة المحاولة.",
        )
    elif state in [STATE_DEP_PHOTO, STATE_PLAN_PHOTO]:
        bot.send_message(message.chat.id, "📸 يرجى إرسال *صورة* الوصل للمتابعة.",
                         parse_mode="Markdown")
    else:
        bot.send_message(message.chat.id, WELCOME_TEXT,
                         parse_mode="Markdown", reply_markup=get_main_menu())


# ── Scrapers ───────────────────────────────────────────────────────────────────

def _try_scrape_table(url, label):
    results = []
    if not SCRAPER_OK:
        return results
    try:
        r    = requests.get(url, headers=HTTP_HEADERS, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("tr")[:25]:
            cells = row.select("td")
            if len(cells) >= 2:
                name  = cells[0].get_text(strip=True)[:60]
                price = cells[-1].get_text(strip=True)[:25]
                if name and price and name != price:
                    results.append({"title": f"{label}: {name}", "price_text": price, "url": url})
    except Exception as e:
        print(f"[Scraper] {label}: {e}")
    return results[:12]


def scrape_kds1():
    results = []
    if not SCRAPER_OK:
        return results
    try:
        r    = requests.get("https://kds1.com", headers=HTTP_HEADERS, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        selectors = [".product", ".item", ".card", "article", "li.product"]
        for sel in selectors:
            for card in soup.select(sel)[:15]:
                title_el = card.select_one("h2,h3,h4,.title,.name,a")
                price_el = card.select_one(".price,.cost,.amount,[class*=price]")
                if title_el and price_el:
                    t = title_el.get_text(strip=True)[:60]
                    p = price_el.get_text(strip=True)[:25]
                    if t and p and t != p:
                        results.append({"title": t, "price_text": p, "url": "https://kds1.com"})
            if results:
                break
    except Exception as e:
        print(f"[Scraper] kds1.com: {e}")
    return results[:12]


def scrape_midasbuy():
    results = []
    if not SCRAPER_OK:
        return results
    url = "https://www.midasbuy.com/midasbuy/uc/buyProduct?area=IQ"
    try:
        r    = requests.get(url, headers=HTTP_HEADERS, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        for el in soup.select("[class*=product],[class*=item],[class*=package]")[:15]:
            title = el.select_one("h2,h3,.title,.name")
            price = el.select_one("[class*=price],[class*=cost]")
            if title and price:
                results.append({
                    "title":      f"Midasbuy IQ: {title.get_text(strip=True)[:50]}",
                    "price_text": price.get_text(strip=True)[:25],
                    "url":        url,
                })
    except Exception as e:
        print(f"[Scraper] Midasbuy: {e}")
    return results[:8]


def scrape_jamsmm():
    return _try_scrape_table("https://jamsmm.com/services", "Jamsmm")


def scrape_smmain():
    for base in ["https://smm-main.com/services", "https://smmain.com/services"]:
        r = _try_scrape_table(base, "SMM-Main")
        if r:
            return r
    return []


def radar_scraper_task():
    print("[Radar] Scraper running...")
    iraq_items  = scrape_kds1() + scrape_midasbuy()
    world_items = scrape_jamsmm() + scrape_smmain()
    with get_conn() as conn:
        if iraq_items:
            conn.execute("DELETE FROM price_cache WHERE source='iraq'")
            for item in iraq_items:
                conn.execute(
                    "INSERT INTO price_cache (source, title, price_text, url) VALUES (?,?,?,?)",
                    ("iraq", item["title"], item["price_text"], item["url"]),
                )
        if world_items:
            conn.execute("DELETE FROM price_cache WHERE source='world'")
            for item in world_items:
                conn.execute(
                    "INSERT INTO price_cache (source, title, price_text, url) VALUES (?,?,?,?)",
                    ("world", item["title"], item["price_text"], item["url"]),
                )
    print(f"[Radar] Done — Iraq: {len(iraq_items)}, World: {len(world_items)}")


# ── Profit Unlock Scheduler ────────────────────────────────────────────────────

def unlock_profits_task():
    """Runs every hour. After 15 days from profit_lock_start, moves
    locked_profits + active_plan_price back to deposit_balance."""
    cutoff = (datetime.now(BAGHDAD_TZ) - timedelta(days=PLAN_LOCK_DAYS)).strftime("%Y-%m-%d")
    print(f"[Unlock] Checking profit locks (cutoff={cutoff})")
    with get_conn() as conn:
        users = conn.execute(
            """SELECT user_id, locked_profits, active_plan_price
               FROM users
               WHERE locked_profits > 0
                 AND profit_lock_start IS NOT NULL
                 AND profit_lock_start <= ?""",
            (cutoff,),
        ).fetchall()
        for u in users:
            unlocked   = u["locked_profits"]
            plan_back  = u["active_plan_price"]
            total_back = unlocked + plan_back
            conn.execute(
                """UPDATE users
                   SET deposit_balance   = deposit_balance + ?,
                       locked_profits    = 0,
                       active_plan_price = 0,
                       profit_lock_start = NULL
                   WHERE user_id=?""",
                (total_back, u["user_id"]),
            )
            conn.execute(
                "UPDATE subscriptions SET is_active=0 WHERE user_id=? AND is_active=1",
                (u["user_id"],),
            )
            add_transaction(conn, u["user_id"], "profit_unlock", total_back,
                            description=f"فتح الأرباح — ربح {fmt(unlocked)} + رأس مال {fmt(plan_back)}",
                            status="approved")
            msg = (
                "🎉 *انتهت مدة الخطة — أرباحك مفتوحة الآن!*\n"
                "━━━━━━━━━━━━━━━━━\n"
                f"💹 الأرباح المُفتوحة: *{fmt(unlocked)} د.ع*\n"
                f"💎 رأس المال المُسترد: *{fmt(plan_back)} د.ع*\n"
                f"💸 الإجمالي المُضاف: *{fmt(total_back)} د.ع*\n"
                "━━━━━━━━━━━━━━━━━\n"
                "✅ يمكنك الآن سحب كامل أرباحك!"
            )
            try:
                bot.send_message(u["user_id"], msg, parse_mode="Markdown",
                                 reply_markup=get_main_menu())
            except Exception as e:
                print(f"[Unlock] Failed to notify {u['user_id']}: {e}")
    if users:
        print(f"[Unlock] Unlocked for {len(users)} user(s).")


# ── Schedulers ─────────────────────────────────────────────────────────────────

def start_scheduler():
    scheduler = BackgroundScheduler(timezone=BAGHDAD_TZ)
    scheduler.add_job(unlock_profits_task, trigger="interval", hours=1,
                      id="unlock_profits", replace_existing=True)
    scheduler.add_job(radar_scraper_task, trigger="interval", minutes=30,
                      id="radar_scraper", replace_existing=True)
    scheduler.start()
    print("[Scheduler] Started — unlock: every 1h | radar: every 30min")
    return scheduler


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    threading.Thread(target=radar_scraper_task, daemon=True).start()
    start_scheduler()
    print(f"Bot started — token: {BOT_TOKEN[:20]}...")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
