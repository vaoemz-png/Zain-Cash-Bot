# =============================================================================
#  🏬 متجر دراهمكو الرقمي — Drahimco Digital Store Bot
# -----------------------------------------------------------------------------
#  Framework : python-telegram-bot >= 21  (Asyncio)
#  Install   : pip install "python-telegram-bot>=21.0"
#  Run       : python bot.py
# =============================================================================

# =============================================================================
#  🔑⚙️  عدّل هنا فقط — EDIT THIS SECTION ONLY  ⚙️🔑
# =============================================================================
BOT_TOKEN = "8732208163:AAEa7cd0tY3-anfL7AunFvJT1Cc2LggKMBM"          # <--ن BotFather هنا
ADMIN_ID   = 122498736                # <-- آيدي حسابك (الأدمن)

# بيانات الدفع (يمكن تعديلها بسهولة من هنا)
ZAINCASH_NUMBER  = "07713356493"      # رقم محفظة زين كاش
MASTERCARD_NUMBER = "1276225909"      # رقم بطاقة/حساب ماستركارد
SUPPORT_CONTACT   = "@DrahimcoSupport" # حساب الدعم الفني

# إعدادات العملة والحدود
USD_TO_IQD       = 1320               # سعر صرف تقريبي للعرض فقط
MIN_DEPOSIT_IQD  = 5_000              # الحد الأدنى للإيداع
MIN_WITHDRAW_IQD = 5_000              # الحد الأدنى للسحب

# مسار قاعدة البيانات (ملف محلي sqlite)
DB_PATH = "drahimco.sqlite3"

# مهلة إنهاء المحادثة تلقائياً (ثواني)
CONVERSATION_TIMEOUT = 15 * 60
# =============================================================================
#  ⛔ لا تعدّل أي شيء تحت هذا السطر إلا إذا كنت تعرف ماذا تفعل
# =============================================================================

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timezone
from enum import IntEnum
from html import escape
from typing import Any, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("drahimco.bot")


# =============================================================================
#  1) UTILITIES
# =============================================================================
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_amount(raw: Optional[str]) -> Optional[int]:
    """يحوّل النص إلى رقم صحيح (يدعم الأرقام العربية/الفارسية والفاصلات)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw.translate(_ARABIC_DIGITS))
    if not digits:
        return None
    value = int(digits)
    return value if value > 0 else None


async def safe_edit(query, text: str, keyboard: Optional[InlineKeyboardMarkup] = None) -> None:
    """تعديل رسالة — مع إرسال رسالة جديدة عند الحاجة."""
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        await query.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except TelegramError:
        logger.warning("safe_edit failed", exc_info=True)


# =============================================================================
#  2) DATABASE (SQLite — يمكن استبدالها بـ PostgreSQL)
# =============================================================================
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    balance     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    method        TEXT NOT NULL,
    amount        INTEGER NOT NULL,
    details       TEXT,
    proof_file_id TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);

CREATE INDEX IF NOT EXISTS idx_requests_status ON requests (status, kind);
CREATE INDEX IF NOT EXISTS idx_requests_user   ON requests (user_id);
"""


