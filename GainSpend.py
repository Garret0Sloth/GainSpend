import logging
import os
from datetime import datetime, date

from urllib.parse import urlparse

import psycopg2
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

# ---------- ЛОГИРОВАНИЕ ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- СОСТОЯНИЯ ДИАЛОГА ----------
(
    INCOME_AMOUNT,
    INCOME_DESC,
    EXPENSE_CATEGORY,
    EXPENSE_AMOUNT,
    EXPENSE_DESC,
    STATS_PERIOD,
    STATS_CUSTOM_MONTH,
) = range(7)

# ---------- КАТЕГОРИИ И ЭМОДЗИ ----------
EXPENSE_CATEGORIES = ["Еда", "Дом", "Коммуналка", "Досуг", "НЗ"]

CATEGORY_EMOJI = {
    "Еда": "🍽️",
    "Дом": "🏠",
    "Коммуналка": "💡",
    "Досуг": "🎉",
    "НЗ": "📦",
}

# ---------- БАЗА ДАННЫХ (PostgreSQL) ----------

DATABASE_URL = os.getenv("DATABASE_URL")


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан. Укажи его в переменных окружения Railway.")
    # Railway выдаёт правильный URL, psycopg2 его понимает
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    conn = get_conn()
    cur = conn.cursor()
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
    conn.commit()
    conn.close()


def add_record(user_id: int, type_: str, amount: float, description: str = None, category: str = None):
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

    params = [user_id]
    where = ["user_id = %s"]

    if date_from is not None:
        where.append("created_at >= %s")
        params.append(date_from)
    if date_to is not None:
        where.append("created_at < %s")
        params.append(date_to)

    where_clause = " AND ".join(where)

    # Итого доход / расход
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

    # Расходы по категориям
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


# ---------- ОБРАБОТЧИКИ КОМАНД ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_firstname = update.effective_user.first_name
    main_keyboard = [
        ["➕ Доход", "➖ Расход"],
        ["📊 Статистика"],
    ]
    await update.message.reply_text(
        f"Привет, {user_firstname}!\n\n"
        "Я бот для учёта доходов и расходов.\n"
        "Выбери действие на клавиатуре.",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True),
    )
    return ConversationHandler.END


# ---------- ДОХОД ----------

async def income_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введи сумму дохода (например: 1500.50):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return INCOME_AMOUNT


async def income_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.replace(",", ".").strip()
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Некорректная сумма. Попробуй ещё раз, только число:")
        return INCOME_AMOUNT

    context.user_data["income_amount"] = amount
    await update.message.reply_text("За что ты получил этот доход? (например: зарплата, заказ, подработка)")
    return INCOME_DESC


async def income_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    description = update.message.text.strip()
    amount = context.user_data.get("income_amount")
    user_id = update.effective_user.id

    add_record(user_id=user_id, type_="income", amount=amount, description=description)

    main_keyboard = [
        ["➕ Доход", "➖ Расход"],
        ["📊 Статистика"],
    ]
    await update.message.reply_text(
        f"Доход {amount:.2f} ₽ сохранён ✅\nОписание: {description}",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True),
    )
    context.user_data.pop("income_amount", None)
    return ConversationHandler.END


# ---------- РАСХОД ----------

