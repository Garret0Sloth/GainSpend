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
# БАЗА ДАННЫХ (PostgreSQL)
# -------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("Не задана переменная окружения DATABASE_URL")
    return psycopg.connect(DATABASE_URL, sslmode="require")


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS records (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            type TEXT NOT NULL,       -- 'income' или 'expense'
            category TEXT,            -- NULL для дохода
            amount NUMERIC(12,2) NOT NULL,
            description TEXT,
            created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def add_record(
    user_id: int,
    type_: str,
    amount: float,
    description: str,
    category: str | None = None,
):
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


def get_stats(user_id: int, date_from: datetime | None, date_to: datetime | None):
    conn = get_conn()
    cur = conn.cursor()

    params: list[object] = [user_id]
    where_parts = ["user_id = %s"]

    if date_from is not None:
        where_parts.append("created_at >= %s")
        params.append(date_from)
    if date_to is not None:
        where_parts.append("created_at < %s")
        params.append(date_to)

    where_clause = " AND ".join(where_parts)

    # суммы доход/расход
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

    # расходы по категориям
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
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# -------------------------------------------------------------
def build_category_keyboard() -> ReplyKeyboardMarkup:
    rows = [[f"{CATEGORY_EMOJI[cat]} {cat}"] for cat in EXPENSE_CATEGORIES]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def extract_category(text: str) -> str | None:
    text = text.strip()
    for cat in EXPENSE_CATEGORIES:
        if cat in text:
            return cat
    return None


def parse_month_mm_yy(text: str) -> tuple[int, int]:
    """
    Формат ММ-ГГ (11-25 -> ноябрь 2025)
    """
    try:
        mm_str, yy_str = text.split("-")
        mm = int(mm_str)
        yy = int(yy_str)
        if not 1 <= mm <= 12:
            raise ValueError
        year = 2000 + yy  # считаем, что всё в 2000-х
        return year, mm
    except Exception:
        raise ValueError("Неверный формат")


def get_current_month_range() -> tuple[datetime, datetime]:
    today = date.today()
    first = today.replace(day=1)
    if first.month == 12:
        next_month = first.replace(year=first.year + 1, month=1, day=1)
    else:
        next_month = first.replace(month=first.month + 1, day=1)
    return (
        datetime.combine(first, datetime.min.time()),
        datetime.combine(next_month, datetime.min.time()),
    )


# -------------------------------------------------------------
# ХЭНДЛЕРЫ
# -------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
    await update.message.reply_text(
        "Привет! Я бот для учёта доходов и расходов.\n\n"
        "Используй кнопки ниже:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )


# ---------- ДОХОД ----------
async def income_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введи сумму дохода (например: 1500.50):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return INCOME_AMOUNT


async def income_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Некорректная сумма. Введи положительное число:")
        return INCOME_AMOUNT

    context.user_data["income_amount"] = amount
    await update.message.reply_text("За что ты получил этот доход? (описание)")
    return INCOME_DESC


async def income_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    amount = context.user_data.get("income_amount")
    user_id = update.effective_user.id

    add_record(user_id, "income", amount, desc)

    kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
    await update.message.reply_text(
        f"✅ Доход {amount:.2f} ₽ сохранён.\nОписание: {desc}",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    context.user_data.pop("income_amount", None)
    return ConversationHandler.END


# ---------- РАСХОД ----------
async def expense_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Выбери категорию расхода:",
        reply_markup=build_category_keyboard(),
    )
    return EXPENSE_CATEGORY


async def expense_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = extract_category(update.message.text)
    if cat is None:
        await update.message.reply_text("Пожалуйста, выбери категорию с клавиатуры.")
        return EXPENSE_CATEGORY

    context.user_data["expense_category"] = cat
    await update.message.reply_text(
        f"Категория: {CATEGORY_EMOJI[cat]} {cat}\nТеперь введи сумму расхода:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return EXPENSE_AMOUNT


async def expense_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Некорректная сумма. Введи положительное число:")
        return EXPENSE_AMOUNT

    context.user_data["expense_amount"] = amount
    await update.message.reply_text(
        "Напиши комментарий: за что потратил?\nНапример: продукты, аренда, кино..."
    )
    return EXPENSE_DESC


async def expense_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    amount = context.user_data.get("expense_amount")
    category = context.user_data.get("expense_category")
    user_id = update.effective_user.id

    add_record(user_id, "expense", amount, desc, category)

    kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
    await update.message.reply_text(
        f"✅ Расход {amount:.2f} ₽ сохранён.\n"
        f"Категория: {CATEGORY_EMOJI[category]} {category}\n"
        f"Комментарий: {desc}",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )

    context.user_data.pop("expense_amount", None)
    context.user_data.pop("expense_category", None)
    return ConversationHandler.END


# ---------- СТАТИСТИКА ----------
async def stats_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["Текущий месяц", "Выбрать месяц"], ["За всё время"]]
    await update.message.reply_text(
        "За какой период показать статистику?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    return STATS_PERIOD


async def stats_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    user_id = update.effective_user.id

    if choice == "Текущий месяц":
        date_from, date_to = get_current_month_range()
        sums, cats = get_stats(user_id, date_from, date_to)
        await send_stats(update, sums, cats, "Текущий месяц")
        return ConversationHandler.END

    if choice == "За всё время":
        sums, cats = get_stats(user_id, None, None)
        await send_stats(update, sums, cats, "За всё время")
        return ConversationHandler.END

    if choice == "Выбрать месяц":
        await update.message.reply_text(
            "Введи месяц в формате ММ-ГГ (например 11-25):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return STATS_CUSTOM_MONTH

    await update.message.reply_text("Пожалуйста, выбери вариант с клавиатуры.")
    return STATS_PERIOD


async def stats_custom_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        year, month = parse_month_mm_yy(text)
    except ValueError:
        await update.message.reply_text("Неверный формат. Нужен ММ-ГГ, например 11-25.")
        return STATS_CUSTOM_MONTH

    user_id = update.effective_user.id
    first = date(year, month, 1)
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)

    date_from = datetime.combine(first, datetime.min.time())
    date_to = datetime.combine(next_month, datetime.min.time())

    sums, cats = get_stats(user_id, date_from, date_to)
    await send_stats(update, sums, cats, f"Месяц {month:02d}-{str(year)[-2:]}")
    return ConversationHandler.END


async def send_stats(
    update: Update,
    sums: dict,
    cats: dict,
    period_label: str,
):
    income = sums.get("income", 0.0)
    expense = sums.get("expense", 0.0)
    balance = income - expense

    nz_amount = cats.get("НЗ", 0.0)
    other_cats = {k: v for k, v in cats.items() if k != "НЗ"}

    lines: list[str] = [
        f"📊 Статистика: {period_label}",
        "",
        f"Доход: {income:.2f} ₽",
        f"Расход: {expense:.2f} ₽",
        f"Баланс: {balance:.2f} ₽",
    ]

    if other_cats:
        lines.append("")
        lines.append("Расходы по категориям:")
        for cat, amt in other_cats.items():
            emoji = CATEGORY_EMOJI.get(cat, "")
            lines.append(f"• {emoji} {cat}: {amt:.2f} ₽")

    if nz_amount:
        lines.append("")
        lines.append("НЗ (Запас):")
        lines.append(f"• {CATEGORY_EMOJI['НЗ']} НЗ: {nz_amount:.2f} ₽")

    kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )


# ---------- ОТМЕНА ----------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        raise RuntimeError("Не задан BOT_TOKEN")

    app = ApplicationBuilder().token(token).build()

    income_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Доход$"), income_start),
            CommandHandler("income", income_start),
        ],
        states={
            INCOME_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, income_amount)],
            INCOME_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, income_desc)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    expense_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➖ Расход$"), expense_start),
            CommandHandler("expense", expense_start),
        ],
        states={
            EXPENSE_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_category)],
            EXPENSE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_amount)],
            EXPENSE_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_desc)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    stats_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📊 Статистика$"), stats_start),
            CommandHandler("stats", stats_start),
        ],
        states={
            STATS_PERIOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, stats_period)],
            STATS_CUSTOM_MONTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, stats_custom_month)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(income_conv)
    app.add_handler(expense_conv)
    app.add_handler(stats_conv)

    app.run_polling()


if __name__ == "__main__":
    main()
