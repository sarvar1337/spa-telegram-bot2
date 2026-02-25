import os
import re
import asyncio
import hashlib
from datetime import datetime, timedelta, time as dtime

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo

# ---------------- ENV ----------------
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
TZ_NAME = (os.getenv("TZ") or "Asia/Tashkent").strip()
MORNING_TIME = (os.getenv("MORNING_TIME") or "09:00").strip()

PIN_RECEPTION = (os.getenv("PIN_RECEPTION") or "").strip()  # 10 digits
PIN_SPA = (os.getenv("PIN_SPA") or "").strip()              # 10 digits

TZ = ZoneInfo(TZ_NAME)
DB_PATH = "hotel_bot.db"

ROLE_RECEPTION = "reception"
ROLE_SPA = "spa"

CAT_RESTAURANT = "restaurant"
CAT_LAUNDRY = "laundry"
CAT_SPA = "spa"

OPEN_TIME = dtime(9, 0)
CLOSE_TIME = dtime(22, 0)

# ---------------- HELPERS ----------------
def now_tz() -> datetime:
    return datetime.now(TZ)

def is_10_digits(s: str) -> bool:
    return bool(re.fullmatch(r"\d{10}", (s or "").strip()))

def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

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
    n = now_tz()
    return datetime(n.year, mo, d, tm.hour, tm.minute, tzinfo=TZ)

def day_range(day: datetime):
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=TZ)
    end = start + timedelta(days=1)
    return start, end

def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m %H:%M")

def in_working_hours(tm: dtime) -> bool:
    return OPEN_TIME <= tm <= CLOSE_TIME

def parse_room(s: str) -> str:
    s = (s or "").strip()
    if not re.fullmatch(r"\d{1,6}", s):
        raise ValueError("Bad room")
    return s

def parse_amount(s: str) -> int:
    s = (s or "").strip().replace(" ", "")
    if not re.fullmatch(r"\d{1,9}", s):
        raise ValueError("Bad amount")
    v = int(s)
    if v <= 0:
        raise ValueError("Bad amount")
    return v

# ---------------- KEYBOARDS ----------------
def kb_auth():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔐 Войти (код)")]],
        resize_keyboard=True
    )

def kb_reception():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Счёт: Ресторан"), KeyboardButton(text="➕ Счёт: Прачка")],
            [KeyboardButton(text="➕ Счёт: SPA (фолио)"), KeyboardButton(text="📄 Фолио по номеру")],
            [KeyboardButton(text="🚪 Выйти")],
        ],
        resize_keyboard=True
    )

def kb_spa():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить бронь"), KeyboardButton(text="🗑 Удалить бронь")],
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📆 Брони на дату")],
            [KeyboardButton(text="➕ Счёт: SPA (фолио)"), KeyboardButton(text="📄 Фолио по номеру")],
            [KeyboardButton(text="🚪 Выйти")],
        ],
        resize_keyboard=True
    )

# ---------------- FSM ----------------
class AuthFlow(StatesGroup):
    waiting_pin = State()

class SpaAddBookingFlow(StatesGroup):
    waiting_date = State()
    waiting_time = State()
    waiting_text = State()

class SpaOnDateFlow(StatesGroup):
    waiting_date = State()

class SpaDeleteBookingFlow(StatesGroup):
    waiting_id = State()

class FolioAddFlow(StatesGroup):
    waiting_room = State()
    waiting_amount = State()
    waiting_note = State()

class FolioViewFlow(StatesGroup):
    waiting_room = State()

