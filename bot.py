import os
import re
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TZ_NAME = os.getenv("TZ", "Asia/Tashkent").strip()
PORT = int(os.getenv("PORT", "10000"))

RECEPTION_CODE = os.getenv("RECEPTION_CODE", "").strip()
SPA_CODE = os.getenv("SPA_CODE", "").strip()

MORNING_TIME = os.getenv("MORNING_TIME", "09:00").strip()

TZ = ZoneInfo(TZ_NAME)
DB_PATH = "hotel_bot.db"

WORK_OPEN = dtime(9, 0)
WORK_CLOSE = dtime(22, 0)

# ----- Roles -----
ROLE_RECEPTION = "reception"
ROLE_SPA = "spa"


def now_tz() -> datetime:
    return datetime.now(TZ)


def parse_hhmm(s: str) -> dtime:
    s = s.strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", s):
        raise ValueError("Bad HH:MM")
    hh, mm = map(int, s.split(":"))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("Bad HH:MM")
    return dtime(hh, mm)


def parse_ddmm(s: str):
    s = s.strip()
    if not re.fullmatch(r"\d{1,2}\.\d{1,2}", s):
        raise ValueError("Bad DD.MM")
    d, m = map(int, s.split("."))
    if not (1 <= d <= 31 and 1 <= m <= 12):
        raise ValueError("Bad DD.MM")
    return d, m


def make_dt_current_year(ddmm: str, hhmm: str) -> datetime:
    d, mo = parse_ddmm(ddmm)
    tm = parse_hhmm(hhmm)
    now = now_tz()
    return datetime(now.year, mo, d, tm.hour, tm.minute, tzinfo=TZ)


def day_range(day: datetime):
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=TZ)
    end = start + timedelta(days=1)
    return start, end


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m %H:%M")


def is_10_digits(s: str) -> bool:
    return bool(re.fullmatch(r"\d{10}", (s or "").strip()))


def is_room(s: str) -> bool:
    # номер комнаты: 1-6 цифр (можно расширить)
    return bool(re.fullmatch(r"\d{1,6}", (s or "").strip()))


def is_amount(s: str) -> bool:
    return bool(re.fullmatch(r"\d+(\.\d{1,2})?", (s or "").strip()))


# ----- Keyboards -----
def kb_start():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔐 Войти в админку")]],
        resize_keyboard=True,
    )


def kb_reception():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Ресторан (добавить)"), KeyboardButton(text="➕ Пачка (добавить)")],
            [KeyboardButton(text="➕ SPA folio (добавить)"), KeyboardButton(text="🏷 Долг по комнате")],
            [KeyboardButton(text="📋 Все долги"), KeyboardButton(text="🚪 Выйти")],
        ],
        resize_keyboard=True,
    )


def kb_spa():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить бронь"), KeyboardButton(text="🗑 Удалить бронь")],
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📆 На дату")],
            [KeyboardButton(text="➕ SPA folio (добавить)"), KeyboardButton(text="🚪 Выйти")],
        ],
        resize_keyboard=True,
    )


# ----- FSM -----
class LoginFlow(StatesGroup):
    waiting_code = State()


class AddFolioFlow(StatesGroup):
    waiting_room = State()
    waiting_amount = State()
    waiting_comment = State()


class RoomDebtFlow(StatesGroup):
    waiting_room = State()


class AddBookingFlow(StatesGroup):
    waiting_date = State()
    waiting_time = State()
    waiting_text = State()


class DeleteBookingFlow(StatesGroup):
    waiting_id = State()


class BookingsOnDateFlow(StatesGroup):
    waiting_date = State()


@dataclass
class FolioDraft:
    folio_type: str
    room: str | None = None
    amount: float | None = None
    comment: str | None = None


# ----- DB -----
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            chat_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL,
            logged_in_at TEXT NOT NULL
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            text TEXT NOT NULL,
            reminded INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS folio_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT NOT NULL,
            folio_type TEXT NOT NULL,     -- restaurant/package/spa
            amount REAL NOT NULL,
            comment TEXT,
            created_by_role TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        await db.commit()


