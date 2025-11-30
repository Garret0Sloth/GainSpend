import os
import logging
from datetime import datetime, date

import psycopg
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# -------------------------------------------------------------
# ЛОГИРОВАНИЕ
# -------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------------------------------------------------
# СОСТОЯНИЯ ДИАЛОГА
# -------------------------------------------------------------
(
    INCOME_AMOUNT,
    INCOME_DESC,
    EXPENSE_CATEGORY,
    EXPENSE_AMOUNT,
    EXPENSE_DESC,
    STATS_PERIOD,
    STATS_CUSTOM_MONTH,
) = range(7)

# -------------------------------------------------------------
# НАСТРОЙКИ ДОСТУПА
# -------------------------------------------------------------
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # твой Telegram ID

# -------------------------------------------------------------
# КАТЕГОРИИ И ЭМОДЗИ
# -------------------------------------------------------------
EXPENSE_CATEGORIES = ["Еда", "Дом", "Коммуналка", "Досуг", "НЗ"]

CATEGORY_EMOJI = {
    "Еда": "🍽️",
    "Дом": "🏠",
    "Коммуналка": "💡",
    "Досуг": "🎉",
    "НЗ": "📦",
}

# -------------------------------------------------------------
# ПОДКЛЮЧЕНИЕ К БАЗЕ POSTGRESQL (Railway)
# -------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")

def get_conn():
    return psycopg.connect(DATABASE_URL, sslmode="require")


# -------------------------------------------------------------
# СОЗДАНИЕ ТАБЛИЦ
# -------------------------------------------------------------
def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # Таблица с доходами/расходами
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS records (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            type TEXT NOT NULL,
            category TEXT,
            amount NUMERIC(12,2) NOT NULL,
            description TEXT,
            created_at TIMESTAMPTZ NOT NULL
        )
        """
    )

    # Таблица с разрешёнными пользователями
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS allowed_users (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    conn.commit()
    conn.close()


# -------------------------------------------------------------
# РАБОТА С БАЗОЙ
# -------------------------------------------------------------
def add_record(user_id, type_, amount, description, category=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO records (user_id, type, category, amount, description, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (user_id, type_, category, amount, description, datetime.utcnow()),
    )
    conn.commit()
    conn.close()


def is_user_allowed(user_id: int) -> bool:
    if OWNER_ID and user_id == OWNER_ID:
        return True

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM allowed_users WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def add_allowed_user(user_id, username, first_name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO allowed_users (user_id, username, first_name)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE
        SET username = EXCLUDED.username,
            first_name = EXCLUDED.first_name
        """,
        (user_id, username, first_name),
    )
    conn.commit()
    conn.close()