class Database:
    """غلاف غير متزامن حول SQLite (blocking calls run in a thread)."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _run(self, fn):
        conn = self._connect()
        try:
            result = fn(conn)
            conn.commit()
            return result
        finally:
            conn.close()

    async def _call(self, fn):
        async with self._lock:
            return await asyncio.to_thread(self._run, fn)

    async def init(self) -> None:
        await self._call(lambda conn: conn.executescript(_SCHEMA))
        logger.info("Database ready at %s", self._path)

    async def upsert_user(self, user_id: int, username: Optional[str], full_name: Optional[str]) -> None:
        now = _now()

        def _fn(conn):
            conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, balance, created_at, updated_at)
                VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username   = excluded.username,
                    full_name  = excluded.full_name,
                    updated_at = excluded.updated_at
                """,
                (user_id, username, full_name, now, now),
            )

        await self._call(_fn)

    async def get_balance(self, user_id: int) -> int:
        def _fn(conn) -> int:
            row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return int(row["balance"]) if row else 0

        return await self._call(_fn)

    async def adjust_balance(self, user_id: int, delta: int) -> int:
        now = _now()

        def _fn(conn) -> int:
            conn.execute(
                "UPDATE users SET balance = balance + ?, updated_at = ? WHERE user_id = ?",
                (delta, now, user_id),
            )
            row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return int(row["balance"]) if row else 0

        return await self._call(_fn)

    async def user_stats(self, user_id: int) -> dict[str, int]:
        def _fn(conn) -> dict[str, int]:
            row = conn.execute(
                """
                SELECT
                  (SELECT COALESCE(SUM(amount), 0) FROM requests
                     WHERE user_id = ? AND kind = 'deposit'  AND status = 'approved') AS deposits,
                  (SELECT COALESCE(SUM(amount), 0) FROM requests
                     WHERE user_id = ? AND kind = 'withdraw' AND status = 'paid')     AS withdrawals,
                  (SELECT COUNT(*) FROM requests
                     WHERE user_id = ? AND status = 'pending')                        AS pending
                """,
                (user_id, user_id, user_id),
            ).fetchone()
            return {k: int(row[k]) for k in ("deposits", "withdrawals", "pending")}

        return await self._call(_fn)

    async def create_request(
        self, user_id: int, kind: str, method: str, amount: int,
        details: Optional[str] = None, proof_file_id: Optional[str] = None,
    ) -> int:
        now = _now()

        def _fn(conn) -> int:
            cursor = conn.execute(
                """
                INSERT INTO requests (user_id, kind, method, amount, details,
                                      proof_file_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (user_id, kind, method, amount, details, proof_file_id, now, now),
            )
            return int(cursor.lastrowid)

        return await self._call(_fn)

    async def get_request(self, request_id: int) -> Optional[dict[str, Any]]:
        def _fn(conn) -> Optional[dict[str, Any]]:
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
            return dict(row) if row else None

        return await self._call(_fn)

    async def set_request_status(self, request_id: int, status: str) -> None:
        now = _now()

        def _fn(conn):
            conn.execute(
                "UPDATE requests SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, request_id),
            )

        await self._call(_fn)

    async def count_pending(self, kind: str) -> int:
        def _fn(conn) -> int:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM requests WHERE kind = ? AND status = 'pending'",
                (kind,),
            ).fetchone()
            return int(row["c"])

        return await self._call(_fn)

    async def list_pending(self, kind: str, limit: int = 20) -> list[dict[str, Any]]:
        def _fn(conn) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT r.*, u.full_name, u.username
                FROM requests r LEFT JOIN users u ON u.user_id = r.user_id
                WHERE r.kind = ? AND r.status = 'pending'
                ORDER BY r.id ASC LIMIT ?
                """,
                (kind, limit),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._call(_fn)


db = Database(DB_PATH)


# =============================================================================
#  3) FSM STATES
# =============================================================================
class S(IntEnum):
    DEPOSIT_METHOD   = 0
    DEPOSIT_AMOUNT   = 1
    DEPOSIT_PROOF    = 2
    WITHDRAW_METHOD  = 3
    WITHDRAW_AMOUNT  = 4
    WITHDRAW_DETAILS = 5


# =============================================================================
#  4) STATIC CONTENT
# =============================================================================
PACKAGES: dict[str, dict[str, Any]] = {
    "1": {"emoji": "🥉", "name": "الباقة البرونزية", "capital": 10,  "profit": 5,  "duration": "أسبوع واحد (7 أيام)"},
    "2": {"emoji": "🥈", "name": "الباقة الفضية",   "capital": 25,  "profit": 15, "duration": "شهر واحد (30 يوماً)"},
    "3": {"emoji": "🥇", "name": "الباقة الذهبية",  "capital": 50,  "profit": 25, "duration": "شهر واحد (30 يوماً)"},
    "4": {"emoji": "💎", "name": "الباقة الماسية",  "capital": 100, "profit": 35, "duration": "شهر واحد (30 يوماً)"},
}

METHOD_INFO: dict[str, dict[str, Any]] = {
    "asiacell": {
        "button": "🟡 كروت آسيا سيل",
        "title": "🟡 <b>الإيداع عبر كروت آسيا سيل</b>",
        "instructions": (
            "خطوات الإيداع:\n"
            "1️⃣ اشترِ كارت آسيا سيل بقيمة المبلغ المطلوب.\n"
            "2️⃣ اكشط الكارت ثم أرسل لنا <b>صورة الكارت</b> مع كتابة <b>رقم الكارت</b>.\n\n"
            "⚠️ لا تُشارك رقم الكارت مع أي شخص آخر غير هذا البوت."
        ),
        "proof_prompt": "📷 أرسل الآن <b>صورة الكارت</b> مع <b>رقم الكارت</b> في وصف الصورة.",
        "needs_photo": True,
    },
    "zaincash": {
        "button": "🔴 زين كاش",
        "title": "🔴 <b>الإيداع عبر زين كاش</b>",
        "instructions": (
            "خطوات الإيداع:\n"
            f"1️⃣ حوّل المبلغ إلى رقم المحفظة: <code>{ZAINCASH_NUMBER}</code>\n"
            "2️⃣ احتفظ بإشعار التحويل.\n"
            "3️⃣ أرسل لنا صورة الإشعار هنا."
        ),
        "proof_prompt": "📷 أرسل الآن <b>لقطة شاشة</b> لإشعار التحويل.",
        "needs_photo": True,
    },
    "mastercard": {
        "button": "🔵 ماستركارد",
        "title": "🔵 <b>الإيداع عبر ماستركارد</b>",
        "instructions": (
            "خطوات الإيداع:\n"
            f"1️⃣ حوّل المبلغ إلى رقم البطاقة/الحساب: <code>{MASTERCARD_NUMBER}</code>\n"
            "2️⃣ احتفظ بإشعار التحويل.\n"
            "3️⃣ أرسل لنا صورة الإشعار هنا."
        ),
        "proof_prompt": "📷 أرسل الآن <b>لقطة شاشة</b> لإشعار التحويل.",
        "needs_photo": True,
    },
}

WITHDRAW_METHODS: dict[str, dict[str, str]] = {
    "zaincash": {
        "button": "🔴 زين كاش",
        "label": "زين كاش (ZainCash)",
        "prompt": (
            "🔴 <b>استلام الأموال عبر زين كاش</b>\n\n"
            "✍️ أرسل الآن <b>رقم محفظة زين كاش</b> (11 رقماً يبدأ بـ 07)، مثال: <code>07701234567</code>"
        ),
    },
    "mastercard": {
        "button": "🔵 ماستركارد",
        "label": "ماستركارد (Mastercard)",
        "prompt": (
            "🔵 <b>استلام الأموال عبر ماستركارد</b>\n\n"
            "✍️ أرسل الآن <b>رقم البطاقة / الحساب</b> بالكامل."
        ),
    },
}

WELCOME_TEXT = (
    "💎 أهلاً بك يا {name} في متجر <b>دراهمكو الرقمي</b>\n\n"
    "البوت الأول في العراق لتحويل النقاط إلى أرباح حقيقية 🇮🇶\n\n"
    "اختر من الأزرار أدناه:"
)


def packages_text() -> str:
    parts: list[str] = [
        "🚀 <b>الباقات السريعة — استثمارك الآمن في العراق</b> 🇮🇶",
        "",
        "هل تعلم أن الأموال الراكدة تفقد قيمتها كل يوم؟ 💡",
        "في <b>دراهمكو الرقمي</b> نحوّل مدخراتك إلى أرباح حقيقية تُسحب بالدينار العراقي مباشرة إلى محفظتك أو بطاقتك.",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "<b>⚙️ كيف تعمل الباقات؟</b>",
        "1️⃣ تختار الباقة المناسبة وتودع رأس المال.",
        "2️⃣ فريقنا يشغّل رأس المال في مشاريع تجارية مربحة.",
        "3️⃣ عند انتهاء المدة تستلم <b>رأس المال + الربح كاملاً</b> بدون رسوم خفية.",
        "",
        "<b>🛡 لماذا يثق بنا آلاف العراقيين؟</b>",
        "✅ أرباح ثابتة ومعلنة مسبقاً — لا مفاجآت ولا شروط مخفية.",
        "✅ إيصال رسمي لكل عملية إيداع وسحب داخل البوت.",
        "✅ سحب الأرباح عبر ZainCash أو Mastercard خلال دقائق.",
        "✅ دعم فني عراقي على مدار الساعة طوال أيام الأسبوع.",
        "🔒 أموالك محفوظة وتُعالج وفق أعلى معايير الأمان.",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "<b>📦 الباقات المتاحة:</b>",
        "",
    ]
    for pkg in PACKAGES.values():
        roi = round(pkg["profit"] / pkg["capital"] * 100)
        parts.append(
            f"{pkg['emoji']} <b>{pkg['name']}</b>\n"
            f"   💵 رأس المال: <b>{pkg['capital']}$</b>\n"
            f"   📈 الربح الصافي: <b>{pkg['profit']}$</b>\n"
            f"   ⏳ المدة: {pkg['duration']}\n"
            f"   🎯 نسبة العائد: <b>{roi}%</b>\n"
        )
    parts.append("👇 اختر الباقة التي تناسبك من الأزرار أدناه:")
    return "\n".join(parts)


# =============================================================================
#  5) KEYBOARDS
# =============================================================================
def main_menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🚀 الباقات السريعة", callback_data="pkg:list")],
        [
            InlineKeyboardButton("💳 إيداع", callback_data="dep:start"),
            InlineKeyboardButton("💸 سحب",  callback_data="wdr:start"),
        ],
        [InlineKeyboardButton("📊 حسابي", callback_data="acc:view")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🛠 لوحة التحكم", callback_data="adm:panel")])
    return InlineKeyboardMarkup(rows)


def packages_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🛒 اشترِ {pkg['name']} — {pkg['capital']}$", callback_data=f"pkg:buy:{key}")]
        for key, pkg in PACKAGES.items()
    ]
    rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="ui:home")])
    return InlineKeyboardMarkup(rows)


