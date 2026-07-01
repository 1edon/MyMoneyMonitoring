import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ===========================================================================
# 1. КОНФИГУРАЦИЯ И НАСТРОЙКИ
# ===========================================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден! Проверьте файл .env")

DB_PATH = "finance.db"
DEFAULT_TIMEZONE = "Europe/Moscow"
POPULAR_TIMEZONES = [
    "Europe/Kaliningrad", "Europe/Moscow", "Europe/Samara",
    "Asia/Yekaterinburg", "Asia/Omsk", "Asia/Novosibirsk",
    "Asia/Krasnoyarsk", "Asia/Irkutsk", "Asia/Yakutsk",
    "Asia/Vladivostok", "Asia/Magadan", "Asia/Kamchatka",
]
DEFAULT_INCOME_CATEGORIES = ["Зарплата", "Подарки", "Подработка", "Инвестиции"]
DEFAULT_EXPENSE_CATEGORIES = ["Еда", "Транспорт", "Жилье", "Развлечения", "Здоровье"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")
logger = logging.getLogger(__name__)

# ===========================================================================
# 2. FSM СОСТОЯНИЯ
# ===========================================================================
class TransactionStates(StatesGroup):
    choosing_category = State()
    entering_amount = State()
    entering_comment = State()

class CategoryStates(StatesGroup):
    adding_category = State()

class DebtStates(StatesGroup):
    entering_person = State()
    entering_amount = State()
    entering_comment = State()

class SettingsStates(StatesGroup):
    entering_timezone = State()

# ===========================================================================
# 3. БАЗА ДАННЫХ
# ===========================================================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
                last_bot_message_id INTEGER,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('income','expense')),
                name TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('income','expense')),
                category_id INTEGER,
                amount REAL NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS debts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                operation_type TEXT NOT NULL,
                person_name TEXT NOT NULL,
                amount REAL NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.commit()

async def db_get_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def db_create_user(user_id: int) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, timezone, created_at) VALUES (?,?,?)",
            (user_id, DEFAULT_TIMEZONE, now),
        )
        for name in DEFAULT_INCOME_CATEGORIES:
            await db.execute("INSERT INTO categories (user_id, type, name) VALUES (?, 'income', ?)", (user_id, name))
        for name in DEFAULT_EXPENSE_CATEGORIES:
            await db.execute("INSERT INTO categories (user_id, type, name) VALUES (?, 'expense', ?)", (user_id, name))
        await db.commit()
    return await db_get_user(user_id)

async def db_get_or_create_user(user_id: int) -> dict:
    user = await db_get_user(user_id)
    return user if user else await db_create_user(user_id)