# ---------------- DB ----------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS pins (
            role TEXT PRIMARY KEY,
            pin_hash TEXT NOT NULL
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            text TEXT NOT NULL,
            reminded INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT NOT NULL
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS folio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT NOT NULL,
            category TEXT NOT NULL,
            amount INTEGER NOT NULL,
            note TEXT,
            created_by_role TEXT NOT NULL,
            created_by_user INTEGER,
            created_at TEXT NOT NULL
        )""")
        await db.commit()

    # pins
    if not (is_10_digits(PIN_RECEPTION) and is_10_digits(PIN_SPA)):
        raise RuntimeError("PIN_RECEPTION и PIN_SPA должны быть ровно 10 цифр (Environment Variables в Render).")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO pins(role,pin_hash) VALUES(?,?)", (ROLE_RECEPTION, sha256(PIN_RECEPTION)))
        await db.execute("INSERT OR REPLACE INTO pins(role,pin_hash) VALUES(?,?)", (ROLE_SPA, sha256(PIN_SPA)))
        await db.commit()

async def get_role(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT role FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

async def set_role(user_id: int, role: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id, role, created_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET role=excluded.role",
            (user_id, role, now_tz().isoformat()),
        )
        await db.commit()

async def clear_role(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        await db.commit()

async def check_pin(pin: str) -> str | None:
    h = sha256(pin.strip())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT role FROM pins WHERE pin_hash=?", (h,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

async def get_all_admin_users():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, role FROM users") as cur:
            return await cur.fetchall()

async def add_booking(dt: datetime, text: str, created_by: int | None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO bookings(ts,text,reminded,created_by,created_at) VALUES(?,?,0,?,?)",
            (dt.isoformat(), text, created_by, now_tz().isoformat()),
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
            "SELECT id, ts, text FROM bookings WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
            (start.isoformat(), end.isoformat()),
        ) as cur:
            return await cur.fetchall()

async def list_bookings_for_reminder(ws: datetime, we: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, ts, text, reminded FROM bookings WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
            (ws.isoformat(), we.isoformat()),
        ) as cur:
            return await cur.fetchall()

async def mark_booking_reminded(bid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE bookings SET reminded=1 WHERE id=?", (bid,))
        await db.commit()

async def folio_add(room: str, category: str, amount: int, note: str | None, created_by_role: str, created_by_user: int | None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO folio(room,category,amount,note,created_by_role,created_by_user,created_at) VALUES(?,?,?,?,?,?,?)",
            (room, category, amount, note, created_by_role, created_by_user, now_tz().isoformat()),
        )
        await db.commit()
        return cur.lastrowid

async def folio_list_by_room(room: str, limit: int = 50):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, category, amount, note, created_by_role, created_at FROM folio WHERE room=? ORDER BY created_at DESC LIMIT ?",
            (room, limit),
        ) as cur:
            return await cur.fetchall()

async def folio_total_by_room(room: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(SUM(amount),0) FROM folio WHERE room=?", (room,)) as cur:
            row = await cur.fetchone()
            return int(row[0] or 0)

# ---------------- NOTIFICATIONS ----------------
async def send_today_bookings(bot: Bot):
    admins = await get_all_admin_users()
    if not admins:
        return
    today = now_tz()
    start, end = day_range(today)
    rows = await list_bookings_between(start, end)

    if not rows:
        msg = "📅 Сегодня броней SPA нет ✅"
    else:
        lines = ["📅 Брони SPA на сегодня:"]
        for bid, ts, txt in rows:
            dt = datetime.fromisoformat(ts)
            lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {txt}")
        msg = "\n".join(lines)

    for uid, _role in admins:
        try:
            await bot.send_message(int(uid), msg)
        except Exception:
            pass

async def send_one_hour_reminders(bot: Bot):
    admins = await get_all_admin_users()
    if not admins:
        return

    now = now_tz()
    ws = now + timedelta(minutes=59)
    we = now + timedelta(minutes=61)

    rows = await list_bookings_for_reminder(ws, we)
    for bid, ts, txt, reminded in rows:
        if reminded:
            continue
        dt = datetime.fromisoformat(ts)
        msg = f"⏰ Через 1 час бронь SPA: {fmt_dt(dt)} — {txt} (#{bid})"
        for uid, _role in admins:
            try:
                await bot.send_message(int(uid), msg)
            except Exception:
                pass
        await mark_booking_reminded(int(bid))

# ---------------- BOT ----------------
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN пустой. Добавь BOT_TOKEN в Environment Variables Render.")

    await init_db()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    scheduler = AsyncIOScheduler(timezone=TZ)
    mt = parse_hhmm(MORNING_TIME)
    scheduler.add_job(send_today_bookings, "cron", hour=mt.hour, minute=mt.minute, args=[bot], id="morning", replace_existing=True)
    scheduler.add_job(send_one_hour_reminders, "interval", minutes=1, args=[bot], id="reminders", replace_existing=True)
    scheduler.start()

    async def show_menu(m: Message):
        role = await get_role(m.from_user.id)
        if role == ROLE_RECEPTION:
            await m.answer("✅ Панель: Ресепшен", reply_markup=kb_reception())
        elif role == ROLE_SPA:
            await m.answer("✅ Панель: SPA", reply_markup=kb_spa())
        else:
            await m.answer("🔐 Нажмите кнопку и введите 10-значный код.", reply_markup=kb_auth())

    @dp.message(Command("start"))
    async def start(m: Message, state: FSMContext):
        await state.clear()
        await show_menu(m)

    @dp.message(F.text == "🚪 Выйти")
    async def logout(m: Message, state: FSMContext):
        await state.clear()
        await clear_role(m.from_user.id)
        await m.answer("Вы вышли ✅", reply_markup=kb_auth())

    @dp.message(F.text == "🔐 Войти (код)")
    async def auth_btn(m: Message, state: FSMContext):
        await state.clear()
        await state.set_state(AuthFlow.waiting_pin)
        await m.answer("Введите 10-значный код (10 цифр):")

    @dp.message(AuthFlow.waiting_pin)
    async def auth_pin(m: Message, state: FSMContext):
        pin = (m.text or "").strip()
        if not is_10_digits(pin):
            await m.answer("❌ Нужно ровно 10 цифр. Введите ещё раз:")
            return
        role = await check_pin(pin)
        if not role:
            await m.answer("❌ Неверный код. Попробуйте ещё раз:")
            return
        await set_role(m.from_user.id, role)
        await state.clear()
        await show_menu(m)

    # --------- FOLIO ADD (reception restaurant/laundry, spa can add spa) ---------
    async def start_folio_add(m: Message, state: FSMContext, category: str):
        role = await get_role(m.from_user.id)
        if role not in (ROLE_RECEPTION, ROLE_SPA):
            return await show_menu(m)

        if category in (CAT_RESTAURANT, CAT_LAUNDRY) and role != ROLE_RECEPTION:
            await m.answer("❌ Это может делать только Ресепшен.", reply_markup=kb_spa())
            return

        await state.clear()
        await state.set_state(FolioAddFlow.waiting_room)
        await state.update_data(category=category, created_by_role=role)
        await m.answer("Введите номер комнаты (только цифры), например 101:")

    @dp.message(F.text == "➕ Счёт: Ресторан")
    async def add_rest(m: Message, state: FSMContext):
        await start_folio_add(m, state, CAT_RESTAURANT)

    @dp.message(F.text == "➕ Счёт: Прачка")
    async def add_laundry(m: Message, state: FSMContext):
        await start_folio_add(m, state, CAT_LAUNDRY)

    @dp.message(F.text == "➕ Счёт: SPA (фолио)")
    async def add_spa_folio(m: Message, state: FSMContext):
        await start_folio_add(m, state, CAT_SPA)

    @dp.message(FolioAddFlow.waiting_room)
    async def folio_room(m: Message, state: FSMContext):
        try:
            room = parse_room(m.text)
        except Exception:
            await m.answer("❌ Неверный номер комнаты. Введите только цифры:")
            return
        await state.update_data(room=room)
        await state.set_state(FolioAddFlow.waiting_amount)
        await m.answer("Введите сумму (только цифры), например 50000:")

    @dp.message(FolioAddFlow.waiting_amount)
    async def folio_amount(m: Message, state: FSMContext):
        try:
            amount = parse_amount(m.text)
        except Exception:
            await m.answer("❌ Неверная сумма. Введите только цифры:")
            return
        await state.update_data(amount=amount)
        await state.set_state(FolioAddFlow.waiting_note)
        await m.answer("Комментарий (или '-' если не нужно):")

    @dp.message(FolioAddFlow.waiting_note)
    async def folio_note(m: Message, state: FSMContext):
        data = await state.get_data()
        category = data["category"]
        room = data["room"]
        amount = int(data["amount"])
        role = data["created_by_role"]

        note = (m.text or "").strip()
        if note == "-" or note == "":
            note = None

        fid = await folio_add(room, category, amount, note, role, m.from_user.id)
        total = await folio_total_by_room(room)
        await state.clear()

        await m.answer(
            f"✅ Добавлено (#{fid})\nКомната: {room}\nКатегория: {category}\nСумма: {amount}\nИтого по комнате: {total}",
            reply_markup=kb_reception() if role == ROLE_RECEPTION else kb_spa(),
        )

    # --------- FOLIO VIEW ---------
    @dp.message(F.text == "📄 Фолио по номеру")
    async def folio_view_btn(m: Message, state: FSMContext):
        role = await get_role(m.from_user.id)
        if role not in (ROLE_RECEPTION, ROLE_SPA):
            return await show_menu(m)
        await state.clear()
        await state.set_state(FolioViewFlow.waiting_room)
        await m.answer("Введите номер комнаты:")

    @dp.message(FolioViewFlow.waiting_room)
    async def folio_view_room(m: Message, state: FSMContext):
        role = await get_role(m.from_user.id)
        if role not in (ROLE_RECEPTION, ROLE_SPA):
            await state.clear()
            return await show_menu(m)

        try:
            room = parse_room(m.text)
        except Exception:
            await m.answer("❌ Неверный номер. Введите только цифры:")
            return

        rows = await folio_list_by_room(room, limit=50)
        total = await folio_total_by_room(room)
        await state.clear()

        if not rows:
            await m.answer(f"Комната {room}: записей нет.\nИтого: {total}",
                           reply_markup=kb_reception() if role == ROLE_RECEPTION else kb_spa())
            return

        lines = [f"📄 Фолио комнаты {room} (итого: {total})"]
        for fid, cat, amt, note, by_role, created_at in rows:
            dt = datetime.fromisoformat(created_at).astimezone(TZ)
            extra = f" — {note}" if note else ""
            lines.append(f"#{fid} | {cat} | {amt} | {dt.strftime('%d.%m %H:%M')} ({by_role}){extra}")
        await m.answer("\n".join(lines),
                       reply_markup=kb_reception() if role == ROLE_RECEPTION else kb_spa())

    # --------- SPA BOOKINGS ---------
    @dp.message(F.text == "➕ Добавить бронь")
    async def spa_add_booking_btn(m: Message, state: FSMContext):
        role = await get_role(m.from_user.id)
        if role != ROLE_SPA:
            return await show_menu(m)
        await state.clear()
        await state.set_state(SpaAddBookingFlow.waiting_date)
        await m.answer("Введите дату ДД.ММ (например 20.02):")

    @dp.message(SpaAddBookingFlow.waiting_date)
    async def spa_add_date(m: Message, state: FSMContext):
        role = await get_role(m.from_user.id)
        if role != ROLE_SPA:
            await state.clear()
            return await show_menu(m)

        ddmm = (m.text or "").strip()
        try:
            d, mo = parse_ddmm(ddmm)
        except Exception:
            await m.answer("❌ Неверная дата. Пример: 20.02")
            return

        n = now_tz()
        day = datetime(n.year, mo, d, 0, 0, tzinfo=TZ)
        if day.date() < n.date():
            await m.answer("⚠️ Прошедшую дату нельзя. Введите снова:")
            return

        await state.update_data(ddmm=ddmm)
        await state.set_state(SpaAddBookingFlow.waiting_time)
        await m.answer("Введите время ЧЧ:ММ (например 14:00):")

    @dp.message(SpaAddBookingFlow.waiting_time)
    async def spa_add_time(m: Message, state: FSMContext):
        role = await get_role(m.from_user.id)
        if role != ROLE_SPA:
            await state.clear()
            return await show_menu(m)

        hhmm = (m.text or "").strip()
        try:
            tm = parse_hhmm(hhmm)
        except Exception:
            await m.answer("❌ Неверное время. Пример: 14:00")
            return

        if not in_working_hours(tm):
            await m.answer("⚠️ Рабочие часы 09:00–22:00. Введите время снова:")
            return

        data = await state.get_data()
        dt = make_dt_current_year(data["ddmm"], hhmm)
        if dt <= now_tz():
            await m.answer("⚠️ Прошедшее время нельзя. Введите время снова:")
            return

        await state.update_data(hhmm=hhmm)
        await state.set_state(SpaAddBookingFlow.waiting_text)
        await m.answer("Введите текст брони (например: Массаж; Имя; Комната 120):")

    @dp.message(SpaAddBookingFlow.waiting_text)
    async def spa_add_text(m: Message, state: FSMContext):
        role = await get_role(m.from_user.id)
        if role != ROLE_SPA:
            await state.clear()
            return await show_menu(m)

        text = (m.text or "").strip()
        if not text:
            await m.answer("❌ Текст пустой. Введите снова:")
            return

        data = await state.get_data()
        dt = make_dt_current_year(data["ddmm"], data["hhmm"])
        if dt <= now_tz():
            await state.clear()
            await m.answer("⚠️ Прошедшее время нельзя. Начните заново.")
            return

        bid = await add_booking(dt, text, m.from_user.id)
        await state.clear()
        await m.answer(f"✅ Бронь добавлена: #{bid} — {fmt_dt(dt)} — {text}", reply_markup=kb_spa())

    @dp.message(F.text == "📅 Сегодня")
    async def spa_today(m: Message):
        role = await get_role(m.from_user.id)
        if role != ROLE_SPA:
            return await show_menu(m)
        today = now_tz()
        start, end = day_range(today)
        rows = await list_bookings_between(start, end)
        if not rows:
            await m.answer("Сегодня броней нет ✅", reply_markup=kb_spa())
            return
        lines = ["📅 Брони на сегодня:"]
        for bid, ts, txt in rows:
            dt = datetime.fromisoformat(ts)
            lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {txt}")
        await m.answer("\n".join(lines), reply_markup=kb_spa())

    @dp.message(F.text == "📆 Брони на дату")
    async def spa_on_date_btn(m: Message, state: FSMContext):
        role = await get_role(m.from_user.id)
        if role != ROLE_SPA:
            return await show_menu(m)
        await state.clear()
        await state.set_state(SpaOnDateFlow.waiting_date)
        await m.answer("Введите дату ДД.ММ (например 20.02):")

    @dp.message(SpaOnDateFlow.waiting_date)
    async def spa_on_date(m: Message, state: FSMContext):
        role = await get_role(m.from_user.id)
        if role != ROLE_SPA:
            await state.clear()
            return await show_menu(m)

        ddmm = (m.text or "").strip()
        try:
            d, mo = parse_ddmm(ddmm)
        except Exception:
            await m.answer("❌ Неверная дата. Пример: 20.02")
            return

        n = now_tz()
        day = datetime(n.year, mo, d, 0, 0, tzinfo=TZ)
        start, end = day_range(day)
        rows = await list_bookings_between(start, end)
        await state.clear()

        if not rows:
            await m.answer(f"На {ddmm} броней нет ✅", reply_markup=kb_spa())
            return

        lines = [f"📆 Брони на {ddmm}:"]
        for bid, ts, txt in rows:
            dt = datetime.fromisoformat(ts)
            lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {txt}")
        await m.answer("\n".join(lines), reply_markup=kb_spa())

    @dp.message(F.text == "🗑 Удалить бронь")
    async def spa_delete_btn(m: Message, state: FSMContext):
        role = await get_role(m.from_user.id)
        if role != ROLE_SPA:
            return await show_menu(m)
        await state.clear()
        await state.set_state(SpaDeleteBookingFlow.waiting_id)
        await m.answer("Введите ID брони (например 12):")

    @dp.message(SpaDeleteBookingFlow.waiting_id)
    async def spa_delete_id(m: Message, state: FSMContext):
        role = await get_role(m.from_user.id)
        if role != ROLE_SPA:
            await state.clear()
            return await show_menu(m)

        txt = (m.text or "").strip()
        if not txt.isdigit():
            await m.answer("❌ Нужно число. Например 12:")
            return
        ok = await delete_booking(int(txt))
        await state.clear()
        await m.answer("✅ Удалено" if ok else "⚠️ Не найдено", reply_markup=kb_spa())

    @dp.message()
    async def fallback(m: Message):
        await show_menu(m)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())