def remove_allowed_user(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM allowed_users WHERE user_id = %s", (user_id,))
    conn.commit()
    conn.close()


def get_stats(user_id, date_from=None, date_to=None):
    conn = get_conn()
    cur = conn.cursor()

    params = [user_id]
    where = ["user_id = %s"]

    if date_from:
        where.append("created_at >= %s")
        params.append(date_from)
    if date_to:
        where.append("created_at < %s")
        params.append(date_to)

    where_clause = " AND ".join(where)

    # Доход/расход
    cur.execute(
        f"""
        SELECT type, SUM(amount)
        FROM records
        WHERE {where_clause}
        GROUP BY type
        """,
        params,
    )
    sums = {row[0]: float(row[1]) for row in cur.fetchall()}

    # Категории
    cur.execute(
        f"""
        SELECT category, SUM(amount)
        FROM records
        WHERE {where_clause} AND type = 'expense'
        GROUP BY category
        """,
        params,
    )
    categories = {row[0]: float(row[1]) for row in cur.fetchall()}

    conn.close()
    return sums, categories


# -------------------------------------------------------------
# ПРОВЕРКА ДОСТУПА
# -------------------------------------------------------------
async def ensure_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False

    user_id = user.id

    if user_id == OWNER_ID:
        return True

    if is_user_allowed(user_id):
        return True

    await update.message.reply_text("❌ У вас нет доступа к этому боту.")

    # Уведомление владельца
    if OWNER_ID:
        try:
            username = f"@{user.username}" if user.username else "(нет username)"
            await context.bot.send_message(
                OWNER_ID,
                f"🚪 Запрос доступа:\nID: {user_id}\nИмя: {user.first_name}\nUsername: {username}",
            )
        except:
            pass

    return False


# -------------------------------------------------------------
# ХЕЛПЕРЫ
# -------------------------------------------------------------
def build_category_keyboard():
    rows = []
    for cat in EXPENSE_CATEGORIES:
        rows.append([f"{CATEGORY_EMOJI[cat]} {cat}"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def extract_category(text: str) -> str | None:
    for cat in EXPENSE_CATEGORIES:
        if cat in text:
            return cat
    return None


def parse_month(text: str):
    # Формат ММ-ГГ
    try:
        mm, yy = text.split("-")
        mm = int(mm)
        yy = int(yy)
        if not 1 <= mm <= 12:
            raise ValueError
        year = 2000 + yy
        return year, mm
    except:
        raise ValueError("Неверный формат")


# -------------------------------------------------------------
# ХЕНДЛЕРЫ
# -------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_authorized(update, context):
        return

    kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
    await update.message.reply_text(
        "Привет! Я бот для учёта доходов и расходов.",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )


# ДОХОД
async def income_start(update, context):
    if not await ensure_authorized(update, context):
        return
    await update.message.reply_text("Введи сумму дохода:", reply_markup=ReplyKeyboardRemove())
    return INCOME_AMOUNT


async def income_amount(update, context):
    try:
        amount = float(update.message.text.replace(",", "."))
    except:
        await update.message.reply_text("Введи число:")
        return INCOME_AMOUNT

    context.user_data["income_amount"] = amount
    await update.message.reply_text("Введите описание дохода:")
    return INCOME_DESC


async def income_desc(update, context):
    amount = context.user_data["income_amount"]
    desc = update.message.text
    uid = update.effective_user.id

    add_record(uid, "income", amount, desc)

    kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
    await update.message.reply_text(
        f"Доход {amount:.2f} ₽ сохранён.",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )

    return ConversationHandler.END


# РАСХОД
async def expense_start(update, context):
    if not await ensure_authorized(update, context):
        return
    await update.message.reply_text("Выбери категорию:", reply_markup=build_category_keyboard())
    return EXPENSE_CATEGORY


async def expense_category(update, context):
    cat = extract_category(update.message.text)
    if not cat:
        await update.message.reply_text("Выберите категорию с кнопки.")
        return EXPENSE_CATEGORY

    context.user_data["expense_category"] = cat
    await update.message.reply_text("Введите сумму:", reply_markup=ReplyKeyboardRemove())
    return EXPENSE_AMOUNT


async def expense_amount(update, context):
    try:
        amount = float(update.message.text.replace(",", "."))
    except:
        await update.message.reply_text("Введите число:")
        return EXPENSE_AMOUNT

    context.user_data["expense_amount"] = amount
    await update.message.reply_text("Введите описание:")
    return EXPENSE_DESC


async def expense_desc(update, context):
    cat = context.user_data["expense_category"]
    amount = context.user_data["expense_amount"]
    desc = update.message.text
    uid = update.effective_user.id

    add_record(uid, "expense", amount, desc, cat)

    kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
    await update.message.reply_text(
        f"Расход {amount:.2f} ₽ сохранён.",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )

    return ConversationHandler.END


# СТАТИСТИКА
async def stats_start(update, context):
    if not await ensure_authorized(update, context):
        return

    kb = [["Текущий месяц", "Выбрать месяц"], ["За всё время"]]
    await update.message.reply_text(
        "Выберите период:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return STATS_PERIOD


async def stats_period(update, context):
    choice = update.message.text
    uid = update.effective_user.id

    if choice == "Текущий месяц":
        today = date.today()
        first = today.replace(day=1)
        nextm = date(first.year + (first.month == 12), (first.month % 12) + 1, 1)
        sums, cats = get_stats(uid, datetime.combine(first, datetime.min.time()),
                               datetime.combine(nextm, datetime.min.time()))
        await send_stats(update, sums, cats, "Текущий месяц")
        return ConversationHandler.END

    elif choice == "За всё время":
        sums, cats = get_stats(uid, None, None)
        await send_stats(update, sums, cats, "За всё время")
        return ConversationHandler.END

    elif choice == "Выбрать месяц":
        await update.message.reply_text("Введите месяц в формате ММ-ГГ (например 11-25):",
                                        reply_markup=ReplyKeyboardRemove())
        return STATS_CUSTOM_MONTH

    else:
        await update.message.reply_text("Выберите вариант с кнопки.")
        return STATS_PERIOD


async def stats_custom_month(update, context):
    try:
        year, month = parse_month(update.message.text)
    except:
        await update.message.reply_text("Формат должен быть ММ-ГГ.")
        return STATS_CUSTOM_MONTH

    uid = update.effective_user.id
    first = date(year, month, 1)
    nextm = date(first.year + (first.month == 12), (first.month % 12) + 1, 1)

    sums, cats = get_stats(
        uid,
        datetime.combine(first, datetime.min.time()),
        datetime.combine(nextm, datetime.min.time())
    )

    await send_stats(update, sums, cats, f"{month:02d}-{str(year)[-2:]}")
    return ConversationHandler.END


async def send_stats(update, sums, cats, period):
    income = sums.get("income", 0)
    expense = sums.get("expense", 0)
    balance = income - expense

    nz = cats.get("НЗ", 0)
    other = {k: v for k, v in cats.items() if k != "НЗ"}

    lines = [
        f"📊 Статистика: {period}",
        "",
        f"Доход: {income:.2f} ₽",
        f"Расход: {expense:.2f} ₽",
        f"Баланс: {balance:.2f} ₽",
    ]

    if other:
        lines.append("")
        lines.append("Расходы по категориям:")
        for c, a in other.items():
            emoji = CATEGORY_EMOJI[c]
            lines.append(f"• {emoji} {c}: {a:.2f} ₽")

    if nz:
        lines.append("")
        lines.append("НЗ (Запас):")
        lines.append(f"• {CATEGORY_EMOJI['НЗ']} НЗ: {nz:.2f} ₽")

    kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )


# -------------------------------------------------------------
# КОМАНДЫ ДОСТУПА
# -------------------------------------------------------------
async def grant(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Команда только для владельца.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /grant <user_id>")
        return

    try:
        uid = int(context.args[0])
    except:
        await update.message.reply_text("user_id должен быть числом.")
        return

    add_allowed_user(uid, None, None)
    await update.message.reply_text(f"Пользователь {uid} добавлен.")


async def revoke(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Команда только для владельца.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /revoke <user_id>")
        return

    try:
        uid = int(context.args[0])
    except:
        await update.message.reply_text("user_id должен быть числом.")
        return

    remove_allowed_user(uid)
    await update.message.reply_text(f"Пользователь {uid} удалён.")


async def myid(update, context):
    await update.message.reply_text(f"Ваш Telegram ID: `{update.effective_user.id}`",
                                    parse_mode="Markdown")


# -------------------------------------------------------------
# ОТМЕНА
# -------------------------------------------------------------
async def cancel(update, context):
    kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
    await update.message.reply_text(
        "Действие отменено.",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return ConversationHandler.END


# -------------------------------------------------------------
# MAIN
# -------------------------------------------------------------
def main():
    init_db()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не найден BOT_TOKEN")

    app = ApplicationBuilder().token(token).build()

    income_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Доход$"), income_start)],
        states={
            INCOME_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, income_amount)],
            INCOME_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, income_desc)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    expense_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➖ Расход$"), expense_start)],
        states={
            EXPENSE_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_category)],
            EXPENSE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_amount)],
            EXPENSE_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_desc)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    stats_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📊 Статистика$"), stats_start)],
        states={
            STATS_PERIOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, stats_period)],
            STATS_CUSTOM_MONTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, stats_custom_month)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CommandHandler("revoke", revoke))
    app.add_handler(CommandHandler("myid", myid))

    app.add_handler(income_conv)
    app.add_handler(expense_conv)
    app.add_handler(stats_conv)

    app.run_polling()


if __name__ == "__main__":
    main()

