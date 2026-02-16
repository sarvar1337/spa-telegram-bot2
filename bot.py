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

from aiohttp import web

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TZ_NAME = os.getenv("TZ", "Asia/Tashkent").strip()
MORNING_TIME_DEFAULT = os.getenv("MORNING_TIME", "09:00").strip()
PORT = int(os.getenv("PORT", "10000"))

TZ = ZoneInfo(TZ_NAME)
DB_PATH = "bookings.db"

SPA_PHONE = "+998916768900"  # номер вашего SPA

OPEN_TIME = dtime(9, 0)
CLOSE_TIME = dtime(22, 0)

ANTISPAM_MINUTES = 10
MY_REQUESTS_LIMIT = 10

SERVICES = {
    "🏊 Бассейн": "Бассейн",
    "🔥 Сауна": "Сауна",
    "💆 Массаж": "Массаж",
}


# ---------- Helpers ----------
def is_admin(msg: Message) -> bool:
    return bool(msg.from_user and msg.from_user.id == ADMIN_ID)

def parse_hhmm(s: str) -> dtime:
    s = s.strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", s):
        raise ValueError("Bad HH:MM format")
    hh, mm = map(int, s.split(":"))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("Bad HH:MM value")
    return dtime(hour=hh, minute=mm)

def parse_ddmm(s: str):
    s = s.strip()
    if not re.fullmatch(r"\d{1,2}\.\d{1,2}", s):
        raise ValueError("Bad DD.MM format")
    d, m = map(int, s.split("."))
    if not (1 <= d <= 31 and 1 <= m <= 12):
        raise ValueError("Bad DD.MM value")
    return d, m

def day_range(day: datetime):
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=TZ)
    end = start + timedelta(days=1)
    return start, end

def normalize_uz_phone(raw: str) -> str | None:
    """
    Accept:
      +998XXXXXXXXX
      998XXXXXXXXX
      XXXXXXXXX (9 digits)
    Return normalized: +998XXXXXXXXX
    """
    s = (raw or "").strip().replace(" ", "").replace("-", "")
    if re.fullmatch(r"\+998\d{9}", s):
        return s
    if re.fullmatch(r"998\d{9}", s):
        return "+" + s
    if re.fullmatch(r"\d{9}", s):
        return "+998" + s
    return None

def spa_phone_examples() -> tuple[str, str, str]:
    # from +998916768900 -> country 998 + last 9 digits
    digits = re.sub(r"\D", "", SPA_PHONE)
    if digits.startswith("998") and len(digits) == 12:
        local9 = digits[3:]
    elif len(digits) >= 9:
        local9 = digits[-9:]
        digits = "998" + local9
    else:
        local9 = "916768900"
        digits = "998" + local9
    return (f"+{digits}", digits, local9)

EX_PLUS, EX_998, EX_9 = spa_phone_examples()

def make_dt_current_year(ddmm: str, hhmm: str) -> datetime:
    d, mo = parse_ddmm(ddmm)
    t = parse_hhmm(hhmm)
    now = datetime.now(TZ)
    return datetime(now.year, mo, d, t.hour, t.minute, tzinfo=TZ)

def in_working_hours(t: dtime) -> bool:
    # 09:00..22:00 inclusive
    return OPEN_TIME <= t <= CLOSE_TIME


# ---------- DB ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
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
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            text TEXT NOT NULL,
            client_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            status TEXT NOT NULL,           -- pending/confirmed/declined
            booking_id INTEGER,
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

async def add_booking(dt: datetime, info: str) -> int:
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
            "SELECT id, ts, text, client_id, chat_id, status, booking_id, created_at FROM requests WHERE id=?",
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

async def list_pending_requests(limit: int = 50):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, ts, text, client_id, created_at FROM requests WHERE status='pending' ORDER BY created_at ASC LIMIT ?",
            (limit,)
        ) as cur:
            return await cur.fetchall()

async def list_requests_for_client(client_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, ts, text, status, booking_id, created_at FROM requests WHERE client_id=? ORDER BY created_at DESC LIMIT ?",
            (client_id, limit)
        ) as cur:
            return await cur.fetchall()

