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
    INCOME_LINE,         # "сумма, источник"
    EXPENSE_CATEGORY,    # выбор категории
    EXPENSE_LINE,        # "сумма, куда потрачено"
    STATS_PERIOD,        # выбор периода
    STATS_CUSTOM_MONTH,  # ввод ММ-ГГ
    STATS_DETAIL_LEVEL,  # "Детально"/"Общее"
) = range(6)

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
            description TEXT,         -- источник дохода / куда потрачено
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


def get_records(user_id: int, date_from: datetime | None, date_to: datetime | None):
    """Полный список записей для детальной статистики."""
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

    cur.execute(
        f"""
        SELECT type, category, amount, description, created_at
        FROM records
        WHERE {where_clause}
        ORDER BY
            type,                         -- income, потом expense
            COALESCE(category, ''),       -- по категории
            created_at
        """,
        params,
    )
    rows = cur.fetchall()
    conn.close()
    return rows


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


def is_cancel(text: str) -> bool:
    """Проверка, хочет ли пользователь отменить ввод."""
    t = text.strip().lower()
    return t in ("отмена", "/cancel", "cancel")


def parse_amount_and_text(line: str) -> tuple[float, str]:
    """
    Ожидается формат: "сумма, текст".
    Пример: "1500, зарплата"
    """
    if is_cancel(line):
        raise ValueError("cancel")  # используем особое значение

    if "," not in line:
        raise ValueError("format")

    amount_part, text_part = line.split(",", 1)
    amount_str = amount_part.strip().replace(",", ".")
    try:
        amount = float(amount_str)
        if amount <= 0:
            raise ValueError
    except Exception:
        raise ValueError("amount")

    description = text_part.strip()
    if not description:
        raise ValueError("desc")

    return amount, description


# -------------------------------------------------------------
# ХЭНДЛЕРЫ
# -------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
    await update.message.reply_text(
        "Привет! Я бот для учёта доходов и расходов.\n\n"
        "Доход: введи сразу `сумма, источник`.\n"
        "Расход: выбери категорию, затем `сумма, куда потрачено`.\n"
        "Везде можно написать «отмена» для отмены ввода.",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )


# ---------- ДОХОД ----------
async def income_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введи доход в формате:\n"
        "`сумма, источник`\n"
        "Например: `1500, зарплата`.\n\n"
        "Для отмены напиши «отмена».",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown",
    )
    return INCOME_LINE


