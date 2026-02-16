import os
import re
import asyncio
from datetime import datetime, timedelta, time as dtime

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# Mini web server for Render (open port)
from aiohttp import web

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TZ_NAME = os.getenv("TZ", "Asia/Tashkent").strip()
MORNING_TIME_DEFAULT = os.getenv("MORNING_TIME", "09:00").strip()
PORT = int(os.getenv("PORT", "10000"))

TZ = ZoneInfo(TZ_NAME)
DB_PATH = "bookings.db"

SPA_PHONE = "+998916768900"


# ---------- Helpers ----------
def is_admin(msg: Message) -> bool:
    return bool(msg.from_user and msg.from_user.id == ADMIN_ID)

def parse_hhmm(s: str) -> dtime:
    s = s.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", s):
        raise ValueError("Bad HH:MM format")
    hh, mm = map(int, s.split(":"))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("Bad HH:MM value")
    return dtime(hour=hh, minute=mm)

def parse_ddmm(s: str):
    s = s.strip()
    if not re.match(r"^\d{1,2}\.\d{1,2}$", s):
        raise ValueError("Bad DD.MM format")
    d, m = map(int, s.split("."))
    if not (1 <= d <= 31 and 1 <= m <= 12):
        raise ValueError("Bad DD.MM value")
    return d, m

def make_dt(ddmm: str, hhmm: str) -> datetime:
    day, month = parse_ddmm(ddmm)
    tm = parse_hhmm(hhmm)
    now = datetime.now(TZ)
    year = now.year
    dt = datetime(year, month, day, tm.hour, tm.minute, tzinfo=TZ)
    # if date already passed -> next year
    if dt < now - timedelta(days=1):
        dt = dt.replace(year=year + 1)
    return dt

def day_range(day: datetime):
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=TZ)
    end = start + timedelta(days=1)
    return start, end


# ---------- DB ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,               -- ISO datetime with TZ
            text TEXT NOT NULL,
            reminded INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """)
        # ВАЖНО: больше НЕ создаём UNIQUE индекс на ts (разрешаем одинаковое время)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,               -- requested time
            text TEXT NOT NULL,             -- client text: service/name/phone/etc
            client_id INTEGER NOT NULL,     -- telegram user id
            chat_id INTEGER NOT NULL,       -- where to reply
            status TEXT NOT NULL,           -- pending/confirmed/declined
            booking_id INTEGER,             -- filled when confirmed
            created_at TEXT NOT NULL
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)
        await db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('morning_time', ?)",
            (MORNING_TIME_DEFAULT,)
        )
        await db.commit()

async def get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else ""

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        await db.commit()

async def add_booking(dt: datetime, info: str):
    # Всегда добавляем (даже если одинаковое время)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO bookings(ts, text, reminded, created_at) VALUES(?,?,0,?)",
            (dt.isoformat(), info, datetime.now(TZ).isoformat())
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
            (start.isoformat(), end.isoformat())
        ) as cur:
            return await cur.fetchall()

async def create_request(dt: datetime, text: str, client_id: int, chat_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO requests(ts, text, client_id, chat_id, status, created_at) VALUES(?,?,?,?, 'pending', ?)",
            (dt.isoformat(), text, client_id, chat_id, datetime.now(TZ).isoformat())
        )
        await db.commit()
        return cur.lastrowid

async def get_request(req_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, ts, text, client_id, chat_id, status, booking_id FROM requests WHERE id=?",
            (req_id,)
        ) as cur:
            return await cur.fetchone()

async def set_request_status(req_id: int, status: str, booking_id: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE requests SET status=?, booking_id=? WHERE id=?",
            (status, booking_id, req_id)
        )
        await db.commit()


# ---------- Notifications (ONLY confirmed/admin-added bookings) ----------
async def send_today_summary(bot: Bot):
    today = datetime.now(TZ)
    start, end = day_range(today)
    rows = await list_bookings_between(start, end)
    if not rows:
        await bot.send_message(ADMIN_ID, "Сегодня броней нет ✅")
        return
    lines = ["📅 Брони на сегодня:"]
    for bid, ts, txt in rows:
        dt = datetime.fromisoformat(ts)
        lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {txt}")
    await bot.send_message(ADMIN_ID, "\n".join(lines))

async def send_one_hour_reminders(bot: Bot):
    now = datetime.now(TZ)
    window_start = now + timedelta(minutes=59)
    window_end = now + timedelta(minutes=61)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT id, ts, text FROM bookings
            WHERE reminded=0 AND ts >= ? AND ts < ?
            ORDER BY ts ASC
        """, (window_start.isoformat(), window_end.isoformat())) as cur:
            rows = await cur.fetchall()

        for bid, ts, txt in rows:
            dt = datetime.fromisoformat(ts)
            await bot.send_message(
                ADMIN_ID,
                f"⏰ Через 1 час: {dt.strftime('%d.%m %H:%M')} — {txt} (#{bid})"
            )
            await db.execute("UPDATE bookings SET reminded=1 WHERE id=?", (bid,))

        await db.commit()