async def has_recent_request(client_id: int, minutes: int) -> int:
    """Return seconds left if still in cooldown else 0."""
    now = datetime.now(TZ)
    border = now - timedelta(minutes=minutes)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT created_at FROM requests WHERE client_id=? ORDER BY created_at DESC LIMIT 1",
            (client_id,)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return 0
    last = datetime.fromisoformat(row[0])
    if last >= border:
        left = int((last + timedelta(minutes=minutes) - now).total_seconds())
        return max(left, 1)
    return 0


# ---------- Notifications (ONLY bookings table) ----------
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
            [KeyboardButton(text="🕓 Ожидающие заявки"), KeyboardButton(text="📅 Сегодня")],
            [KeyboardButton(text="➕ Добавить бронь"), KeyboardButton(text="📆 На дату")],
            [KeyboardButton(text="🗑 Удалить"), KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True
    )

def client_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Записаться"), KeyboardButton(text="🧾 Мои заявки")],
            [KeyboardButton(text="☎️ Связаться с админом")],
        ],
        resize_keyboard=True
    )

def cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )

def services_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏊 Бассейн"), KeyboardButton(text="🔥 Сауна")],
            [KeyboardButton(text="💆 Массаж")],
            [KeyboardButton(text="❌ Отмена")],
        ],
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
    waiting_name = State()
    waiting_phone = State()
    waiting_service = State()


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

PHONE_FORMAT_TEXT = (
    "⚠️ Номер введён неверно.\n"
    "Введите заново в одном из форматов:\n"
    f"✅ {EX_PLUS}\n"
    f"✅ {EX_998}\n"
    f"✅ {EX_9}\n\n"
    "⚠️ Telefon raqami noto‘g‘ri.\n"
    "Quyidagi formatlardan birida kiriting:\n"
    f"✅ {EX_PLUS}\n"
    f"✅ {EX_998}\n"
    f"✅ {EX_9}"
)

PAST_DATE_TEXT = (
    "⚠️ Нельзя выбрать прошедшую дату/время. Введите заново.\n\n"
    "⚠️ O‘tgan sana/vaqtni tanlab bo‘lmaydi. Qayta kiriting."
)

WORK_HOURS_TEXT = (
    "⚠️ Мы работаем с 09:00 до 22:00. Введите время заново.\n\n"
    "⚠️ Ish vaqti 09:00 dan 22:00 gacha. Vaqtni qayta kiriting."
)

ANTISPAM_TEXT = (
    "⏳ Слишком часто. Можно отправлять заявку раз в 10 минут.\n"
    "Попробуйте чуть позже.\n\n"
    "⏳ Juda tez-tez. 10 daqiqada 1 marta ariza yuborish mumkin.\n"
    "Birozdan keyin urinib ko‘ring."
)