def deposit_methods_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟡 كروت آسيا سيل", callback_data="dep:m:asiacell")],
        [InlineKeyboardButton("🔴 زين كاش",       callback_data="dep:m:zaincash")],
        [InlineKeyboardButton("🔵 ماستركارد",     callback_data="dep:m:mastercard")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="ui:home")],
    ])


def withdraw_methods_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 زين كاش",   callback_data="wdr:m:zaincash")],
        [InlineKeyboardButton("🔵 ماستركارد", callback_data="wdr:m:mastercard")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="ui:home")],
    ])


def flow_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="ui:home"),
        InlineKeyboardButton("❌ إلغاء",            callback_data="ui:cancel"),
    ]])


# =============================================================================
#  6) SHARED HANDLERS
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if update.effective_chat is None:
        return ConversationHandler.END
    await db.upsert_user(user.id, user.username, user.full_name)
    name = escape(user.first_name or str(user.id))
    await update.effective_chat.send_message(
        WELCOME_TEXT.format(name=name),
        reply_markup=main_menu_keyboard(user.id == ADMIN_ID),
        parse_mode=ParseMode.HTML,
    )
    context.user_data.pop("deposit", None)
    context.user_data.pop("withdraw", None)
    return ConversationHandler.END


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(
        "ℹ️ <b>المساعدة والدعم</b>\n\n"
        "• 💳 <b>إيداع</b>: أضف رصيداً عبر آسيا سيل أو زين كاش أو ماستركارد.\n"
        "• 💸 <b>سحب</b>: اسحب أرباحك إلى محفظتك أو بطاقتك.\n"
        "• 🚀 <b>الباقات</b>: استثمر رأس مالك واحصل على أرباح ثابتة.\n\n"
        f"لأي مشكلة تواصل مع الدعم: {SUPPORT_CONTACT}",
        reply_markup=main_menu_keyboard(update.effective_user.id == ADMIN_ID),
        parse_mode=ParseMode.HTML,
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("deposit", None)
    context.user_data.pop("withdraw", None)
    await update.effective_chat.send_message(
        "❌ تم إلغاء العملية الحالية.\n\nيمكنك البدء من جديد عبر الأزرار أدناه 👇",
        reply_markup=main_menu_keyboard(update.effective_user.id == ADMIN_ID),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def ui_go_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("deposit", None)
    context.user_data.pop("withdraw", None)
    user = update.effective_user
    await safe_edit(
        query,
        WELCOME_TEXT.format(name=escape(user.first_name or str(user.id))),
        main_menu_keyboard(user.id == ADMIN_ID),
    )
    return ConversationHandler.END


async def ui_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("تم الإلغاء ❌")
    context.user_data.pop("deposit", None)
    context.user_data.pop("withdraw", None)
    await safe_edit(
        query,
        "❌ تم إلغاء العملية الحالية.\n\nاختر ما تريد من الأزرار أدناه:",
        main_menu_keyboard(update.effective_user.id == ADMIN_ID),
    )
    return ConversationHandler.END


async def show_packages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await safe_edit(query, packages_text(), packages_keyboard())
    return ConversationHandler.END


async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    balance = await db.get_balance(user.id)
    stats = await db.user_stats(user.id)

    text = (
        "📊 <b>حسابي</b>\n\n"
        f"👤 الاسم: {escape(user.full_name or '—')}\n"
        f"🔗 اليوزر: {('@' + user.username) if user.username else '—'}\n"
        f"🆔 المعرّف: <code>{user.id}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 الرصيد الحالي: <b>{balance:,} IQD</b>\n"
        f"≈ <b>${balance / USD_TO_IQD:,.2f}</b>\n\n"
        f"📥 إجمالي الإيداعات المعتمدة: {stats['deposits']:,} IQD\n"
        f"📤 إجمالي السحوبات المدفوعة: {stats['withdrawals']:,} IQD\n"
        f"⏳ طلبات قيد المراجعة: {stats['pending']}\n"
    )
    await safe_edit(query, text, main_menu_keyboard(user.id == ADMIN_ID))
    return ConversationHandler.END


async def global_unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None or update.effective_user is None:
        return
    await update.effective_message.reply_text(
        "🤔 لم أفهم هذه الرسالة.\n\n"
        "استخدم الأزرار المتاحة، أو أرسل /start للعودة إلى القائمة الرئيسية.",
        reply_markup=main_menu_keyboard(update.effective_user.id == ADMIN_ID),
        parse_mode=ParseMode.HTML,
    )


# =============================================================================
#  7) DEPOSIT WORKFLOW
# =============================================================================
async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["deposit"] = {}
    await safe_edit(
        query,
        "💳 <b>إيداع رصيد جديد</b>\n\nاختر وسيلة الإيداع التي تناسبك 👇",
        deposit_methods_keyboard(),
    )
    return S.DEPOSIT_METHOD


async def pkg_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 2)[2]
    pkg = PACKAGES.get(key)
    if pkg is None:
        await query.answer("⚠️ هذه الباقة غير متاحة حالياً.", show_alert=True)
        return ConversationHandler.END

    amount_iqd = pkg["capital"] * USD_TO_IQD
    context.user_data["deposit"] = {"prefill_iqd": amount_iqd, "package": pkg["name"]}

    await safe_edit(
        query,
        f"🛒 <b>اخترت {pkg['emoji']} {pkg['name']}</b>\n\n"
        f"💵 رأس المال: <b>{pkg['capital']}$</b> ≈ <b>{amount_iqd:,} IQD</b>\n"
        f"📈 الربح المتوقع: <b>{pkg['profit']}$</b>\n"
        f"⏳ المدة: {pkg['duration']}\n\n"
        "لإتمام الشراء، اختر وسيلة الإيداع 👇",
        deposit_methods_keyboard(),
    )
    return S.DEPOSIT_METHOD


async def on_deposit_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    method = query.data.split(":")[2]
    info = METHOD_INFO[method]
    draft = context.user_data.setdefault("deposit", {})
    draft["method"] = method

    header = f"{info['title']}\n\n{info['instructions']}\n"
    prefill: Optional[int] = draft.pop("prefill_iqd", None)
    if prefill:
        draft["amount"] = prefill
        await safe_edit(
            query,
            f"{header}\n💰 المبلغ: <b>{prefill:,} IQD</b>\n\n{info['proof_prompt']}",
            flow_keyboard(),
        )
        return S.DEPOSIT_PROOF

    await safe_edit(
        query,
        f"{header}\n💰 <b>حدّد المبلغ</b>\n\n"
        "✍️ أرسل المبلغ بالدينار العراقي (IQD) كرقم فقط، مثال: <code>25000</code>\n\n"
        f"🔹 الحد الأدنى للإيداع: <b>{MIN_DEPOSIT_IQD:,} IQD</b>",
        flow_keyboard(),
    )
    return S.DEPOSIT_AMOUNT


async def on_deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount = parse_amount(update.effective_message.text)
    if amount is None or amount < MIN_DEPOSIT_IQD:
        await update.effective_message.reply_text(
            "⚠️ <b>المبلغ غير صالح.</b>\n\n"
            f"أرسل رقماً لا يقل عن <b>{MIN_DEPOSIT_IQD:,} IQD</b>.\n"
            "مثال: <code>25000</code>",
            reply_markup=flow_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return S.DEPOSIT_AMOUNT

    draft = context.user_data.setdefault("deposit", {})
    draft["amount"] = amount
    info = METHOD_INFO[draft["method"]]

    await update.effective_message.reply_text(
        f"✅ المبلغ المحدد: <b>{amount:,} IQD</b> (≈ ${amount / USD_TO_IQD:,.2f})\n\n"
        f"{info['proof_prompt']}",
        reply_markup=flow_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return S.DEPOSIT_PROOF


async def on_deposit_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    draft = context.user_data.setdefault("deposit", {})
    method = draft.get("method")
    if method is None:
        await message.reply_text(
            "⚠️ انتهت الجلسة. الرجاء البدء من جديد.",
            reply_markup=main_menu_keyboard(update.effective_user.id == ADMIN_ID),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    if message.photo:
        draft["proof_file_id"] = message.photo[-1].file_id
        caption = (message.caption or "").strip()

        if method == "asiacell" and not caption:
            await message.reply_text(
                "✅ تم استلام صورة الكارت.\n\nالآن أرسل <b>رقم الكارت</b> في رسالة نصية.",
                reply_markup=flow_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return S.DEPOSIT_PROOF

        draft["proof_text"] = caption
        return await _submit_deposit(update, context)

    if message.text:
        text = message.text.strip()
        if method == "asiacell":
            if not draft.get("proof_file_id"):
                await message.reply_text(
                    "⚠️ نحتاج أولاً إلى <b>صورة الكارت</b>، وبعدها أرسل رقم الكارت.",
                    reply_markup=flow_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
                return S.DEPOSIT_PROOF
            draft["proof_text"] = text
            return await _submit_deposit(update, context)

        await message.reply_text(
            "⚠️ الرجاء إرسال <b>صورة</b> الإشعار، لا نصاً.",
            reply_markup=flow_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return S.DEPOSIT_PROOF

    await message.reply_text(
        "⚠️ نوع الملف غير مدعوم. أرسل صورة الإشعار.",
        reply_markup=flow_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return S.DEPOSIT_PROOF


def _admin_deposit_caption(user, request_id: int, method_label: str, amount: int, proof_text: Optional[str]) -> str:
    lines = [
        "🆕 <b>طلب إيداع جديد</b>",
        "",
        f"🆔 رقم الطلب: <code>#{request_id}</code>",
        f"👤 الاسم: {escape(user.full_name or '—')}",
        f"🔗 اليوزر: {('@' + user.username) if user.username else '—'}",
        f"🆔 المعرّف: <code>{user.id}</code>",
        f"💳 الوسيلة: {escape(method_label)}",
        f"💰 المبلغ: <b>{amount:,} IQD</b> (≈ ${amount / USD_TO_IQD:,.2f})",
    ]
    if proof_text:
        lines.append(f"📝 تفاصيل: <code>{escape(proof_text)}</code>")
    lines.append("")
    lines.append(f"📅 {datetime.now():%Y-%m-%d %H:%M}")
    return "\n".join(lines)


async def _submit_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    user = update.effective_user
    draft = context.user_data.get("deposit", {})

    method = draft.get("method")
    amount = int(draft.get("amount", 0))
    proof_file_id = draft.get("proof_file_id")
    proof_text = draft.get("proof_text")
    info = METHOD_INFO[method]

    request_id = await db.create_request(
        user_id=user.id, kind="deposit", method=method,
        amount=amount, details=proof_text, proof_file_id=proof_file_id,
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ موافقة", callback_data=f"adm:dep:ok:{request_id}"),
        InlineKeyboardButton("❌ رفض",    callback_data=f"adm:dep:no:{request_id}"),
    ]])
    caption = _admin_deposit_caption(user, request_id, info["button"], amount, proof_text)

    try:
        if proof_file_id:
            await context.bot.send_photo(
                chat_id=ADMIN_ID, photo=proof_file_id, caption=caption,
                reply_markup=keyboard, parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=ADMIN_ID, text=caption,
                reply_markup=keyboard, parse_mode=ParseMode.HTML,
            )
    except TelegramError:
        logger.exception("Could not forward deposit #%s to the admin", request_id)

    await message.reply_text(
        "✅ <b>تم استلام طلب الإيداع بنجاح</b>\n\n"
        f"🆔 رقم الطلب: <code>#{request_id}</code>\n"
        f"💳 الوسيلة: {info['button']}\n"
        f"💰 المبلغ: <b>{amount:,} IQD</b>\n"
        "⏳ الحالة: قيد المراجعة\n\n"
        "سيتم إشعارك فور اعتماد الطلب. شكراً لثقتك بمتجر دراهمكو 💎",
        reply_markup=main_menu_keyboard(user.id == ADMIN_ID),
        parse_mode=ParseMode.HTML,
    )

    context.user_data.pop("deposit", None)
    return ConversationHandler.END


# =============================================================================
#  8) WITHDRAWAL WORKFLOW
# =============================================================================
async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["withdraw"] = {}
    await safe_edit(
        query,
        "💸 <b>سحب الأرباح</b>\n\nاختر وسيلة استلام الأموال 👇",
        withdraw_methods_keyboard(),
    )
    return S.WITHDRAW_METHOD


async def on_withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    method = query.data.split(":")[2]
    info = WITHDRAW_METHODS[method]
    draft = context.user_data.setdefault("withdraw", {})
    draft["method"] = method

    balance = await db.get_balance(update.effective_user.id)
    await safe_edit(
        query,
        f"{info['button']} <b>سحب عبر {escape(info['label'])}</b>\n\n"
        f"💰 رصيدك المتاح: <b>{balance:,} IQD</b>\n\n"
        "✍️ أرسل المبلغ الذي تريد سحبه بالدينار العراقي كرقم فقط، مثال: <code>15000</code>\n\n"
        f"🔹 الحد الأدنى للسحب: <b>{MIN_WITHDRAW_IQD:,} IQD</b>",
        flow_keyboard(),
    )
    return S.WITHDRAW_AMOUNT


async def on_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount = parse_amount(update.effective_message.text)
    user = update.effective_user

    if amount is None or amount < MIN_WITHDRAW_IQD:
        await update.effective_message.reply_text(
            "⚠️ <b>المبلغ غير صالح.</b>\n\n"
            f"أرسل رقماً لا يقل عن <b>{MIN_WITHDRAW_IQD:,} IQD</b>.",
            reply_markup=flow_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return S.WITHDRAW_AMOUNT

    balance = await db.get_balance(user.id)
    if amount > balance:
        await update.effective_message.reply_text(
            "❌ <b>رصيدك غير كافٍ</b>\n\n"
            f"رصيدك الحالي: <b>{balance:,} IQD</b>\n"
            f"المبلغ المطلوب: <b>{amount:,} IQD</b>\n\n"
            "أرسل مبلغاً أصغر أو أودع رصيداً أولاً.",
            reply_markup=flow_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return S.WITHDRAW_AMOUNT

    draft = context.user_data.setdefault("withdraw", {})
    draft["amount"] = amount
    info = WITHDRAW_METHODS[draft["method"]]

    await update.effective_message.reply_text(
        f"✅ المبلغ المحدد: <b>{amount:,} IQD</b>\n\n{info['prompt']}",
        reply_markup=flow_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return S.WITHDRAW_DETAILS


async def on_withdraw_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    details = (update.effective_message.text or "").strip()
    if len(re.sub(r"\D", "", details)) < 6:
        await update.effective_message.reply_text(
            "⚠️ الرجاء إرسال تفاصيل استلام صحيحة (رقم محفظة أو رقم بطاقة).",
            reply_markup=flow_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return S.WITHDRAW_DETAILS

    draft = context.user_data.setdefault("withdraw", {})
    draft["details"] = details
    return await _submit_withdrawal(update, context)


def _admin_withdraw_caption(user, request_id: int, method_label: str, amount: int, details: str) -> str:
    return "\n".join([
        "🆕 <b>طلب سحب جديد</b>",
        "",
        f"🆔 رقم الطلب: <code>#{request_id}</code>",
        f"👤 الاسم: {escape(user.full_name or '—')}",
        f"🔗 اليوزر: {('@' + user.username) if user.username else '—'}",
        f"🆔 المعرّف: <code>{user.id}</code>",
        f"💳 وسيلة الاستلام: {escape(method_label)}",
        f"💰 المبلغ: <b>{amount:,} IQD</b> (≈ ${amount / USD_TO_IQD:,.2f})",
        f"📮 تفاصيل الاستلام: <code>{escape(details)}</code>",
        "",
        f"📅 {datetime.now():%Y-%m-%d %H:%M}",
    ])


async def _submit_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    draft = context.user_data.get("withdraw", {})
    method = draft.get("method")
    amount = int(draft.get("amount", 0))
    details = draft.get("details", "")
    info = WITHDRAW_METHODS[method]

    balance = await db.get_balance(user.id)
    if amount > balance:
        await update.effective_message.reply_text(
            "❌ <b>تغيّر رصيدك ولم يعد كافياً.</b>\n\n"
            f"رصيدك الحالي: <b>{balance:,} IQD</b>",
            reply_markup=main_menu_keyboard(user.id == ADMIN_ID),
            parse_mode=ParseMode.HTML,
        )
        context.user_data.pop("withdraw", None)
        return ConversationHandler.END

    await db.adjust_balance(user.id, -amount)  # تجميد المبلغ

    request_id = await db.create_request(
        user_id=user.id, kind="withdraw", method=method,
        amount=amount, details=details,
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ موافقة ودفع", callback_data=f"adm:wdr:ok:{request_id}"),
        InlineKeyboardButton("❌ رفض",         callback_data=f"adm:wdr:no:{request_id}"),
    ]])

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=_admin_withdraw_caption(user, request_id, info["label"], amount, details),
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        logger.exception("Could not forward withdrawal #%s", request_id)
        await db.adjust_balance(user.id, amount)  # إرجاع المبلغ
        await update.effective_message.reply_text(
            "⚠️ حدث خطأ مؤقت. الرجاء المحاولة مرة أخرى.",
            reply_markup=main_menu_keyboard(user.id == ADMIN_ID),
            parse_mode=ParseMode.HTML,
        )
        context.user_data.pop("withdraw", None)
        return ConversationHandler.END

    await update.effective_message.reply_text(
        "✅ <b>تم إرسال طلب السحب بنجاح</b>\n\n"
        f"🆔 رقم الطلب: <code>#{request_id}</code>\n"
        f"💳 الوسيلة: {info['button']}\n"
        f"💰 المبلغ: <b>{amount:,} IQD</b>\n"
        "⏳ الحالة: قيد المراجعة\n\n"
        "🔒 تم تجميد المبلغ من رصيدك لحين التنفيذ.",
        reply_markup=main_menu_keyboard(user.id == ADMIN_ID),
        parse_mode=ParseMode.HTML,
    )

    context.user_data.pop("withdraw", None)
    return ConversationHandler.END


# =============================================================================
#  9) ADMIN ACTIONS
# =============================================================================
def _is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update):
        await query.answer("⛔ هذه اللوحة مخصصة للإدارة فقط.", show_alert=True)
        return
    await query.answer()

    deposits = await db.count_pending("deposit")
    withdrawals = await db.count_pending("withdraw")

    await safe_edit(
        query,
        "🛠 <b>لوحة تحكم الإدارة — دراهمكو</b>\n\n"
        f"📥 طلبات إيداع معلقة: <b>{deposits}</b>\n"
        f"📤 طلبات سحب معلقة: <b>{withdrawals}</b>\n\n"
        "اختر ما تريد إدارته:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📥 الإيداعات ({deposits})", callback_data="adm:list:deposit")],
            [InlineKeyboardButton(f"📤 السحوبات ({withdrawals})", callback_data="adm:list:withdraw")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="ui:home")],
        ]),
    )


async def admin_list_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update):
        await query.answer("⛔", show_alert=True)
        return
    await query.answer()

    kind = query.data.split(":")[2]
    rows = await db.list_pending(kind)

    if not rows:
        await safe_edit(
            query, "✅ لا توجد طلبات معلقة حالياً.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="adm:panel")]]),
        )
        return

    lines = [f"📋 <b>الطلبات المعلقة ({kind})</b>", ""]
    for row in rows:
        who = escape(row.get("full_name") or f"ID {row['user_id']}")
        lines.append(
            f"🆔 <code>#{row['id']}</code> | {who}\n"
            f"   💰 {row['amount']:,} IQD — {row['method']}\n"
            f"   📅 {row['created_at'][:16].replace('T', ' ')}\n"
        )
    lines.append("ℹ️ استخدم أزرار الطلب في الرسالة المُحوَّلة للموافقة أو الرفض.")

    await safe_edit(
        query, "\n".join(lines),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="adm:panel")]]),
    )