def build_expense_keyboard():
    # Кнопки вида "🍽️ Еда", "🏠 Дом" и т.д.
    rows = []
    for cat in EXPENSE_CATEGORIES:
        rows.append([f"{CATEGORY_EMOJI[cat]} {cat}"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def extract_category_from_button(text: str) -> str | None:
    text = text.strip()
    # Пытаемся найти базовое название категории в тексте кнопки
    for cat in EXPENSE_CATEGORIES:
        if text == cat or text.endswith(cat) or cat in text:
            return cat
    return None


async def expense_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Выбери категорию расхода:",
        reply_markup=build_expense_keyboard(),
    )
    return EXPENSE_CATEGORY


async def expense_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    category = extract_category_from_button(raw)
    if category is None:
        await update.message.reply_text("Пожалуйста, выбери категорию с клавиатуры.")
        return EXPENSE_CATEGORY

    context.user_data["expense_category"] = category
    await update.message.reply_text(
        f"Категория: {CATEGORY_EMOJI[category]} {category}\nТеперь введи сумму расхода:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return EXPENSE_AMOUNT


async def expense_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.replace(",", ".").strip()
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Некорректная сумма. Попробуй ещё раз, только число:")
        return EXPENSE_AMOUNT

    context.user_data["expense_amount"] = amount
    await update.message.reply_text(
        "Напиши комментарий: за что потратил?\n"
        "Например: продукты, кафе, аренда и т.п.",
    )
    return EXPENSE_DESC


async def expense_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    description = update.message.text.strip()
    amount = context.user_data.get("expense_amount")
    category = context.user_data.get("expense_category")
    user_id = update.effective_user.id

    add_record(
        user_id=user_id,
        type_="expense",
        amount=amount,
        description=description,
        category=category,
    )

    main_keyboard = [
        ["➕ Доход", "➖ Расход"],
        ["📊 Статистика"],
    ]
    await update.message.reply_text(
        f"Расход {amount:.2f} ₽ сохранён ✅\n"
        f"Категория: {CATEGORY_EMOJI[category]} {category}\n"
        f"Комментарий: {description}",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True),
    )
    context.user_data.pop("expense_amount", None)
    context.user_data.pop("expense_category", None)
    return ConversationHandler.END


# ---------- СТАТИСТИКА ----------

async def stats_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        ["Текущий месяц", "Выбрать месяц"],
        ["За всё время"],
    ]
    await update.message.reply_text(
        "За какой период показать статистику?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    return STATS_PERIOD


def get_current_month_range():
    today = date.today()
    first_day = today.replace(day=1)
    # первый день следующего месяца
    if first_day.month == 12:
        next_month = first_day.replace(year=first_day.year + 1, month=1, day=1)
    else:
        next_month = first_day.replace(month=first_day.month + 1, day=1)
    return datetime.combine(first_day, datetime.min.time()), datetime.combine(next_month, datetime.min.time())


def parse_mm_yy(text: str) -> tuple[int, int]:
    """Парсинг формата ММ-ГГ (например '11-25' → (2025, 11))."""
    month_str, year2_str = text.split("-")
    month = int(month_str)
    year2 = int(year2_str)
    if not (1 <= month <= 12):
        raise ValueError("Неверный месяц")
    # считаем, что это 2000–2099
    year = 2000 + year2
    return year, month


async def stats_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    user_id = update.effective_user.id

    if choice == "Текущий месяц":
        date_from, date_to = get_current_month_range()
        sums, categories = get_stats(user_id, date_from, date_to)
        period_str = f"Текущий месяц ({date_from.date()} — {date_to.date()})"
        await send_stats(update, sums, categories, period_str)
        return ConversationHandler.END

    elif choice == "За всё время":
        sums, categories = get_stats(user_id, None, None)
        await send_stats(update, sums, categories, "За всё время")
        return ConversationHandler.END

    elif choice == "Выбрать месяц":
        await update.message.reply_text(
            "Введи месяц в формате ММ-ГГ (последние 2 цифры года), например: 11-25",
            reply_markup=ReplyKeyboardRemove(),
        )
        return STATS_CUSTOM_MONTH

    else:
        await update.message.reply_text("Пожалуйста, выбери вариант с клавиатуры.")
        return STATS_PERIOD


async def stats_custom_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        year, month = parse_mm_yy(text)
        first_day = date(year, month, 1)
    except Exception:
        await update.message.reply_text(
            "Неверный формат. Нужен ММ-ГГ, например: 11-25 (ноябрь 2025)."
        )
        return STATS_CUSTOM_MONTH

    # первый день следующего месяца
    if first_day.month == 12:
        next_month = first_day.replace(year=first_day.year + 1, month=1, day=1)
    else:
        next_month = first_day.replace(month=first_day.month + 1, day=1)

    date_from = datetime.combine(first_day, datetime.min.time())
    date_to = datetime.combine(next_month, datetime.min.time())

    user_id = update.effective_user.id
    sums, categories = get_stats(user_id, date_from, date_to)
    period_str = f"{month:02d}-{str(year)[-2:]}"
    await send_stats(update, sums, categories, f"Месяц {period_str}")
    return ConversationHandler.END


async def send_stats(update: Update, sums: dict, categories: dict, period_label: str):
    income = sums.get("income", 0) or 0
    expense = sums.get("expense", 0) or 0
    balance = income - expense

    # Отделяем НЗ (Запас) от остальных категорий
    nz_amount = 0
    other_cats = {}
    for cat, amt in categories.items():
        if cat == "НЗ":
            nz_amount = amt
        else:
            other_cats[cat] = amt

    text_lines = [
        f"📊 Статистика: {period_label}",
        "",
        f"Доход: {income:.2f} ₽",
        f"Расход: {expense:.2f} ₽",
        f"Баланс: {balance:.2f} ₽",
    ]

    if other_cats:
        text_lines.append("")
        text_lines.append("Расходы по категориям:")
        for cat, amt in other_cats.items():
            cat_name = cat if cat else "Без категории"
            emoji = CATEGORY_EMOJI.get(cat, "")
            prefix = f"{emoji} " if emoji else ""
            text_lines.append(f"• {prefix}{cat_name}: {amt:.2f} ₽")

    if nz_amount:
        text_lines.append("")
        text_lines.append("НЗ (Запас):")
        emoji = CATEGORY_EMOJI.get("НЗ", "")
        prefix = f"{emoji} " if emoji else ""
        text_lines.append(f"• {prefix}НЗ: {nz_amount:.2f} ₽")

    main_keyboard = [
        ["➕ Доход", "➖ Расход"],
        ["📊 Статистика"],
    ]
    await update.message.reply_text(
        "\n".join(text_lines),
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True),
    )


# ---------- ОТМЕНА ----------

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    main_keyboard = [
        ["➕ Доход", "➖ Расход"],
        ["📊 Статистика"],
    ]
    await update.message.reply_text(
        "Действие отменено.", reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
    )
    return ConversationHandler.END


# ---------- MAIN ----------

def main():
    init_db()

    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Укажи его в переменных окружения Railway.")

    app = ApplicationBuilder().token(TOKEN).build()

    # Доход
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

    # Расход
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

    # Статистика
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