# ---------- Render web server ----------
async def handle_root(_request):
    return web.Response(text="OK")

async def handle_healthz(_request):
    return web.Response(text="OK")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/healthz", handle_healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()


# ---------- Keyboards ----------
def admin_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить бронь"), KeyboardButton(text="📅 Сегодня")],
            [KeyboardButton(text="📆 На дату"), KeyboardButton(text="🗑 Удалить")],
            [KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True
    )

def client_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📅 Записаться")]],
        resize_keyboard=True
    )

def cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )

def req_inline_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"req:ok:{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"req:no:{req_id}"),
        ]
    ])


# ---------- FSM ----------
class AdminAddFlow(StatesGroup):
    waiting_date = State()
    waiting_time = State()
    waiting_text = State()

class ClientFlow(StatesGroup):
    waiting_date = State()
    waiting_time = State()
    waiting_text = State()


# ---------- Messages RU/UZ ----------
WAIT_TEXT = (
    "✅ Ваша заявка отправлена.\n"
    "⏳ Ожидайте подтверждение администратора.\n"
    f"☎️ Телефон SPA: {SPA_PHONE}\n\n"
    "✅ Arizangiz yuborildi.\n"
    "⏳ Administrator tasdig‘ini kuting.\n"
    f"☎️ SPA telefoni: {SPA_PHONE}"
)

CONFIRMED_TEXT = (
    "✅ Ваша бронь подтверждена!\n"
    f"☎️ Телефон SPA: {SPA_PHONE}\n\n"
    "✅ Band qilishingiz tasdiqlandi!\n"
    f"☎️ SPA telefoni: {SPA_PHONE}"
)

DECLINED_TEXT = (
    "❌ К сожалению, бронь отклонена.\n"
    f"☎️ Телефон SPA: {SPA_PHONE}\n\n"
    "❌ Afsuski, band qilish rad etildi.\n"
    f"☎️ SPA telefoni: {SPA_PHONE}"
)