async def _stamp_admin_message(query, note: str) -> None:
    try:
        if query.message.photo:
            original = query.message.caption or ""
            await query.edit_message_caption(
                caption=f"{note}\n\n{original}", parse_mode=ParseMode.HTML, reply_markup=None,
            )
        else:
            original = query.message.text or ""
            await query.edit_message_text(
                text=f"{original}\n\n{note}", parse_mode=ParseMode.HTML, reply_markup=None,
            )
    except TelegramError:
        logger.warning("Could not stamp admin message", exc_info=True)


async def admin_deposit_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update):
        await query.answer("⛔", show_alert=True)
        return

    request_id = int(query.data.split(":")[3])
    request = await db.get_request(request_id)

    if request is None or request["status"] != "pending":
        await query.answer("⚠️ تمت معالجة هذا الطلب مسبقاً.", show_alert=True)
        await _stamp_admin_message(query, "ℹ️ الطلب مُعالج مسبقاً.")
        return

    await query.answer("تمت الموافقة ✅")
    await db.set_request_status(request_id, "approved")
    new_balance = await db.adjust_balance(request["user_id"], int(request["amount"]))

    try:
        await context.bot.send_message(
            chat_id=request["user_id"],
            text=(
                "✅ <b>تمت الموافقة على طلب الإيداع</b>\n\n"
                f"🆔 رقم الطلب: <code>#{request_id}</code>\n"
                f"💰 المبلغ المضاف: <b>{int(request['amount']):,} IQD</b>\n"
                f"💼 رصيدك الجديد: <b>{new_balance:,} IQD</b>\n\n"
                "تم إضافة المبلغ إلى رصيدك بنجاح. نتمنى لك أرباحاً وفيرة 💎"
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        logger.warning("Could not notify user %s", request["user_id"], exc_info=True)

    await _stamp_admin_message(
        query, f"✅ <b>تمت الموافقة</b> — أُضيف {int(request['amount']):,} IQD للرصيد."
    )


async def admin_deposit_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update):
        await query.answer("⛔", show_alert=True)
        return

    request_id = int(query.data.split(":")[3])
    request = await db.get_request(request_id)

    if request is None or request["status"] != "pending":
        await query.answer("⚠️ تمت معالجة هذا الطلب مسبقاً.", show_alert=True)
        await _stamp_admin_message(query, "ℹ️ الطلب مُعالج مسبقاً.")
        return

    await query.answer("تم الرفض ❌")
    await db.set_request_status(request_id, "rejected")

    try:
        await context.bot.send_message(
            chat_id=request["user_id"],
            text=(
                "❌ <b>تم رفض طلب الإيداع</b>\n\n"
                f"🆔 رقم الطلب: <code>#{request_id}</code>\n\n"
                f"يرجى التواصل مع الدعم الفني: {SUPPORT_CONTACT}"
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        logger.warning("Could not notify user %s", request["user_id"], exc_info=True)

    await _stamp_admin_message(query, "❌ <b>تم رفض الطلب.</b>")


async def admin_withdraw_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update):
        await query.answer("⛔", show_alert=True)
        return

    request_id = int(query.data.split(":")[3])
    request = await db.get_request(request_id)

    if request is None or request["status"] != "pending":
        await query.answer("⚠️ تمت معالجة هذا الطلب مسبقاً.", show_alert=True)
        await _stamp_admin_message(query, "ℹ️ الطلب مُعالج مسبقاً.")
        return

    await db.set_request_status(request_id, "awaiting_proof")
    context.bot_data.setdefault("pending_payout", {})[ADMIN_ID] = request_id

    await query.answer("أرسل صورة إثبات الدفع 📷", show_alert=True)
    await _stamp_admin_message(
        query, f"⏳ <b>بانتظار إثبات الدفع</b> للطلب <code>#{request_id}</code>."
    )
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            "📷 <b>أرسل الآن صورة إثبات الدفع (Screenshot)</b>\n\n"
            f"🆔 رقم الطلب: <code>#{request_id}</code>\n"
            f"💰 المبلغ: <b>{int(request['amount']):,} IQD</b>\n"
            f"📮 إلى: <code>{escape(request.get('details') or '')}</code>\n\n"
            "سيتم إرسال الصورة تلقائياً إلى المستخدم بمجرد استلامها."
        ),
        parse_mode=ParseMode.HTML,
    )