async def set_admin(chat_id: int, role: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO admins(chat_id, role, logged_in_at) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET role=excluded.role, logged_in_at=excluded.logged_in_at",
            (chat_id, role, now_tz().isoformat()),
        )
        await db.commit()


async def clear_admin(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE chat_id=?", (chat_id,))
        await db.commit()


async def get_admin_role(chat_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT role FROM admins WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def list_admin_chats(role: str | None = None) -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        if role:
            q = "SELECT chat_id FROM admins WHERE role=?"
            args = (role,)
        else:
            q = "SELECT chat_id FROM admins"
            args = ()
        async with db.execute(q, args) as cur:
            rows = await cur.fetchall()
    return [int(r[0]) for r in rows]


async def add_booking(dt: datetime, text: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO bookings(ts, text, reminded, created_at) VALUES(?,?,0,?)",
            (dt.isoformat(), text, now_tz().isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def delete_booking(bid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM bookings WHERE id=?", (bid,))
        await db.commit()
        return cur.rowcount > 0


async def list_bookings_between(start: datetime, end: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, ts, text FROM bookings WHERE ts>=? AND ts<? ORDER BY ts ASC",
            (start.isoformat(), end.isoformat()),
        ) as cur:
            return await cur.fetchall()


async def get_booking(bid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, ts, text, reminded FROM bookings WHERE id=?", (bid,)) as cur:
            return await cur.fetchone()


async def add_folio(room: str, folio_type: str, amount: float, comment: str | None, created_by_role: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO folio_items(room, folio_type, amount, comment, created_by_role, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (room, folio_type, amount, comment, created_by_role, now_tz().isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def room_debts(room: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT folio_type, COALESCE(SUM(amount),0)
            FROM folio_items
            WHERE room=?
            GROUP BY folio_type
            """,
            (room,),
        ) as cur:
            rows = await cur.fetchall()

        # details
        async with db.execute(
            """
            SELECT id, folio_type, amount, comment, created_by_role, created_at
            FROM folio_items
            WHERE room=?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (room,),
        ) as cur2:
            items = await cur2.fetchall()

    totals = {r[0]: float(r[1]) for r in rows}
    return totals, items


async def all_rooms_debts():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT room,
                   SUM(CASE WHEN folio_type='restaurant' THEN amount ELSE 0 END) AS restaurant_total,
                   SUM(CASE WHEN folio_type='package' THEN amount ELSE 0 END) AS package_total,
                   SUM(CASE WHEN folio_type='spa' THEN amount ELSE 0 END) AS spa_total,
                   SUM(amount) AS grand_total
            FROM folio_items
            GROUP BY room
            ORDER BY grand_total DESC
            """
        ) as cur:
            rows = await cur.fetchall()
    return rows


# ----- Notifications -----
async def send_morning_bookings(bot: Bot):
    today = now_tz()
    start, end = day_range(today)
    rows = await list_bookings_between(start, end)

    text = "📅 Брони на сегодня:\n"
    if not rows:
        text += "— нет броней ✅"
    else:
        for bid, ts, info in rows:
            dt = datetime.fromisoformat(ts)
            text += f"#{bid} — {dt.strftime('%H:%M')} — {info}\n"

    # отправляем всем активным админам (и ресепшен, и spa)
    for chat_id in await list_admin_chats(None):
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            pass


async def send_one_hour_reminders(bot: Bot):
    now = now_tz()
    w_start = now + timedelta(minutes=59)
    w_end = now + timedelta(minutes=61)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, ts, text, reminded
            FROM bookings
            WHERE ts>=? AND ts<?
            ORDER BY ts ASC
            """,
            (w_start.isoformat(), w_end.isoformat()),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return

        spa_admins = await list_admin_chats(ROLE_SPA)

        for bid, ts, info, reminded in rows:
            if int(reminded) == 1:
                continue
            dt = datetime.fromisoformat(ts)

            msg = f"⏰ Через 1 час бронь: {fmt_dt(dt)} — {info} (#{bid})"
            for chat_id in spa_admins:
                try:
                    await bot.send_message(chat_id, msg)
                except Exception:
                    pass

            await db.execute("UPDATE bookings SET reminded=1 WHERE id=?", (bid,))
        await db.commit()


# ----- Web server (Render health) -----
async def handle_root(_request):
    return web.Response(text="OK")


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle_root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()


# ----- Main bot -----
async def run_bot():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty")
    if not (is_10_digits(RECEPTION_CODE) and is_10_digits(SPA_CODE)):
        raise RuntimeError("RECEPTION_CODE and SPA_CODE must be 10 digits")

    await init_db()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    scheduler = AsyncIOScheduler(timezone=TZ)

    # morning schedule
    mt = parse_hhmm(MORNING_TIME)
    scheduler.add_job(
        send_morning_bookings,
        "cron",
        hour=mt.hour,
        minute=mt.minute,
        args=[bot],
        id="morning",
        replace_existing=True,
    )

    # reminders every minute
    scheduler.add_job(send_one_hour_reminders, "interval", minutes=1, args=[bot], id="reminders", replace_existing=True)
    scheduler.start()

    # ---- helpers for menus ----
    async def show_menu(m: Message):
        role = await get_admin_role(m.chat.id)
        if role == ROLE_RECEPTION:
            await m.answer("✅ Вы в панели Ресепшен.", reply_markup=kb_reception())
        elif role == ROLE_SPA:
            await m.answer("✅ Вы в панели SPA.", reply_markup=kb_spa())
        else:
            await m.answer("Нажмите кнопку входа 👇", reply_markup=kb_start())

    # ---- /start ----
    @dp.message(Command("start"))
    async def cmd_start(m: Message, state: FSMContext):
        await state.clear()
        await show_menu(m)

    # ---- login ----
    @dp.message(F.text == "🔐 Войти в админку")
    async def login_btn(m: Message, state: FSMContext):
        await state.clear()
        await state.set_state(LoginFlow.waiting_code)
        await m.answer("Введите 10-значный код администратора:")

    @dp.message(LoginFlow.waiting_code)
    async def login_code(m: Message, state: FSMContext):
        code = (m.text or "").strip()
        if not is_10_digits(code):
            await m.answer("❌ Код должен быть из 10 цифр. Введите ещё раз:")
            return

        if code == RECEPTION_CODE:
            await set_admin(m.chat.id, ROLE_RECEPTION)
            await state.clear()
            await m.answer("✅ Вход выполнен: Ресепшен.", reply_markup=kb_reception())
            return

        if code == SPA_CODE:
            await set_admin(m.chat.id, ROLE_SPA)
            await state.clear()
            await m.answer("✅ Вход выполнен: SPA.", reply_markup=kb_spa())
            return

        await m.answer("❌ Неверный код. Попробуйте снова:")

    # ---- logout ----
    @dp.message(F.text == "🚪 Выйти")
    async def logout(m: Message, state: FSMContext):
        await state.clear()
        await clear_admin(m.chat.id)
        await m.answer("Вы вышли ✅", reply_markup=kb_start())

    # ---------------- RECEPTION: add folio restaurant/package/spa ----------------
    async def start_add_folio(m: Message, state: FSMContext, folio_type: str):
        role = await get_admin_role(m.chat.id)
        if role not in (ROLE_RECEPTION, ROLE_SPA):
            await m.answer("Нет доступа. Нажмите /start")
            return
        # reception can add restaurant/package/spa; spa can add spa
        if folio_type in ("restaurant", "package") and role != ROLE_RECEPTION:
            await m.answer("Это может делать только Ресепшен.")
            return
        if folio_type == "spa" and role not in (ROLE_RECEPTION, ROLE_SPA):
            await m.answer("Нет доступа.")
            return

        await state.clear()
        await state.set_state(AddFolioFlow.waiting_room)
        await state.update_data(folio=FolioDraft(folio_type=folio_type).__dict__)
        await m.answer("Введите номер комнаты (например 120):")

    @dp.message(F.text == "➕ Ресторан (добавить)")
    async def add_rest(m: Message, state: FSMContext):
        await start_add_folio(m, state, "restaurant")

    @dp.message(F.text == "➕ Пачка (добавить)")
    async def add_pack(m: Message, state: FSMContext):
        await start_add_folio(m, state, "package")

    @dp.message(F.text == "➕ SPA folio (добавить)")
    async def add_spa_folio(m: Message, state: FSMContext):
        # доступно и ресепшену и spa
        await start_add_folio(m, state, "spa")

    @dp.message(AddFolioFlow.waiting_room)
    async def folio_room(m: Message, state: FSMContext):
        room = (m.text or "").strip()
        if not is_room(room):
            await m.answer("❌ Неверный номер комнаты. Введите снова (только цифры):")
            return
        data = await state.get_data()
        folio = data.get("folio", {})
        folio["room"] = room
        await state.update_data(folio=folio)
        await state.set_state(AddFolioFlow.waiting_amount)
        await m.answer("Введите сумму (например 150000 или 150000.50):")

    @dp.message(AddFolioFlow.waiting_amount)
    async def folio_amount(m: Message, state: FSMContext):
        amount_s = (m.text or "").strip().replace(",", ".")
        if not is_amount(amount_s):
            await m.answer("❌ Неверная сумма. Введите снова:")
            return
        amount = float(amount_s)

        data = await state.get_data()
        folio = data.get("folio", {})
        folio["amount"] = amount
        await state.update_data(folio=folio)
        await state.set_state(AddFolioFlow.waiting_comment)
        await m.answer("Комментарий (можно пусто). Напишите текст или отправьте '-' :")

    @dp.message(AddFolioFlow.waiting_comment)
    async def folio_comment(m: Message, state: FSMContext):
        comment = (m.text or "").strip()
        if comment == "-" or comment == "":
            comment = None

        role = await get_admin_role(m.chat.id)
        data = await state.get_data()
        folio = data.get("folio", {})
        ftype = folio.get("folio_type")
        room = folio.get("room")
        amount = float(folio.get("amount") or 0)

        await add_folio(room=room, folio_type=ftype, amount=amount, comment=comment, created_by_role=role)

        await state.clear()

        # show to reception always (если добавил SPA — ресепшен увидит)
        msg = f"💳 Folio добавлен: комната {room}\nТип: {ftype}\nСумма: {amount}\nКомментарий: {comment or '—'}\nКем: {role}"
        # отправим всем ресепшен-админам
        for chat_id in await list_admin_chats(ROLE_RECEPTION):
            try:
                await bot.send_message(chat_id, msg)
            except Exception:
                pass

        # ответ тому, кто добавил
        await m.answer("✅ Добавлено.", reply_markup=kb_reception() if role == ROLE_RECEPTION else kb_spa())

    # ---------------- RECEPTION: debts ----------------
    @dp.message(F.text == "🏷 Долг по комнате")
    async def debt_by_room(m: Message, state: FSMContext):
        role = await get_admin_role(m.chat.id)
        if role != ROLE_RECEPTION:
            await m.answer("Только для Ресепшен.")
            return
        await state.clear()
        await state.set_state(RoomDebtFlow.waiting_room)
        await m.answer("Введите номер комнаты:")

    @dp.message(RoomDebtFlow.waiting_room)
    async def debt_room_input(m: Message, state: FSMContext):
        room = (m.text or "").strip()
        if not is_room(room):
            await m.answer("❌ Неверный номер комнаты. Введите снова:")
            return

        totals, items = await room_debts(room)
        rest = totals.get("restaurant", 0.0)
        pack = totals.get("package", 0.0)
        spa = totals.get("spa", 0.0)
        grand = rest + pack + spa

        lines = [
            f"🏷 Комната {room}",
            f"🍽 Ресторан: {rest}",
            f"📦 Пачка: {pack}",
            f"💆 SPA: {spa}",
            f"💰 Итого: {grand}",
            "",
            "Последние операции (до 20):",
        ]
        if not items:
            lines.append("— нет операций")
        else:
            for _id, ftype, amount, comment, who, created_at in items:
                dt = datetime.fromisoformat(created_at)
                lines.append(f"#{_id} {dt.strftime('%d.%m %H:%M')} {ftype} {amount} ({who}) {comment or ''}".strip())

        await state.clear()
        await m.answer("\n".join(lines), reply_markup=kb_reception())

    @dp.message(F.text == "📋 Все долги")
    async def debts_all(m: Message):
        role = await get_admin_role(m.chat.id)
        if role != ROLE_RECEPTION:
            await m.answer("Только для Ресепшен.")
            return
        rows = await all_rooms_debts()
        if not rows:
            await m.answer("Долгов нет ✅", reply_markup=kb_reception())
            return
        lines = ["📋 Долги по комнатам:"]
        for room, rest, pack, spa, grand in rows[:80]:
            lines.append(f"Комната {room}: 🍽{rest} 📦{pack} 💆{spa} = 💰{grand}")
        await m.answer("\n".join(lines), reply_markup=kb_reception())

    # ---------------- SPA: bookings ----------------
    @dp.message(F.text == "➕ Добавить бронь")
    async def spa_add_booking(m: Message, state: FSMContext):
        role = await get_admin_role(m.chat.id)
        if role != ROLE_SPA:
            await m.answer("Только для SPA админа.")
            return
        await state.clear()
        await state.set_state(AddBookingFlow.waiting_date)
        await m.answer("Введите дату ДД.ММ (например 20.02):")

    @dp.message(AddBookingFlow.waiting_date)
    async def spa_booking_date(m: Message, state: FSMContext):
        role = await get_admin_role(m.chat.id)
        if role != ROLE_SPA:
            return
        ddmm = (m.text or "").strip()
        try:
            d, mo = parse_ddmm(ddmm)
        except Exception:
            await m.answer("❌ Неверная дата. Пример: 20.02")
            return

        # запрет прошлых дат (по дню)
        now = now_tz()
        candidate = datetime(now.year, mo, d, 0, 0, tzinfo=TZ)
        if candidate.date() < now.date():
            await m.answer("⚠️ Прошедшую дату нельзя. Введите снова:")
            return

        await state.update_data(ddmm=ddmm)
        await state.set_state(AddBookingFlow.waiting_time)
        await m.answer("Введите время ЧЧ:ММ (например 14:00):")

    @dp.message(AddBookingFlow.waiting_time)
    async def spa_booking_time(m: Message, state: FSMContext):
        role = await get_admin_role(m.chat.id)
        if role != ROLE_SPA:
            return
        hhmm = (m.text or "").strip()
        try:
            tm = parse_hhmm(hhmm)
        except Exception:
            await m.answer("❌ Неверное время. Пример: 14:00")
            return
        if not (WORK_OPEN <= tm <= WORK_CLOSE):
            await m.answer("⚠️ Рабочее время 09:00–22:00. Введите время снова:")
            return

        data = await state.get_data()
        ddmm = data["ddmm"]
        dt = make_dt_current_year(ddmm, hhmm)
        if dt <= now_tz():
            await m.answer("⚠️ Прошедшее время нельзя. Введите время снова:")
            return

        await state.update_data(hhmm=hhmm)
        await state.set_state(AddBookingFlow.waiting_text)
        await m.answer("Введите текст брони (например: Массаж; Комната 120; Имя):")

    @dp.message(AddBookingFlow.waiting_text)
    async def spa_booking_text(m: Message, state: FSMContext):
        role = await get_admin_role(m.chat.id)
        if role != ROLE_SPA:
            return
        text = (m.text or "").strip()
        if not text:
            await m.answer("Текст не должен быть пустым. Введите снова:")
            return

        data = await state.get_data()
        ddmm, hhmm = data["ddmm"], data["hhmm"]
        dt = make_dt_current_year(ddmm, hhmm)
        if dt <= now_tz():
            await m.answer("⚠️ Прошедшее время нельзя. Начните заново: ➕ Добавить бронь")
            await state.clear()
            return

        bid = await add_booking(dt, text)
        await state.clear()
        await m.answer(f"✅ Добавлено: #{bid} — {fmt_dt(dt)} — {text}", reply_markup=kb_spa())

    @dp.message(F.text == "🗑 Удалить бронь")
    async def spa_delete_booking(m: Message, state: FSMContext):
        role = await get_admin_role(m.chat.id)
        if role != ROLE_SPA:
            await m.answer("Только для SPA админа.")
            return
        await state.clear()
        await state.set_state(DeleteBookingFlow.waiting_id)
        await m.answer("Введите ID брони (например 12):")

    @dp.message(DeleteBookingFlow.waiting_id)
    async def spa_delete_booking_id(m: Message, state: FSMContext):
        role = await get_admin_role(m.chat.id)
        if role != ROLE_SPA:
            return
        txt = (m.text or "").strip()
        if not txt.isdigit():
            await m.answer("Введите число (ID). Например 12:")
            return
        bid = int(txt)
        ok = await delete_booking(bid)
        await state.clear()
        await m.answer("✅ Удалено." if ok else "⚠️ Не найдено.", reply_markup=kb_spa())

    @dp.message(F.text == "📅 Сегодня")
    async def spa_today(m: Message):
        role = await get_admin_role(m.chat.id)
        if role != ROLE_SPA:
            await m.answer("Только для SPA админа.")
            return
        today = now_tz()
        start, end = day_range(today)
        rows = await list_bookings_between(start, end)
        if not rows:
            await m.answer("Сегодня броней нет ✅", reply_markup=kb_spa())
            return
        lines = ["📅 Брони на сегодня:"]
        for bid, ts, info in rows:
            dt = datetime.fromisoformat(ts)
            lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {info}")
        await m.answer("\n".join(lines), reply_markup=kb_spa())

    @dp.message(F.text == "📆 На дату")
    async def spa_on_date(m: Message, state: FSMContext):
        role = await get_admin_role(m.chat.id)
        if role != ROLE_SPA:
            await m.answer("Только для SPA админа.")
            return
        await state.clear()
        await state.set_state(BookingsOnDateFlow.waiting_date)
        await m.answer("Введите дату ДД.ММ (например 20.02):")

    @dp.message(BookingsOnDateFlow.waiting_date)
    async def spa_on_date_input(m: Message, state: FSMContext):
        role = await get_admin_role(m.chat.id)
        if role != ROLE_SPA:
            return
        ddmm = (m.text or "").strip()
        try:
            d, mo = parse_ddmm(ddmm)
        except Exception:
            await m.answer("❌ Неверная дата. Пример: 20.02")
            return

        now = now_tz()
        day = datetime(now.year, mo, d, 0, 0, tzinfo=TZ)
        start, end = day_range(day)
        rows = await list_bookings_between(start, end)
        await state.clear()

        if not rows:
            await m.answer(f"На {ddmm} броней нет ✅", reply_markup=kb_spa())
            return

        lines = [f"📆 Брони на {ddmm}:"]
        for bid, ts, info in rows:
            dt = datetime.fromisoformat(ts)
            lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {info}")
        await m.answer("\n".join(lines), reply_markup=kb_spa())

    # ---- fallback ----
    @dp.message()
    async def fallback(m: Message):
        await show_menu(m)

    await dp.start_polling(bot)


async def main():
    await init_db()
    await asyncio.gather(run_web_server(), run_bot())


if __name__ == "__main__":
    asyncio.run(main())