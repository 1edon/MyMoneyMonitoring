import asyncio
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    String,
    func,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

# --------------------------------------------------------------------------- #
# Конфигурация и логирование
# --------------------------------------------------------------------------- #
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("finance_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в .env")

DB_URL = os.getenv("DB_URL", "sqlite+aiosqlite:///finance.db")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Moscow")

DEFAULT_INCOME_CATEGORIES = [
    "Зарплата",
    "Подработка",
    "Подарок",
    "Инвестиции",
    "Прочее",
]
DEFAULT_EXPENSE_CATEGORIES = [
    "Еда",
    "Транспорт",
    "Жильё",
    "Развлечения",
    "Здоровье",
    "Одежда",
    "Прочее",
]

TZ_CHOICES = [
    "Europe/Kaliningrad",
    "Europe/Moscow",
    "Europe/Samara",
    "Asia/Yekaterinburg",
    "Asia/Omsk",
    "Asia/Krasnoyarsk",
    "Asia/Irkutsk",
    "Asia/Yakutsk",
    "Asia/Vladivostok",
    "Asia/Kamchatka",
    "Europe/Kyiv",
    "Europe/Minsk",
    "Asia/Almaty",
    "Asia/Tashkent",
    "UTC",
]

MAX_CATEGORIES = 30  # защита от бесконечного добавления


# --------------------------------------------------------------------------- #
# Модели БД
# --------------------------------------------------------------------------- #
class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # telegram id
    timezone: Mapped[str] = mapped_column(String(64), default=DEFAULT_TZ)
    last_bot_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class IncomeCategory(Base):
    __tablename__ = "income_categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))

    records: Mapped[list["IncomeRecord"]] = relationship(back_populates="category")


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))

    records: Mapped[list["ExpenseRecord"]] = relationship(back_populates="category")


class IncomeRecord(Base):
    __tablename__ = "income_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("income_categories.id"))
    amount: Mapped[float] = mapped_column(Float)
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    category: Mapped["IncomeCategory"] = relationship(back_populates="records")


class ExpenseRecord(Base):
    __tablename__ = "expense_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("expense_categories.id"))
    amount: Mapped[float] = mapped_column(Float)
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    category: Mapped["ExpenseCategory"] = relationship(back_populates="records")


class Debt(Base):
    __tablename__ = "debts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # "owe" — я должен, "lent" — мне должны
    counterparty: Mapped[str] = mapped_column(String(128))
    amount: Mapped[float] = mapped_column(Float)
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


engine = create_async_engine(DB_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


# --------------------------------------------------------------------------- #
# FSM состояния
# --------------------------------------------------------------------------- #
class AddFlow(StatesGroup):
    amount = State()
    comment = State()


class CategoryFlow(StatesGroup):
    name = State()


class DebtFlow(StatesGroup):
    counterparty = State()
    amount = State()
    comment = State()


# --------------------------------------------------------------------------- #
# Инициализация БД
# --------------------------------------------------------------------------- #
async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("База данных инициализирована")


async def get_or_create_user(session: AsyncSession, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id, timezone=DEFAULT_TZ)
        session.add(user)
        await session.flush()
        # сеем дефолтные категории для нового пользователя
        session.add_all([IncomeCategory(user_id=user_id, name=n) for n in DEFAULT_INCOME_CATEGORIES])
        session.add_all([ExpenseCategory(user_id=user_id, name=n) for n in DEFAULT_EXPENSE_CATEGORIES])
        await session.commit()
        logger.info("Создан новый пользователь: %s", user_id)
    return user


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #
def get_user_tz(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def month_start_utc(user: User) -> datetime:
    tz = get_user_tz(user)
    now_local = datetime.now(tz)
    start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).replace(tzinfo=None)


def fmt_money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


async def month_totals(session: AsyncSession, user: User) -> tuple[float, float]:
    start = month_start_utc(user)
    income = (
        await session.execute(
            select(func.coalesce(func.sum(IncomeRecord.amount), 0.0)).where(
                IncomeRecord.user_id == user.id,
                IncomeRecord.created_at >= start,
            )
        )
    ).scalar_one()
    expense = (
        await session.execute(
            select(func.coalesce(func.sum(ExpenseRecord.amount), 0.0)).where(
                ExpenseRecord.user_id == user.id,
                ExpenseRecord.created_at >= start,
            )
        )
    ).scalar_one()
    return float(income), float(expense)


async def get_categories(session: AsyncSession, user_id: int, kind: str):
    model = IncomeCategory if kind == "income" else ExpenseCategory
    return (
        await session.execute(
            select(model).where(model.user_id == user_id).order_by(model.id)
        )
    ).scalars().all()


# --------------------------------------------------------------------------- #
# Единое актуальное сообщение
# --------------------------------------------------------------------------- #
async def send_or_edit(
    bot: Bot,
    session: AsyncSession,
    user: User,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    chat_id = user.id
    if user.last_bot_message_id:
        try:
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=user.last_bot_message_id,
                reply_markup=keyboard,
            )
            return
        except TelegramBadRequest:
            try:
                await bot.delete_message(chat_id, user.last_bot_message_id)
            except TelegramBadRequest:
                pass

    msg = await bot.send_message(chat_id, text, reply_markup=keyboard)
    user.last_bot_message_id = msg.message_id
    await session.commit()


# --------------------------------------------------------------------------- #
# Клавиатуры
# --------------------------------------------------------------------------- #
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Доход", callback_data="add:income"),
                InlineKeyboardButton(text="➖ Расход", callback_data="add:expense"),
            ],
            [InlineKeyboardButton(text="📊 Статистика за месяц", callback_data="stats")],
            [
                InlineKeyboardButton(text="🗂 Категории доходов", callback_data="cats:income"),
                InlineKeyboardButton(text="🗂 Категории расходов", callback_data="cats:expense"),
            ],
            [InlineKeyboardButton(text="💳 Долги", callback_data="debts")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")],
        ]
    )