async def admin_withdraw_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update):
        await query.answer("⛔", show_alert=True)
        return

    request_id = int(query.data.split(":")[3])
    request = await db.get_request(request_id)

    if request is None or request["status"] != "pending":
        await query.answer("⚠️ تمت معالجة هذا الطلب مسبقاً.", show_alert=True)
        await _stamp_admin_message(query, "ℹ️ الطلب مُعالج مسبقاً.")
        return

    await query.answer("تم الرفض ❌")
    await db.set_request_status(request_id, "rejected")
    refunded = await db.adjust_balance(request["user_id"], int(request["amount"]))

    try:
        await context.bot.send_message(
            chat_id=request["user_id"],
            text=(
                "❌ <b>تم رفض طلب السحب</b>\n\n"
                f"🆔 رقم الطلب: <code>#{request_id}</code>\n"
                "↩️ تم إرجاع المبلغ إلى رصيدك.\n"
                f"💼 رصيدك الحالي: <b>{refunded:,} IQD</b>\n\n"
                f"للاستفسار تواصل مع الدعم: {SUPPORT_CONTACT}"
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        logger.warning("Could not notify user %s", request["user_id"], exc_info=True)

    await _stamp_admin_message(query, "❌ <b>تم رفض الطلب وإرجاع المبلغ للمستخدم.</b>")


async def admin_payout_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """الأدمن يرسل صورة إثبات الدفع بعد الضغط على «موافقة ودفع»."""
    message = update.effective_message
    if message is None:
        return

    pending: dict[int, int] = context.bot_data.setdefault("pending_payout", {})
    request_id = pending.pop(ADMIN_ID, None)

    if request_id is None:
        await message.reply_text(
            "ℹ️ لا يوجد طلب سحب بانتظار إثبات الدفع حالياً.\n"
            "اضغط «✅ موافقة ودفع» على الطلب أولاً."
        )
        return

    request = await db.get_request(request_id)
    if request is None:
        await message.reply_text("⚠️ لم يتم العثور على الطلب في قاعدة البيانات.")
        return

    photo = message.photo[-1]
    await db.set_request_status(request_id, "paid")

    try:
        await context.bot.send_photo(
            chat_id=request["user_id"],
            photo=photo.file_id,
            caption=(
                "💸 <b>تم تحويل أموالك بنجاح!</b>\n\n"
                f"🆔 رقم الطلب: <code>#{request_id}</code>\n"
                f"💰 المبلغ: <b>{int(request['amount']):,} IQD</b>\n\n"
                "نشكرك على استخدامك متجر <b>دراهمكو</b>. شكراً لاستخدامك Drahimco."
            ),
            parse_mode=ParseMode.HTML,
        )
        await message.reply_text(
            f"✅ تم إرسال إثبات الدفع للطلب <code>#{request_id}</code> بنجاح.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        logger.exception("Could not deliver payout proof for #%s", request_id)
        await message.reply_text("⚠️ فشل إرسال الصورة للمستخدم.")


# =============================================================================
#  10) APPLICATION WIRING
# =============================================================================
def _menu_state_handlers() -> list[BaseHandler]:
    """أزرار تعمل في أي خطوة — والعودة منها تُغلق الـ FSM."""
    return [
        CommandHandler("start", cmd_start),
        CommandHandler("cancel", cmd_cancel),
        CallbackQueryHandler(ui_go_home, pattern=r"^ui:home$"),
        CallbackQueryHandler(ui_cancel, pattern=r"^ui:cancel$"),
        CallbackQueryHandler(show_packages, pattern=r"^pkg:list$"),
        CallbackQueryHandler(pkg_buy, pattern=r"^pkg:buy:\d+$"),
        CallbackQueryHandler(show_account, pattern=r"^acc:view$"),
        CallbackQueryHandler(deposit_start, pattern=r"^dep:start$"),
        CallbackQueryHandler(withdraw_start, pattern=r"^wdr:start$"),
    ]


def build_conversation_handler() -> ConversationHandler:
    entry_points = [
        CommandHandler("start", cmd_start),
        CallbackQueryHandler(deposit_start, pattern=r"^dep:start$"),
        CallbackQueryHandler(withdraw_start, pattern=r"^wdr:start$"),
        CallbackQueryHandler(pkg_buy, pattern=r"^pkg:buy:\d+$"),
    ]

    states = {
        S.DEPOSIT_METHOD: _menu_state_handlers() + [
            CallbackQueryHandler(on_deposit_method, pattern=r"^dep:m:(asiacell|zaincash|mastercard)$"),
        ],
        S.DEPOSIT_AMOUNT: _menu_state_handlers() + [
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_deposit_amount),
        ],
        S.DEPOSIT_PROOF: _menu_state_handlers() + [
            MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), on_deposit_proof),
        ],
        S.WITHDRAW_METHOD: _menu_state_handlers() + [
            CallbackQueryHandler(on_withdraw_method, pattern=r"^wdr:m:(zaincash|mastercard)$"),
        ],
        S.WITHDRAW_AMOUNT: _menu_state_handlers() + [
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_withdraw_amount),
        ],
        S.WITHDRAW_DETAILS: _menu_state_handlers() + [
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_withdraw_details),
        ],
    }

    fallbacks = [
        CommandHandler("cancel", cmd_cancel),
        CommandHandler("start", cmd_start),
    ]

    return ConversationHandler(
        entry_points=entry_points,
        states=states,
        fallbacks=fallbacks,
        conversation_timeout=CONVERSATION_TIMEOUT,
        allow_reentry=True,
        name="drahimco_main",
    )