async def income_line(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_cancel(text):
        kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
        await update.message.reply_text(
            "Ввод дохода отменён.",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return ConversationHandler.END

    try:
        amount, source = parse_amount_and_text(text)
    except ValueError as e:
        reason = str(e)
        if reason == "cancel":
            kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
            await update.message.reply_text(
                "Ввод дохода отменён.",
                reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
            )
            return ConversationHandler.END
        elif reason == "format":
            await update.message.reply_text(
                "Нужен формат: `сумма, источник`\nНапример: `1500, зарплата`\n\n"
                "Для отмены напиши «отмена».",
                parse_mode="Markdown",
            )
        elif reason == "amount":
            await update.message.reply_text(
                "Некорректная сумма. Пример: `1500, зарплата`.\n\n"
                "Для отмены напиши «отмена».",
                parse_mode="Markdown",
            )
        elif reason == "desc":
            await update.message.reply_text(
                "После запятой нужно указать источник.\nПример: `1500, зарплата`.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("Не понял ввод. Попробуй ещё раз.")
        return INCOME_LINE

    user_id = update.effective_user.id
    add_record(user_id, "income", amount, source)

    kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
    await update.message.reply_text(
        f"✅ Доход {amount:.2f} ₽ сохранён.\nИсточник: {source}",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
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
        "Теперь введи строкой:\n"
        "`сумма, куда потрачено`\n"
        "Например: `500, продукты`.\n\n"
        "Для отмены напиши «отмена».",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown",
    )
    return EXPENSE_LINE


async def expense_line(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_cancel(text):
        context.user_data.pop("expense_category", None)
        kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
        await update.message.reply_text(
            "Ввод расхода отменён.",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return ConversationHandler.END

    try:
        amount, target = parse_amount_and_text(text)
    except ValueError as e:
        reason = str(e)
        if reason == "cancel":
            context.user_data.pop("expense_category", None)
            kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
            await update.message.reply_text(
                "Ввод расхода отменён.",
                reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
            )
            return ConversationHandler.END
        elif reason == "format":
            await update.message.reply_text(
                "Нужен формат: `сумма, куда потрачено`\n"
                "Например: `500, продукты`.\n\n"
                "Для отмены напиши «отмена».",
                parse_mode="Markdown",
            )
        elif reason == "amount":
            await update.message.reply_text(
                "Некорректная сумма. Пример: `500, продукты`.\n\n"
                "Для отмены напиши «отмена».",
                parse_mode="Markdown",
            )
        elif reason == "desc":
            await update.message.reply_text(
                "После запятой нужно написать, куда потратил.\n"
                "Пример: `500, продукты`.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("Не понял ввод. Попробуй ещё раз.")
        return EXPENSE_LINE

    category = context.user_data.get("expense_category")
    user_id = update.effective_user.id

    add_record(user_id, "expense", amount, target, category)

    kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
    await update.message.reply_text(
        f"✅ Расход {amount:.2f} ₽ сохранён.\n"
        f"Категория: {CATEGORY_EMOJI.get(category, '')} {category}\n"
        f"Куда: {target}",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
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
        context.user_data["stats_range"] = (date_from, date_to, "Текущий месяц")
        return await ask_detail_or_summary(update, context)

    if choice == "За всё время":
        context.user_data["stats_range"] = (None, None, "За всё время")
        return await ask_detail_or_summary(update, context)

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
        await update.message.reply_text(
            "Неверный формат. Нужен ММ-ГГ, например 11-25 (ноябрь 2025)."
        )
        return STATS_CUSTOM_MONTH

    first = date(year, month, 1)
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)

    date_from = datetime.combine(first, datetime.min.time())
    date_to = datetime.combine(next_month, datetime.min.time())
    label = f"Месяц {month:02d}-{str(year)[-2:]}"

    context.user_data["stats_range"] = (date_from, date_to, label)
    return await ask_detail_or_summary(update, context)


async def ask_detail_or_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["Детально", "Общее"]]
    await update.message.reply_text(
        "Как показать статистику?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    return STATS_DETAIL_LEVEL


async def stats_detail_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    user_id = update.effective_user.id
    sr = context.user_data.get("stats_range")
    if not sr:
        await update.message.reply_text("Период не найден, начни со /stats заново.")
        return ConversationHandler.END

    date_from, date_to, label = sr

    if choice == "Общее":
        sums, cats = get_stats(user_id, date_from, date_to)
        await send_summary_stats(update, sums, cats, label)
        return ConversationHandler.END

    if choice == "Детально":
        rows = get_records(user_id, date_from, date_to)
        await send_detailed_stats(update, rows, label)
        return ConversationHandler.END

    await update.message.reply_text("Пожалуйста, выбери «Детально» или «Общее».")
    return STATS_DETAIL_LEVEL


async def send_summary_stats(
    update: Update,
    sums: dict,
    cats: dict,
    period_label: str,
):
    income = sums.get("income", 0.0)
    expense = sums.get("expense", 0.0)
    nz_amount = cats.get("НЗ", 0.0)

    # "На руках" — простая модель: доходы - все расходы (включая НЗ)
    on_hands = income - expense

    lines: list[str] = [
        f"📊 Общая статистика: {period_label}",
        "",
        f"Доходы: {income:.2f} ₽",
        f"Расходы: {expense:.2f} ₽",
        f"Запас (НЗ): {nz_amount:.2f} ₽",
        f"На руках: {on_hands:.2f} ₽",
    ]

    kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )


async def send_detailed_stats(
    update: Update,
    rows: list[tuple],
    period_label: str,
):
    if not rows:
        kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
        await update.message.reply_text(
            f"За период «{period_label}» записей нет.",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return

    # Разделим на доходы и расходы по категориям
    incomes: list[str] = []
    expenses_by_cat: dict[str | None, list[str]] = {}

    for type_, category, amount, desc, created_at in rows:
        date_str = created_at.strftime("%Y-%m-%d")
        if type_ == "income":
            incomes.append(f"• {date_str} — {amount:.2f} ₽ — {desc}")
        else:
            expenses_by_cat.setdefault(category, []).append(
                f"• {date_str} — {amount:.2f} ₽ — {desc}"
            )

    lines: list[str] = [f"📋 Детальная статистика: {period_label}", ""]

    if incomes:
        lines.append("Доходы:")
        lines.extend(incomes)
        lines.append("")

    if expenses_by_cat:
        lines.append("Расходы по категориям:")
        for cat in sorted(expenses_by_cat.keys(), key=lambda c: c or ""):
            cat_name = cat or "Без категории"
            emoji = CATEGORY_EMOJI.get(cat, "")
            prefix = f"{emoji} " if emoji else ""
            lines.append(f"{prefix}{cat_name}:")
            lines.extend(expenses_by_cat[cat])
            lines.append("")

    text = "\n".join(lines).strip()

    # Если очень длинно — можно было бы резать на части, но пока отправим как есть
    kb = [["➕ Доход", "➖ Расход"], ["📊 Статистика"]]
    await update.message.reply_text(
        text,
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
            INCOME_LINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, income_line)],
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
            EXPENSE_LINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_line)],
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
            STATS_DETAIL_LEVEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, stats_detail_level)],
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