CONTACT_TEXT = (
    f"☎️ Связаться с администратором:\n{SPA_PHONE}\n\n"
    f"☎️ Administrator bilan bog‘lanish:\n{SPA_PHONE}"
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
            await m.answer("✅ Админ-режим.\nКнопки 👇", reply_markup=admin_kb())
        else:
            await m.answer(
                "Здравствуйте! 👋\nНажмите «📅 Записаться», чтобы отправить заявку.\n\n"
                "Assalomu alaykum! 👋\n«📅 Yozilish» tugmasini bosing.",
                reply_markup=client_kb()
            )

    # ----------------- COMMON BUTTONS -----------------
    @dp.message(F.text == "❌ Отмена")
    async def cancel_any(m: Message, state: FSMContext):
        await state.clear()
        if is_admin(m):
            await m.answer("Ок, отменено ✅", reply_markup=admin_kb())
        else:
            await m.answer("Ок ✅", reply_markup=client_kb())

    # ----------------- CLIENT: contact -----------------
    @dp.message(F.text == "☎️ Связаться с админом")
    async def client_contact(m: Message):
        if is_admin(m):
            return
        await m.answer(CONTACT_TEXT, reply_markup=client_kb())

    # ----------------- CLIENT: my requests -----------------
    @dp.message(F.text == "🧾 Мои заявки")
    async def client_my_requests(m: Message):
        if is_admin(m):
            return
        rows = await list_requests_for_client(m.from_user.id, MY_REQUESTS_LIMIT)
        if not rows:
            await m.answer(
                "У вас пока нет заявок.\n\nSizda hali arizalar yo‘q.",
                reply_markup=client_kb()
            )
            return

        def st_icon(st: str) -> str:
            return {"pending": "🕓", "confirmed": "✅", "declined": "❌"}.get(st, "•")

        lines = ["🧾 Ваши заявки (последние):", ""]

        for rid, ts, text, status, booking_id, created_at in rows:
            dt = datetime.fromisoformat(ts)
            lines.append(f"{st_icon(status)} #{rid} — {dt.strftime('%d.%m %H:%M')} — {text}")

        lines.append("\n☎️ SPA: " + SPA_PHONE)
        await m.answer("\n".join(lines), reply_markup=client_kb())

    # ----------------- ADMIN: pending list -----------------
    @dp.message(F.text == "🕓 Ожидающие заявки")
    async def admin_pending(m: Message):
        if not is_admin(m):
            return
        rows = await list_pending_requests(50)
        if not rows:
            await m.answer("Нет ожидающих заявок ✅", reply_markup=admin_kb())
            return

        await m.answer(f"🕓 Ожидающие заявки: {len(rows)}", reply_markup=admin_kb())
        # отправим по сообщениям (удобно нажимать ✅/❌)
        for rid, ts, text, client_id, created_at in rows:
            dt = datetime.fromisoformat(ts)
            msg = (
                f"🕓 Заявка #{rid}\n"
                f"🕒 {dt.strftime('%d.%m %H:%M')}\n"
                f"👤 client_id: {client_id}\n"
                f"📝 {text}"
            )
            await m.answer(msg, reply_markup=req_inline_kb(rid))

    # ----------------- ADMIN: today/list/add/del/time -----------------
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

    @dp.message(F.text == "📅 Сегодня")
    async def admin_today_btn(m: Message):
        if is_admin(m):
            await cmd_today(m)

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
            dt = make_dt_current_year(ddmm, hhmm)
        except Exception:
            await m.answer("Неверная дата/время. Пример: /add 20.02 14:00 Текст", reply_markup=admin_kb())
            return

        bid = await add_booking(dt, text)
        await m.answer(f"✅ Добавлено: #{bid} — {dt.strftime('%d.%m %H:%M')} — {text}", reply_markup=admin_kb())

    @dp.message(F.text == "📆 На дату")
    async def admin_list_btn(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        await state.clear()
        await state.update_data(mode="admin_list")
        await state.set_state(AdminAddFlow.waiting_date)
        await m.answer("Введите дату ДД.ММ (например 20.02) или ❌ Отмена", reply_markup=cancel_kb())

    @dp.message(F.text == "➕ Добавить бронь")
    async def admin_add_btn(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        await state.clear()
        await state.update_data(mode="admin_add")
        await state.set_state(AdminAddFlow.waiting_date)
        await m.answer("Введите дату ДД.ММ (например 20.02) или ❌ Отмена", reply_markup=cancel_kb())

    @dp.message(F.text == "🗑 Удалить")
    async def admin_del_btn(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        await state.clear()
        await state.update_data(mode="admin_delete")
        await m.answer("Введите ID брони для удаления (пример: 12) или ❌ Отмена", reply_markup=cancel_kb())

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
            "Кнопка 🕓 Ожидающие заявки показывает pending заявки.\n"
            "Напоминания/утренний список идут только по подтверждённым броням (таблица bookings).",
            reply_markup=admin_kb()
        )

    # ----------------- CLIENT FLOW: date -> time -> name -> phone -> service(button) -----------------
    @dp.message(F.text == "📅 Записаться")
    async def client_book_btn(m: Message, state: FSMContext):
        if is_admin(m):
            return

        # anti-spam gate at the start (so even if they cancel mid-way, still ok)
        left = await has_recent_request(m.from_user.id, ANTISPAM_MINUTES)
        if left > 0:
            await m.answer(ANTISPAM_TEXT, reply_markup=client_kb())
            return

        await state.clear()
        await state.set_state(ClientFlow.waiting_date)
        await m.answer("Введите дату ДД.ММ (например 20.02) или ❌ Отмена", reply_markup=cancel_kb())

    @dp.message(ClientFlow.waiting_date)
    async def client_date(m: Message, state: FSMContext):
        if is_admin(m):
            return
        txt = (m.text or "").strip()
        try:
            d, mo = parse_ddmm(txt)
        except Exception:
            await m.answer("Неверная дата. Пример: 20.02\n\nNoto‘g‘ri sana. Masalan: 20.02")
            return

        now = datetime.now(TZ)
        candidate = datetime(now.year, mo, d, 0, 0, tzinfo=TZ)
        if candidate.date() < now.date():
            await m.answer(PAST_DATE_TEXT)
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
            t = parse_hhmm(txt)
        except Exception:
            await m.answer("Неверное время. Пример: 14:00\n\nNoto‘g‘ri vaqt. Masalan: 14:00")
            return

        if not in_working_hours(t):
            await m.answer(WORK_HOURS_TEXT)
            return

        data = await state.get_data()
        ddmm = data.get("ddmm")
        try:
            dt = make_dt_current_year(ddmm, txt)
        except Exception:
            await m.answer("Ошибка даты/времени. Введите заново.\n\nSana/vaqt xatosi. Qayta kiriting.")
            return

        now = datetime.now(TZ)
        # Запрет прошедшего времени (включая сегодня)
        if dt <= now:
            await m.answer(PAST_DATE_TEXT)
            return

        await state.update_data(hhmm=txt)
        await state.set_state(ClientFlow.waiting_name)
        await m.answer("Введите имя\n\nIsmingizni kiriting")

    @dp.message(ClientFlow.waiting_name)
    async def client_name(m: Message, state: FSMContext):
        if is_admin(m):
            return
        name = (m.text or "").strip()
        if len(name) < 2:
            await m.answer("Имя слишком короткое. Введите снова.\n\nIsm juda qisqa. Qayta kiriting.")
            return
        await state.update_data(client_name=name)
        await state.set_state(ClientFlow.waiting_phone)
        await m.answer(
            "Введите номер телефона в одном из форматов:\n"
            f"{EX_PLUS} или {EX_998} или {EX_9}\n\n"
            "Telefon raqamini quyidagi formatlardan birida kiriting:\n"
            f"{EX_PLUS} yoki {EX_998} yoki {EX_9}"
        )

    @dp.message(ClientFlow.waiting_phone)
    async def client_phone(m: Message, state: FSMContext):
        if is_admin(m):
            return
        phone = normalize_uz_phone(m.text or "")
        if phone is None:
            await m.answer(PHONE_FORMAT_TEXT)
            return
        await state.update_data(phone=phone)
        await state.set_state(ClientFlow.waiting_service)
        await m.answer(
            "Выберите услугу кнопкой 👇\n\nXizmatni tanlang 👇",
            reply_markup=services_kb()
        )

    @dp.message(ClientFlow.waiting_service)
    async def client_service(m: Message, state: FSMContext):
        if is_admin(m):
            return
        choice = (m.text or "").strip()
        if choice not in SERVICES:
            await m.answer("Пожалуйста нажмите кнопку услуги 👇\n\nIltimos tugmani bosing 👇", reply_markup=services_kb())
            return

        # антиспам прямо перед созданием (на случай если юзер обошёл через /start)
        left = await has_recent_request(m.from_user.id, ANTISPAM_MINUTES)
        if left > 0:
            await state.clear()
            await m.answer(ANTISPAM_TEXT, reply_markup=client_kb())
            return

        data = await state.get_data()
        ddmm = data.get("ddmm")
        hhmm = data.get("hhmm")
        name = data.get("client_name")
        phone = data.get("phone")
        service = SERVICES[choice]

        try:
            dt = make_dt_current_year(ddmm, hhmm)
        except Exception:
            await state.clear()
            await m.answer("Ошибка даты/времени. Попробуйте снова.", reply_markup=client_kb())
            return

        now = datetime.now(TZ)
        if dt <= now:
            await state.clear()
            await m.answer(PAST_DATE_TEXT, reply_markup=client_kb())
            return

        req_text = f"Услуга: {service}; Имя: {name}; Тел: {phone}"

        req_id = await create_request(dt, req_text, m.from_user.id, m.chat.id)
        await state.clear()

        await m.answer(WAIT_TEXT, reply_markup=client_kb())

        admin_msg = (
            f"🆕 Заявка #{req_id}\n"
            f"🕒 {dt.strftime('%d.%m %H:%M')}\n"
            f"👤 Клиент: {m.from_user.full_name} (id {m.from_user.id})\n"
            f"📝 {req_text}"
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

        _id, ts, text, client_id, chat_id, status, booking_id, created_at = row
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

    # ----------------- ADMIN wizards + delete mode -----------------
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
            dt = make_dt_current_year(ddmm, hhmm)
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