async def db_save_last_message(user_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_bot_message_id = ? WHERE user_id = ?", (message_id, user_id))
        await db.commit()

async def db_update_timezone(user_id: int, tz: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET timezone = ? WHERE user_id = ?", (tz, user_id))
        await db.commit()

async def db_get_categories(user_id: int, cat_type: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM categories WHERE user_id=? AND type=? ORDER BY name", (user_id, cat_type)) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def db_add_category(user_id: int, cat_type: str, name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO categories (user_id, type, name) VALUES (?,?,?)", (user_id, cat_type, name))
        await db.commit()

async def db_delete_category(cat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM categories WHERE id=? AND user_id=?", (cat_id, user_id))
        await db.commit()

async def db_add_transaction(user_id: int, ttype: str, cat_id: int, amount: float, comment: str):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO transactions (user_id,type,category_id,amount,comment,created_at) VALUES (?,?,?,?,?,?)",
            (user_id, ttype, cat_id, amount, comment, now)
        )
        await db.commit()

async def db_get_total(user_id: int, ttype: str, start: datetime, end: datetime) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND type=? AND created_at>=? AND created_at<?",
            (user_id, ttype, start.isoformat(), end.isoformat())
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0.0

async def db_get_top_categories(user_id: int, ttype: str, start: datetime, end: datetime, limit: int = 5) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT c.name, SUM(t.amount) as total FROM transactions t
               LEFT JOIN categories c ON t.category_id=c.id
               WHERE t.user_id=? AND t.type=? AND t.created_at>=? AND t.created_at<?
               GROUP BY t.category_id ORDER BY total DESC LIMIT ?""",
            (user_id, ttype, start.isoformat(), end.isoformat(), limit)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def db_add_debt(user_id: int, op_type: str, person: str, amount: float, comment: str):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO debts (user_id,operation_type,person_name,amount,comment,created_at) VALUES (?,?,?,?,?,?)",
            (user_id, op_type, person, amount, comment, now)
        )
        await db.commit()

async def db_get_debt_summary(user_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT person_name, 
               SUM(CASE WHEN operation_type='i_borrowed' THEN amount ELSE 0 END) - 
               SUM(CASE WHEN operation_type='i_returned' THEN amount ELSE 0 END) as balance
               FROM debts WHERE user_id=? AND operation_type IN ('i_borrowed','i_returned') GROUP BY person_name""", 
            (user_id,)
        ) as cur:
            i_owe = {r["person_name"]: r["balance"] for r in await cur.fetchall() if r["balance"] > 0}

        async with db.execute(
            """SELECT person_name, 
               SUM(CASE WHEN operation_type='they_borrowed' THEN amount ELSE 0 END) - 
               SUM(CASE WHEN operation_type='they_returned' THEN amount ELSE 0 END) as balance
               FROM debts WHERE user_id=? AND operation_type IN ('they_borrowed','they_returned') GROUP BY person_name""", 
            (user_id,)
        ) as cur:
            they_owe = {r["person_name"]: r["balance"] for r in await cur.fetchall() if r["balance"] > 0}
    return {"i_owe": i_owe, "they_owe": they_owe}

async def db_get_debt_history(user_id: int, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM debts WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit)) as cur:
            return [dict(r) for r in await cur.fetchall()]

# ===========================================================================
# 4. УТИЛИТЫ
# ===========================================================================
def parse_amount(text: str) -> float | None:
    try:
        val = float(text.strip().replace(",", ".").replace(" ", ""))
        return round(val, 2) if val > 0 else None
    except ValueError:
        return None

def fmt_amount(amount: float) -> str:
    return f"{int(amount):,} ₽".replace(",", " ") if amount == int(amount) else f"{amount:,.2f} ₽".replace(",", " ")

def get_tz(tz_str: str) -> ZoneInfo:
    try: return ZoneInfo(tz_str)
    except Exception: return ZoneInfo(DEFAULT_TIMEZONE)