# ---------- Bot ----------
async def run_bot():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set BOT_TOKEN env var.")
    if ADMIN_ID == 0:
        raise RuntimeError("ADMIN_ID is 0. Set ADMIN_ID env var.")

    await init_db()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Scheduler
    scheduler = AsyncIOScheduler(timezone=TZ)

    def schedule_morning_job(morning_hhmm: str):
        try:
            scheduler.remove_job("morning_summary")
        except Exception:
            pass
        mt = parse_hhmm(morning_hhmm)
        scheduler.add_job(
            send_today_summary,
            "cron",
            id="morning_summary",
            hour=mt.hour,
            minute=mt.minute,
            args=[bot],
            replace_existing=True
        )

    scheduler.add_job(send_one_hour_reminders, "interval", minutes=1, args=[bot])

    morning_time = (await get_setting("morning_time")) or MORNING_TIME_DEFAULT
    try:
        parse_hhmm(morning_time)
    except Exception:
        morning_time = MORNING_TIME_DEFAULT
        await set_setting("morning_time", morning_time)

    schedule_morning_job(morning_time)
    scheduler.start()

    # ----------------- START -----------------
    @dp.message(Command("start"))
    async def cmd_start(m: Message, state: FSMContext):
        await state.clear()
        if is_admin(m):
            await m.answer(
                "✅ Админ-режим.\n\n"
                "Кнопки 👇\n"
                "ℹ️ Теперь разрешены одинаковые времена старта (если нужно).",
                reply_markup=admin_kb()
            )
        else:
            await m.answer(
                "Здравствуйте! 👋\n"
                "Нажмите «📅 Записаться», чтобы отправить заявку.\n\n"
                "Assalomu alaykum! 👋\n"
                "«📅 Yozilish» tugmasini bosing.",
                reply_markup=client_kb()
            )

    # ----------------- ADMIN COMMANDS -----------------
    @dp.message(Command("today"))
    async def cmd_today(m: Message):
        if not is_admin(m):
            return
        today = datetime.now(TZ)
        start, end = day_range(today)
        rows = await list_bookings_between(start, end)
        if not rows:
            await m.answer("Сегодня броней нет ✅", reply_markup=admin_kb())
            return
        lines = ["📅 Брони на сегодня:"]
        for bid, ts, txt in rows:
            dt = datetime.fromisoformat(ts)
            lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {txt}")
        await m.answer("\n".join(lines), reply_markup=admin_kb())

    @dp.message(Command("list"))
    async def cmd_list(m: Message):
        if not is_admin(m):
            return
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) != 2:
            await m.answer("Формат: /list 20.02", reply_markup=admin_kb())
            return
        ddmm = parts[1].strip()
        try:
            d, mo = parse_ddmm(ddmm)
            now = datetime.now(TZ)
            target = datetime(now.year, mo, d, 0, 0, tzinfo=TZ)
        except Exception:
            await m.answer("Неверная дата. Пример: /list 20.02", reply_markup=admin_kb())
            return

        start, end = day_range(target)
        rows = await list_bookings_between(start, end)
        if not rows:
            await m.answer(f"На {ddmm} броней нет ✅", reply_markup=admin_kb())
            return

        lines = [f"📅 Брони на {ddmm}:"]
        for bid, ts, txt in rows:
            dt = datetime.fromisoformat(ts)
            lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {txt}")
        await m.answer("\n".join(lines), reply_markup=admin_kb())

    @dp.message(Command("del"))
    async def cmd_del(m: Message):
        if not is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit():
            await m.answer("Формат: /del 12", reply_markup=admin_kb())
            return
        ok = await delete_booking(int(parts[1]))
        await m.answer("🗑 Удалено" if ok else "Не найдено", reply_markup=admin_kb())

    @dp.message(Command("time"))
    async def cmd_time(m: Message):
        if not is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) != 2:
            await m.answer("Формат: /time 09:00", reply_markup=admin_kb())
            return
        try:
            mt = parts[1].strip()
            parse_hhmm(mt)
        except Exception:
            await m.answer("Неверное время. Пример: /time 09:00", reply_markup=admin_kb())
            return
        await set_setting("morning_time", mt)
        schedule_morning_job(mt)
        await m.answer(f"✅ Утренний отчёт теперь в {mt}", reply_markup=admin_kb())

    @dp.message(Command("add"))
    async def cmd_add(m: Message):
        if not is_admin(m):
            return
        mm = re.match(r"^/add\s+(\d{1,2}\.\d{1,2})\s+(\d{1,2}:\d{2})\s+(.+)$", (m.text or "").strip())
        if not mm:
            await m.answer("Формат: /add 20.02 14:00 Текст", reply_markup=admin_kb())
            return
        ddmm, hhmm, text = mm.group(1), mm.group(2), mm.group(3)
        try:
            dt = make_dt(ddmm, hhmm)
        except Exception:
            await m.answer("Неверная дата/время. Пример: /add 20.02 14:00 Текст", reply_markup=admin_kb())
            return

        bid = await add_booking(dt, text)
        await m.answer(f"✅ Добавлено: #{bid} — {dt.strftime('%d.%m %H:%M')} — {text}", reply_markup=admin_kb())

    # ----------------- ADMIN BUTTONS -----------------
    @dp.message(F.text == "📅 Сегодня")
    async def admin_today_btn(m: Message):
        if is_admin(m):
            await cmd_today(m)

    @dp.message(F.text == "📆 На дату")
    async def admin_list_btn(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        await state.clear()
        await state.update_data(mode="admin_list")
        await state.set_state(AdminAddFlow.waiting_date)
        await m.answer("Введите дату ДД.ММ (например 20.02) или ❌ Отмена", reply_markup=cancel_kb())

    @dp.message(F.text == "🗑 Удалить")
    async def admin_del_btn(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        await state.clear()
        await state.update_data(mode="admin_delete")
        await m.answer("Введите ID брони для удаления (пример: 12) или ❌ Отмена", reply_markup=cancel_kb())

    @dp.message(F.text == "➕ Добавить бронь")
    async def admin_add_btn(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        await state.clear()
        await state.update_data(mode="admin_add")
        await state.set_state(AdminAddFlow.waiting_date)
        await m.answer("Введите дату ДД.ММ (например 20.02) или ❌ Отмена", reply_markup=cancel_kb())

    @dp.message(F.text == "ℹ️ Помощь")
    async def admin_help_btn(m: Message):
        if not is_admin(m):
            return
        await m.answer(
            "Админ команды:\n"
            "/add ДД.ММ ЧЧ:ММ текст\n"
            "/today\n"
            "/list ДД.ММ\n"
            "/del ID\n"
            "/time HH:MM\n\n"
            "Заявки от клиентов приходят с кнопками ✅/❌.\n"
            "ℹ️ Одинаковое время старта разрешено.",
            reply_markup=admin_kb()
        )

    # ----------------- CLIENT MODE (REQUESTS) -----------------
    @dp.message(F.text == "📅 Записаться")
    async def client_book_btn(m: Message, state: FSMContext):
        if is_admin(m):
            return
        await state.clear()
        await state.set_state(ClientFlow.waiting_date)
        await m.answer("Введите дату ДД.ММ (например 20.02) или ❌ Отмена", reply_markup=cancel_kb())

    @dp.message(F.text == "❌ Отмена")
    async def cancel_any(m: Message, state: FSMContext):
        await state.clear()
        if is_admin(m):
            await m.answer("Ок, отменено ✅", reply_markup=admin_kb())
        else:
            await m.answer("Ок ✅", reply_markup=client_kb())

    @dp.message(ClientFlow.waiting_date)
    async def client_date(m: Message, state: FSMContext):
        if is_admin(m):
            return
        txt = (m.text or "").strip()
        try:
            parse_ddmm(txt)
        except Exception:
            await m.answer("Неверная дата. Пример: 20.02\n\nNoto‘g‘ri sana. Masalan: 20.02")
            return
        await state.update_data(ddmm=txt)
        await state.set_state(ClientFlow.waiting_time)
        await m.answer("Введите время ЧЧ:ММ (например 14:00)\n\nVaqtni kiriting (masalan 14:00)")

    @dp.message(ClientFlow.waiting_time)
    async def client_time(m: Message, state: FSMContext):
        if is_admin(m):
            return
        txt = (m.text or "").strip()
        try:
            parse_hhmm(txt)
        except Exception:
            await m.answer("Неверное время. Пример: 14:00\n\nNoto‘g‘ri vaqt. Masalan: 14:00")
            return
        await state.update_data(hhmm=txt)
        await state.set_state(ClientFlow.waiting_text)
        await m.answer(
            "Напишите текст заявки (услуга, имя, телефон)\n"
            "Пример: Массаж, Алишер, +998...\n\n"
            "Ariza matnini yozing (xizmat, ism, telefon)"
        )

    @dp.message(ClientFlow.waiting_text)
    async def client_text(m: Message, state: FSMContext):
        if is_admin(m):
            return
        text = (m.text or "").strip()
        if not text:
            await m.answer("Текст не должен быть пустым.\n\nMatn bo‘sh bo‘lmasin.")
            return

        data = await state.get_data()
        ddmm, hhmm = data.get("ddmm"), data.get("hhmm")

        try:
            dt = make_dt(ddmm, hhmm)
        except Exception:
            await state.clear()
            await m.answer("Ошибка даты/времени. Попробуйте снова.", reply_markup=client_kb())
            return

        req_id = await create_request(dt, text, m.from_user.id, m.chat.id)
        await state.clear()

        # client: wait message RU+UZ + phone
        await m.answer(WAIT_TEXT, reply_markup=client_kb())

        # admin: request + approve/decline buttons
        admin_msg = (
            f"🆕 Заявка #{req_id}\n"
            f"🕒 {dt.strftime('%d.%m %H:%M')}\n"
            f"👤 Клиент: {m.from_user.full_name} (id {m.from_user.id})\n"
            f"📝 {text}\n\n"
            f"ℹ️ Одинаковое время старта разрешено."
        )
        await bot.send_message(ADMIN_ID, admin_msg, reply_markup=req_inline_kb(req_id))

    # ----------------- ADMIN CONFIRM/DECLINE -----------------
    @dp.callback_query(F.data.startswith("req:"))
    async def req_action(cb: CallbackQuery):
        if not (cb.from_user and cb.from_user.id == ADMIN_ID):
            await cb.answer("Нет доступа", show_alert=True)
            return

        parts = (cb.data or "").split(":")
        if len(parts) != 3:
            await cb.answer("Ошибка кнопки", show_alert=True)
            return

        action, req_id_s = parts[1], parts[2]
        if not req_id_s.isdigit():
            await cb.answer("Ошибка id", show_alert=True)
            return

        req_id = int(req_id_s)
        row = await get_request(req_id)
        if not row:
            await cb.answer("Заявка не найдена", show_alert=True)
            return

        _id, ts, text, client_id, chat_id, status, booking_id = row
        dt = datetime.fromisoformat(ts)

        if status != "pending":
            await cb.answer("Уже обработано", show_alert=True)
            return

        if action == "ok":
            bid = await add_booking(dt, text)
            await set_request_status(req_id, "confirmed", int(bid))
            await cb.message.answer(f"✅ Заявка #{req_id} подтверждена. Создана бронь #{bid} на {dt.strftime('%d.%m %H:%M')}.")
            await cb.answer("Подтверждено ✅")
            await bot.send_message(chat_id, CONFIRMED_TEXT + f"\n\n📅 {dt.strftime('%d.%m %H:%M')}")
            return

        if action == "no":
            await set_request_status(req_id, "declined", None)
            await cb.message.answer(f"❌ Заявка #{req_id} отклонена.")
            await cb.answer("Отклонено ❌")
            await bot.send_message(chat_id, DECLINED_TEXT)
            return

        await cb.answer("Неизвестное действие", show_alert=True)

    # ----------------- ADMIN LIST/ADD wizard + ADMIN DELETE mode -----------------
    @dp.message(AdminAddFlow.waiting_date)
    async def admin_flow_date(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        txt = (m.text or "").strip()
        data = await state.get_data()
        mode = data.get("mode")

        if mode not in ("admin_add", "admin_list"):
            await state.clear()
            await m.answer("Режим сброшен. /start", reply_markup=admin_kb())
            return

        try:
            parse_ddmm(txt)
        except Exception:
            await m.answer("Неверная дата. Пример: 20.02")
            return

        if mode == "admin_list":
            await state.clear()
            d, mo = parse_ddmm(txt)
            now = datetime.now(TZ)
            target = datetime(now.year, mo, d, 0, 0, tzinfo=TZ)
            start, end = day_range(target)
            rows = await list_bookings_between(start, end)
            if not rows:
                await m.answer(f"На {txt} броней нет ✅", reply_markup=admin_kb())
                return
            lines = [f"📅 Брони на {txt}:"]
            for bid, ts, t2 in rows:
                dt = datetime.fromisoformat(ts)
                lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {t2}")
            await m.answer("\n".join(lines), reply_markup=admin_kb())
            return

        # admin_add flow continues
        await state.update_data(ddmm=txt)
        await state.set_state(AdminAddFlow.waiting_time)
        await m.answer("Введите время ЧЧ:ММ (например 14:00) или ❌ Отмена", reply_markup=cancel_kb())

    @dp.message(AdminAddFlow.waiting_time)
    async def admin_flow_time(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        txt = (m.text or "").strip()
        try:
            parse_hhmm(txt)
        except Exception:
            await m.answer("Неверное время. Пример: 14:00")
            return
        await state.update_data(hhmm=txt)
        await state.set_state(AdminAddFlow.waiting_text)
        await m.answer("Введите текст брони (услуга/имя/телефон) или ❌ Отмена", reply_markup=cancel_kb())

    @dp.message(AdminAddFlow.waiting_text)
    async def admin_flow_text(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        text = (m.text or "").strip()
        if not text:
            await m.answer("Текст не должен быть пустым.")
            return

        data = await state.get_data()
        ddmm, hhmm = data.get("ddmm"), data.get("hhmm")

        try:
            dt = make_dt(ddmm, hhmm)
        except Exception:
            await state.clear()
            await m.answer("Ошибка даты/времени. Начните заново.", reply_markup=admin_kb())
            return

        bid = await add_booking(dt, text)
        await state.clear()
        await m.answer(f"✅ Добавлено: #{bid} — {dt.strftime('%d.%m %H:%M')} — {text}", reply_markup=admin_kb())

    @dp.message()
    async def fallback(m: Message, state: FSMContext):
        data = await state.get_data()
        mode = data.get("mode")

        if mode == "admin_delete" and is_admin(m):
            txt = (m.text or "").strip()
            if not txt.isdigit():
                await m.answer("Введите ID цифрами (пример: 12) или ❌ Отмена", reply_markup=cancel_kb())
                return
            ok = await delete_booking(int(txt))
            await state.clear()
            await m.answer("🗑 Удалено" if ok else "Не найдено", reply_markup=admin_kb())
            return

        if is_admin(m):
            await m.answer("Нажмите кнопку или /start для меню.", reply_markup=admin_kb())
        else:
            await m.answer("Нажмите «📅 Записаться» или /start.", reply_markup=client_kb())

    await dp.start_polling(bot)


async def main():
    await asyncio.gather(
        run_web_server(),
        run_bot()
    )

if __name__ == "__main__":
    asyncio.run(main())