def categories_kb(kind: str, categories) -> InlineKeyboardMarkup:
    """Клавиатура выбора категории при добавлении дохода/расхода."""
    rows = []
    row = []
    for cat in categories:
        row.append(
            InlineKeyboardButton(text=cat.name, callback_data=f"cat:{kind}:{cat.id}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def category_manage_kb(kind: str, categories) -> InlineKeyboardMarkup:
    """Клавиатура управления категориями (добавить / удалить)."""
    rows = []
    for cat in categories:
        rows.append(
            [
                InlineKeyboardButton(text=cat.name, callback_data="noop"),
                InlineKeyboardButton(text="🗑", callback_data=f"catdel:{kind}:{cat.id}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Добавить категорию", callback_data=f"catadd:{kind}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def debts_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📕 Я должен", callback_data="debt_add:owe"),
                InlineKeyboardButton(text="📗 Мне должны", callback_data="debt_add:lent"),
            ],
            [InlineKeyboardButton(text="📋 Список долгов", callback_data="debt_list")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
        ]
    )


def debt_list_kb(debts) -> InlineKeyboardMarkup:
    rows = []
    for d in debts:
        icon = "📕" if d.direction == "owe" else "📗"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {d.counterparty}: {fmt_money(d.amount)}",
                    callback_data="noop",
                ),
                InlineKeyboardButton(text="✅ Закрыть", callback_data=f"debt_close:{d.id}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="debts")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb(back: str = "menu:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data=back)]]
    )


def comment_kb(skip_cb: str, back: str = "menu:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data=skip_cb)],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data=back)],
        ]
    )


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")]]
    )


def settings_kb(current_tz: str) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for tz in TZ_CHOICES:
        mark = "✅ " if tz == current_tz else ""
        row.append(InlineKeyboardButton(text=f"{mark}{tz}", callback_data=f"tz:{tz}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------- #
# Тексты экранов
# --------------------------------------------------------------------------- #
async def build_main_menu_text(session: AsyncSession, user: User) -> str:
    tz = get_user_tz(user)
    now_local = datetime.now(tz)
    income, expense = await month_totals(session, user)
    balance = income - expense
    return (
        "<b>💰 Личные финансы</b>\n\n"
        f"📅 Дата: <b>{now_local.strftime('%d.%m.%Y %H:%M')}</b>\n"
        f"🌍 Таймзона: <code>{user.timezone}</code>\n\n"
        f"📈 Доходы за месяц: <b>{fmt_money(income)}</b>\n"
        f"📉 Расходы за месяц: <b>{fmt_money(expense)}</b>\n"
        f"🧮 Баланс: <b>{fmt_money(balance)}</b>\n\n"
        "Выберите действие:"
    )


async def build_stats_text(session: AsyncSession, user: User) -> str:
    tz = get_user_tz(user)
    now_local = datetime.now(tz)
    start = month_start_utc(user)

    income_rows = (
        await session.execute(
            select(IncomeCategory.name, func.sum(IncomeRecord.amount))
            .join(IncomeRecord, IncomeRecord.category_id == IncomeCategory.id)
            .where(IncomeRecord.user_id == user.id, IncomeRecord.created_at >= start)
            .group_by(IncomeCategory.name)
            .order_by(func.sum(IncomeRecord.amount).desc())
        )
    ).all()

    expense_rows = (
        await session.execute(
            select(ExpenseCategory.name, func.sum(ExpenseRecord.amount))
            .join(ExpenseRecord, ExpenseRecord.category_id == ExpenseCategory.id)
            .where(ExpenseRecord.user_id == user.id, ExpenseRecord.created_at >= start)
            .group_by(ExpenseCategory.name)
            .order_by(func.sum(ExpenseRecord.amount).desc())
        )
    ).all()

    income_total = sum(v for _, v in income_rows)
    expense_total = sum(v for _, v in expense_rows)

    lines = [f"<b>📊 Статистика за {now_local.strftime('%B %Y')}</b>\n"]

    lines.append("<b>📈 Доходы:</b>")
    if income_rows:
        for name, value in income_rows:
            lines.append(f"  • {name}: {fmt_money(float(value))}")
    else:
        lines.append("  — нет записей")
    lines.append(f"  <b>Итого: {fmt_money(float(income_total))}</b>\n")

    lines.append("<b>📉 Расход