def month_bounds_utc(tz_str: str) -> tuple[datetime, datetime]:
    tz = get_tz(tz_str)
    now = datetime.now(tz)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=now.year + 1, month=1) if now.month == 12 else start.replace(month=now.month + 1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

def day_bounds_utc(tz_str: str) -> tuple[datetime, datetime]:
    tz = get_tz(tz_str)
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

def all_time_bounds() -> tuple[datetime, datetime]:
    return (datetime(2000, 1, 1, tzinfo=timezone.utc), datetime(2100, 1, 1, tzinfo=timezone.utc))

def fmt_date(dt_str: str, tz_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
        return dt.astimezone(get_tz(tz_str)).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return dt_str

MONTH_NAMES = {1:"январь", 2:"февраль", 3:"март", 4:"апрель", 5:"май", 6:"июнь",
               7:"июль", 8:"август", 9:"сентябрь", 10:"октябрь", 11:"ноябрь", 12:"декабрь"}

DEBT_LABELS = {"i_borrowed": "Я взял", "i_returned": "Я вернул", "they_borrowed": "У меня взяли", "they_returned": "Мне вернули"}

async def safe_delete(bot: Bot, chat_id: int, message_id: int | None):
    if not message_id: return
    try: await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest: pass
    except Exception as e: logger.debug(f"Delete error: {e}")

async def send_new_screen(bot: Bot, user_id: int, chat_id: int, text: str, markup: InlineKeyboardMarkup):
    """Удаляет старое сообщение и отправляет новое (принцип одного сообщения)"""
    user = await db_get_or_create_user(user_id)
    await safe_delete(bot, chat_id, user.get("last_bot_message_id"))
    msg = await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    await db_save_last_message(user_id, msg.message_id)

# ===========================================================================
# 5. КЛАВИАТУРЫ
# ===========================================================================
def kb_main_menu():
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="💰 Доход", callback_data="add_income"), InlineKeyboardButton(text="💸 Расход", callback_data="add_expense"))
    b.row(InlineKeyboardButton(text="📂 Категории доходов", callback_data="cats_income"), InlineKeyboardButton(text="📂 Категории расходов", callback_data="cats_expense"))
    b.row(InlineKeyboardButton(text="🤝 Долги", callback_data="debts_menu"))
    b.row(InlineKeyboardButton(text="📊 Детальная статистика", callback_data="stats_detail"))
    b.row(InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings_menu"))
    return b.as_markup()

def kb_cancel():
    return InlineKeyboardBuilder().row(InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu")).as_markup()

def kb_back():
    return InlineKeyboardBuilder().row(InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu")).as_markup()

def kb_cats_select(categories, prefix):
    b = InlineKeyboardBuilder()
    for c in categories: b.row(InlineKeyboardButton(text=c["name"], callback_data=f"{prefix}:{c['id']}"))
    b.row(InlineKeyboardButton(text="➕ Новая категория", callback_data=f"newcat_{prefix}"))
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu"))
    return b.as_markup()

def kb_cats_manage(categories, cat_type):
    b = InlineKeyboardBuilder()
    for c in categories: b.row(InlineKeyboardButton(text=f"🗑 {c['name']}", callback_data=f"delcat:{c['id']}"))
    b.row(InlineKeyboardButton(text="➕ Добавить", callback_data=f"addcatm:{cat_type}"))
    b.row(InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu"))
    return b.as_markup()

def kb_comment():
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⏭ Без комментария", callback_data="no_comment"))
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu"))
    return b.as_markup()

def kb_debts():
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📥 Я взял", callback_data="debtop:i_borrowed"), InlineKeyboardButton(text="📤 Я вернул", callback_data="debtop:i_returned"))
    b.row(InlineKeyboardButton(text="📤 У меня взяли", callback_data="debtop:they_borrowed"), InlineKeyboardButton(text="📥 Мне вернули", callback_data="debtop:they_returned"))
    b.row(InlineKeyboardButton(text="📋 Текущие долги", callback_data="debt_sum"), InlineKeyboardButton(text="📜 История", callback_data="debt_hist"))
    b.row(InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu"))
    return b.as_markup()

def kb_stats():
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="За день", callback_data="statp:day"), InlineKeyboardButton(text="За месяц", callback_data="statp:month"), InlineKeyboardButton(text="За всё время", callback_data="statp:all"))
    b.row(InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu"))
    return b.as_markup()

def kb_tz():
    b = InlineKeyboardBuilder()
    for tz in POPULAR_TIMEZONES: b.row(InlineKeyboardButton(text=tz, callback_data=f"settz:{tz}"))
    b.row(InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="settz_manual"))
    b.row(InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu"))
    return b.as_markup()

# ===========================================================================
# 6. РОУТЕР И ХЕНДЛЕРЫ
# ===========================================================================
router = Router()

@router.message(CommandStart())
async def cmd_start(msg: Message, bot: Bot, state: FSMContext):
    await state.clear()
    await safe_delete(bot, msg.chat.id, msg.message_id) # Удаляем сообщение пользователя
    await show_main_menu(msg.from_user.id, msg.chat.id, bot)

@router.callback_query(F.data == "main_menu")
async def cb_main(call: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    await show_main_menu(call.from_user.id, call.message.chat.id, bot)
    await call.answer()

async def show_main_menu(user_id: int, chat_id: int, bot: Bot):
    user = await db_get_or_create_user(user_id)
    tz_str = user["timezone"]
    start, end = month_bounds_utc(tz_str)
    inc = await db_get_total(user_id, "income", start, end)
    exp = await db_get_total(user_id, "expense", start, end)
    bal = inc - exp
    now = datetime.now(get_tz(tz_str))
    
    text = (f"<b>💼 Финансовый учёт</b>\n\n📅 Дата: <b>{now.strftime('%d.%m.%Y')}</b>\n🌍 Часовой пояс: <b>{tz_str}</b>\n\n"
            f"<b>Статистика за {MONTH_NAMES.get(now.month, '')}:</b>\n"
            f"💰 Доходы: <b>{fmt_amount(inc)}</b>\n💸 Расходы: <b>{fmt_amount(exp)}</b>\n📊 Баланс: <b>{fmt_amount(bal)}</b>\n\nВыберите действие:")
    await send_new_screen(bot, user_id, chat_id, text, kb_main_menu())

# --- ТРАНЗАКЦИИ ---
@router.callback_query(F.data.in_({"add_income", "add_expense"}))
async def cb_add_trans(call: CallbackQuery, bot: Bot, state: FSMContext):
    ttype = "income" if call.data == "add_income" else "expense"
    cats = await db_get_categories(call.from_user.id, ttype)
    await state.update_data(ttype=ttype)
    lbl = "дохода" if ttype == "income" else "расхода"
    await send_new_screen(bot, call.from_user.id, call.message.chat.id, f"Выберите категорию {lbl}:", kb_cats_select(cats, "selcat"))
    await state.set_state(TransactionStates.choosing_category)
    await call.answer()

@router.callback_query(TransactionStates.choosing_category, F.data.startswith("selcat:"))
async def cb_sel_cat(call: CallbackQuery, bot: Bot, state: FSMContext):
    cat_id = int(call.data.split(":")[1])
    await state.update_data(cat_id=cat_id)
    await send_new_screen(bot, call.from_user.id, call.message.chat.id, "Введите сумму (например, 1000 или 150.50):", kb_cancel())
    await state.set_state(TransactionStates.entering_amount)
    await call.answer()

@router.callback_query(TransactionStates.choosing_category, F.data.startswith("newcat_"))
async def cb_newcat_trans(call: CallbackQuery, bot: Bot, state: FSMContext):
    await send_new_screen(bot, call.from_user.id, call.message.chat.id, "Введите название новой категории:", kb_cancel())
    await state.set_state(CategoryStates.adding_category)
    await call.answer()

@router.message(TransactionStates.entering_amount)
async def msg_amount(msg: Message, bot: Bot, state: FSMContext):
    await safe_delete(bot, msg.chat.id, msg.message_id)
    amount = parse_amount(msg.text)
    if not amount:
        await send_new_screen(bot, msg.from_user.id, msg.chat.id, "❌ Некорректная сумма. Попробуйте еще раз (например, 1000):", kb_cancel())
        return
    await state.update_data(amount=amount)
    await send_new_screen(bot, msg.from_user.id, msg.chat.id, f"Сумма: {fmt_amount(amount)}\nВведите комментарий или нажмите кнопку:", kb_comment())
    await state.set_state(TransactionStates.entering_comment)

@router.message(TransactionStates.entering_comment)
async def msg_comment(msg: Message, bot: Bot, state: FSMContext):
    await safe_delete(bot, msg.chat.id, msg.message_id)
    await finish_transaction(msg.from_user.id, msg.chat.id, bot, state, msg.text)

@router.callback_query(TransactionStates.entering_comment, F.data == "no_comment")
async def cb_no_comment(call: CallbackQuery, bot: Bot, state: FSMContext):
    await finish_transaction(call.from_user.id, call.message.chat.id, bot, state, "")
    await call.answer()

async def finish_transaction(user_id: int, chat_id: int, bot: Bot, state: FSMContext, comment: str):
    data = await state.get_data()
    await db_add_transaction(user_id, data["ttype"], data["cat_id"], data["amount"], comment)
    await state.clear()
    await show_main_menu(user_id, chat_id, bot)

# --- УПРАВЛЕНИЕ КАТЕГОРИЯМИ ---
@router.callback_query(F.data.in_({"cats_income", "cats_expense"}))
async def cb_cats_manage(call: CallbackQuery, bot: Bot):
    ttype = "income" if call.data == "cats_income" else "expense"
    cats = await db_get_categories(call.fromuser.id if hasattr(call, 'from_user') else call.from_user.id, ttype)
    lbl = "доходов" if ttype == "income" else "расходов"
    await send_new_screen(bot, call.from_user.id, call.message.chat.id, f"📂 Ваши категории {lbl}:", kb_cats_manage(cats, ttype))
    await call.answer()

@router.callback_query(F.data.startswith("delcat:"))
async def cb_del_cat(call: CallbackQuery, bot: Bot):
    cat_id = int(call.data.split(":")[1])
    await db_delete_category(cat_id, call.from_user.id)
    await show_main_menu(call.from_user.id, call.message.chat.id, bot)
    await call.answer("Категория удалена")

@router.callback_query(F.data.startswith("addcatm:"))
async def cb_addcatm(call: CallbackQuery, bot: Bot, state: FSMContext):
    ttype = call.data.split(":")[1]
    await state.update_data(ttype=ttype)
    await send_new_screen(bot, call.from_user.id, call.message.chat.id, "Введите название новой категории:", kb_cancel())
    await state.set_state(CategoryStates.adding_category)
    await call.answer()

@router.message(CategoryStates.adding_category)
async def msg_add_cat(msg: Message, bot: Bot, state: FSMContext):
    await safe_delete(bot, msg.chat.id, msg.message_id)
    name = msg.text.strip()
    data = await state.get_data()
    ttype = data.get("ttype", "expense")
    await db_add_category(msg.from_user.id, ttype, name)
    
    # Если мы пришли из процесса ввода транзакции, возвращаемся туда
    if await state.get_state() == CategoryStates.adding_category.state and "cat_id" not in data and "amount" not in data:
         # Возвращаем пользователя к выбору категорий
         cats = await db_get_categories(msg.from_user.id, ttype)
         lbl = "дохода" if ttype == "income" else "расхода"
         await send_new_screen(bot, msg.from_user.id, msg.chat.id, f"Выберите категорию {lbl}:", kb_cats_select(cats, "selcat"))
         await state.set_state(TransactionStates.choosing_category)
    else:
         await state.clear()
         await show_main_menu(msg.from_user.id, msg.chat.id, bot)

# --- ДОЛГИ ---
@router.callback_query(F.data == "debts_menu")
async def cb_debts(call: CallbackQuery, bot: Bot):
    await send_new_screen(bot, call.from_user.id, call.message.chat.id, "🤝 Раздел долгов. Выберите действие:", kb_debts())
    await call.answer()

@router.callback_query(F.data.startswith("debtop:"))
async def cb_debt_op(call: CallbackQuery, bot: Bot, state: FSMContext):
    op = call.data.split(":")[1]
    await state.update_data(op=op)
    await send_new_screen(bot, call.from_user.id, call.message.chat.id, "Введите имя человека:", kb_cancel())
    await state.set_state(DebtStates.entering_person)
    await call.answer()

@router.message(DebtStates.entering_person)
async def msg_debt_person(msg: Message, bot: Bot, state: FSMContext):
    await safe_delete(bot, msg.chat.id, msg.message_id)
    await state.update_data(person=msg.text.strip())
    await send_new_screen(bot, msg.from_user.id, msg.chat.id, "Введите сумму:", kb_cancel())
    await state.set_state(DebtStates.entering_amount)

@router.message(DebtStates.entering_amount)
async def msg_debt_amount(msg: Message, bot: Bot, state: FSMContext):
    await safe_delete(bot, msg.chat.id, msg.message_id)
    amount = parse_amount(msg.text)
    if not amount:
        await send_new_screen(bot, msg.from_user.id, msg.chat.id, "❌ Ошибка. Введите сумму цифрами:", kb_cancel())
        return
    await state.update_data(amount=amount)
    await send_new_screen(bot, msg.from_user.id, msg.chat.id, "Введите комментарий или пропустите:", kb_comment())
    await state.set_state(DebtStates.entering_comment)

@router.message(DebtStates.entering_comment)
async def msg_debt_comment(msg: Message, bot: Bot, state: FSMContext):
    await safe_delete(bot, msg.chat.id, msg.message_id)
    await finish_debt(msg.from_user.id, msg.chat.id, bot, state, msg.text)

@router.callback_query(DebtStates.entering_comment, F.data == "no_comment")
async def cb_debt_nocomment(call: CallbackQuery, bot: Bot, state: FSMContext):
    await finish_debt(call.from_user.id, call.message.chat.id, bot, state, "")
    await call.answer()

async def finish_debt(user_id: int, chat_id: int, bot: Bot, state: FSMContext, comment: str):
    d = await state.get_data()
    await db_add_debt(user_id, d["op"], d["person"], d["amount"], comment)
    await state.clear()
    await cb_debts(CallbackQuery(id="", from_user=Message(message_id=0, date=datetime.now(), chat=Message(id=chat_id, type="private")).from_user, chat_instance="", message=Message(message_id=0, date=datetime.now(), chat=Message(id=chat_id, type="private"))), bot) # Хак для возврата в меню долгов, лучше просто вызвать функцию
    await send_new_screen(bot, user_id, chat_id, "✅ Запись о долге сохранена.\nВыберите действие:", kb_debts())

@router.callback_query(F.data == "debt_sum")
async def cb_debt_sum(call: CallbackQuery, bot: Bot):
    summary = await db_get_debt_summary(call.from_user.id)
    text = "<b>📋 Текущие долги</b>\n\n"
    if summary["i_owe"]:
        text += "<b>🔴 Я должен:</b>\n"
        for p, a in summary["i_owe"].items(): text += f"• {p}: {fmt_amount(a)}\n"
    else: text += "<b>🔴 Я должен:</b> <i>нет долгов</i>\n"
    
    text += "\n"
    if summary["they_owe"]:
        text += "<b>🟢 Мне должны:</b>\n"
        for p, a in summary["they_owe"].items(): text += f"• {p}: {fmt_amount(a)}\n"
    else: text += "<b>🟢 Мне должны:</b> <i>никто не должен</i>\n"
    
    await send_new_screen(bot, call.from_user.id, call.message.chat.id, text, kb_back())
    await call.answer()

@router.callback_query(F.data == "debt_hist")
async def cb_debt_hist(call: CallbackQuery, bot: Bot):
    user = await db_get_or_create_user(call.from_user.id)
    hist = await db_get_debt_history(call.from_user.id)
    text = "<b>📜 Последние операции по долгам:</b>\n\n"
    if not hist: text += "<i>История пуста</i>"
    for r in hist:
        text += f"• {fmt_date(r['created_at'], user['timezone'])} | {DEBT_LABELS[r['operation_type']]} | {r['person_name']} | <b>{fmt_amount(r['amount'])}</b>\n"
    await send_new_screen(bot, call.from_user.id, call.message.chat.id, text, kb_back())
    await call.answer()

# --- СТАТИСТИКА ---
@router.callback_query(F.data == "stats_detail")
async def cb_stats_menu(call: CallbackQuery, bot: Bot):
    await send_new_screen(bot, call.from_user.id, call.message.chat.id, "📊 Выберите период для статистики:", kb_stats())
    await call.answer()

@router.callback_query(F.data.startswith("statp:"))
async def cb_stats_show(call: CallbackQuery, bot: Bot):
    period = call.data.split(":")[1]
    user = await db_get_or_create_user(call.from_user.id)
    tz_str = user["timezone"]
    
    if period == "day":
        start, end = day_bounds_utc(tz_str)
        lbl = "день"
    elif period == "month":
        start, end = month_bounds_utc(tz_str)
        lbl = "месяц"
    else:
        start, end = all_time_bounds()
        lbl = "всё время"
        
    inc = await db_get_total(user.get("user_id"), "income", start, end)
    exp = await db_get_total(user.get("user_id"), "expense", start, end)
    bal = inc - exp
    top_exp = await db_get_top_categories(user.get("user_id"), "expense", start, end)
    top_inc = await db_get_top_categories(user.get("user_id"), "income", start, end)
    
    text = f"<b>📊 Статистика за {lbl}</b>\n\n💰 Доходы: <b>{fmt_amount(inc)}</b>\n💸 Расходы: <b>{fmt_amount(exp)}</b>\n📈 Баланс: <b>{fmt_amount(bal)}</b>\n\n"
    if top_exp:
        text += "<b>Топ расходов:</b>\n"
        for i, r in enumerate(top_exp, 1): text += f"  {i}. {r['name']}: {fmt_amount(r['total'])}\n"
    if top_inc:
        text += "\n<b>Топ доходов:</b>\n"
        for i, r in enumerate(top_inc, 1): text += f"  {i}. {r['name']}: {fmt_amount(r['total'])}\n"
    if not top_exp and not top_inc:
        text += "<i>Транзакций нет.</i>"
        
    await send_new_screen(bot, call.from_user.id, call.message.chat.id, text, kb_back())
    await call.answer()

# --- НАСТРОЙКИ ---
@router.callback_query(F.data == "settings_menu")
async def cb_settings(call: CallbackQuery, bot: Bot):
    user = await db_get_or_create_user(call.from_user.id)
    text = f"⚙️ <b>Настройки</b>\n\nТекущий часовой пояс: <b>{user['timezone']}</b>\n\nВыберите новый из списка или введите вручную:"
    await send_new_screen(bot, call.from_user.id, call.message.chat.id, text, kb_tz())
    await call.answer()

@router.callback_query(F.data.startswith("settz:"))
async def cb_set_tz(call: CallbackQuery, bot: Bot, state: FSMContext):
    tz = call.data.split("settz:")[1]
    if tz == "manual":
        await send_new_screen(bot, call.from_user.id, call.message.chat.id, "Введите часовой пояс (например, Europe/Moscow):", kb_cancel())
        await state.set_state(SettingsStates.entering_timezone)
    else:
        await db_update_timezone(call.from_user.id, tz)
        await show_main_menu(call.from_user.id, call.message.chat.id, bot)
    await call.answer()

@router.message(SettingsStates.entering_timezone)
async def msg_set_tz(msg: Message, bot: Bot, state: FSMContext):
    await safe_delete(bot, msg.chat.id, msg.message_id)
    tz = msg.text.strip()
    try:
        ZoneInfo(tz)
        await db_update_timezone(msg.from_user.id, tz)
        await state.clear()
        await show_main_menu(msg.from_user.id, msg.chat.id, bot)
    except (ZoneInfoNotFoundError, KeyError):
        await send_new_screen(bot, msg.from_user.id, msg.chat.id, "❌ Неверный формат. Введите часовой пояс (например, Europe/Moscow):", kb_cancel())


# ===========================================================================
# 7. ЗАПУСК БОТА
# ===========================================================================
async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    logger.info("Бот запущен. Ожидание обновлений...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