async def post_init(application: Application) -> None:
    await db.init()
    await application.bot.set_my_commands([
        ("start",  "🏠 القائمة الرئيسية"),
        ("cancel", "❌ إلغاء العملية الحالية"),
        ("help",   "ℹ️ المساعدة والدعم"),
    ])
    logger.info("Drahimco bot is up. Admin ID = %s", ADMIN_ID)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat is not None:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ حدث خطأ غير متوقع. الرجاء إعادة المحاولة أو إرسال /start.",
            )
        except TelegramError:
            pass


def build_application() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # ---- Group 0: FSM ------------------------------------------------------
    application.add_handler(build_conversation_handler())

    # ---- Group 1: handlers خارج الـ FSM ------------------------------------
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CallbackQueryHandler(show_packages, pattern=r"^pkg:list$"))
    application.add_handler(CallbackQueryHandler(pkg_buy,       pattern=r"^pkg:buy:\d+$"))
    application.add_handler(CallbackQueryHandler(show_account,  pattern=r"^acc:view$"))
    application.add_handler(CallbackQueryHandler(ui_go_home,    pattern=r"^ui:home$"))
    application.add_handler(CallbackQueryHandler(ui_cancel,     pattern=r"^ui:cancel$"))

    # لوحة الأدمن
    application.add_handler(CallbackQueryHandler(admin_panel,           pattern=r"^adm:panel$"))
    application.add_handler(CallbackQueryHandler(admin_list_pending,    pattern=r"^adm:list:(deposit|withdraw)$"))
    application.add_handler(CallbackQueryHandler(admin_deposit_approve, pattern=r"^adm:dep:ok:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_deposit_reject,  pattern=r"^adm:dep:no:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_withdraw_approve,pattern=r"^adm:wdr:ok:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_withdraw_reject, pattern=r"^adm:wdr:no:\d+$"))

    # إثبات الدفع من الأدمن (صورة)
    application.add_handler(MessageHandler(filters.PHOTO & filters.User(ADMIN_ID), admin_payout_proof))

    # catch-all
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, global_unknown_message))

    application.add_error_handler(on_error)
    return application


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "ضع_التوكن_هنا":
        raise SystemExit(
            "❌ لم تضف التوكن بعد!\n"
            "   افتح الملف وعدّل السطر:\n"
            '   BOT_TOKEN = "123456:ABC-DEF..."'
        )

    application = build_application()
    logger.info("Starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
