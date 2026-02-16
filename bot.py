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

SPA_PHONE = "+998916768900"

OPEN_TIME = dtime(9, 0)
CLOSE_TIME = dtime(22, 0)

ANTISPAM_MINUTES = 10
MY_REQUESTS_LIMIT = 10

SERVICE_BTNS = [
    "🧖‍♂️ Турецкий SPA",
    "🏊 Бассейн (отдельный)",
    "🏊‍♂️🔥 Бассейн + Сауна",
    "💆 Массаж",
    "🏋️ Тренажёрный зал",
]
SERVICE_MAP_RU = {s: s for s in SERVICE_BTNS}
SERVICE_MAP_UZ = {
    "🧖‍♂️ Турецкий SPA": "🧖‍♂️ Turkcha SPA",
    "🏊 Бассейн (отдельный)": "🏊 Alohida basseyn",
    "🏊‍♂️🔥 Бассейн + Сауна": "🏊‍♂️🔥 Basseyn + Sauna",
    "💆 Массаж": "💆 Massaj",
    "🏋️ Тренажёрный зал": "🏋️ Trenajor zali",
}


# -------------------- helpers / i18n --------------------
def t(lang: str, ru: str, uz: str) -> str:
    return uz if lang == "uz" else ru


def is_admin(m: Message) -> bool:
    return bool(m.from_user and m.from_user.id == ADMIN_ID)


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


def day_range(day: datetime):
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=TZ)
    end = start + timedelta(days=1)
    return start, end


def normalize_uz_phone(raw: str) -> str | None:
    s = (raw or "").strip().replace(" ", "").replace("-", "")
    if re.fullmatch(r"\+998\d{9}", s):
        return s
    if re.fullmatch(r"998\d{9}", s):
        return "+" + s
    if re.fullmatch(r"\d{9}", s):
        return "+998" + s
    return None


def spa_phone_examples() -> tuple[str, str, str]:
    digits = re.sub(r"\D", "", SPA_PHONE)
    if digits.startswith("998") and len(digits) == 12:
        local9 = digits[3:]
    else:
        local9 = digits[-9:] if len(digits) >= 9 else "916768900"
        digits = "998" + local9
    return (f"+{digits}", digits, local9)


EX_PLUS, EX_998, EX_9 = spa_phone_examples()


def make_dt_current_year(ddmm: str, hhmm: str) -> datetime:
    d, mo = parse_ddmm(ddmm)
    tm = parse_hhmm(hhmm)
    now = datetime.now(TZ)
    return datetime(now.year, mo, d, tm.hour, tm.minute, tzinfo=TZ)


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m %H:%M")


def in_working_hours(tm: dtime) -> bool:
    return OPEN_TIME <= tm <= CLOSE_TIME


# -------------------- DB migrations --------------------
async def table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cols = set()
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    for r in rows:
        cols.add(r[1])
    return cols


async def add_column_if_missing(db: aiosqlite.Connection, table: str, col: str, ddl_type: str, default_sql: str | None = None):
    cols = await table_columns(db, table)
    if col in cols:
        return
    if default_sql is None:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl_type}")
    else:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl_type} DEFAULT {default_sql}")


# -------------------- DB --------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            lang TEXT NOT NULL
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            text TEXT NOT NULL,
            reminded_admin INTEGER DEFAULT 0,
            reminded_client INTEGER DEFAULT 0,
            client_chat_id INTEGER,
            client_user_id INTEGER,
            client_lang TEXT,
            created_at TEXT NOT NULL
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            service TEXT NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            client_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            lang TEXT NOT NULL,
            status TEXT NOT NULL,            -- pending/confirmed/declined/cancelled
            booking_id INTEGER,
            decline_comment TEXT,
            created_at TEXT NOT NULL
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")

        # migrations (safe for old DB)
        await add_column_if_missing(db, "bookings", "reminded_admin", "INTEGER", "0")
        await add_column_if_missing(db, "bookings", "reminded_client", "INTEGER", "0")
        await add_column_if_missing(db, "bookings", "client_chat_id", "INTEGER")
        await add_column_if_missing(db, "bookings", "client_user_id", "INTEGER")
        await add_column_if_missing(db, "bookings", "client_lang", "TEXT")

        await add_column_if_missing(db, "requests", "decline_comment", "TEXT")

        await db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('morning_time', ?)",
            (MORNING_TIME_DEFAULT,),
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
            (key, value),
        )
        await db.commit()


async def get_user_lang(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT lang FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_user_lang(user_id: int, lang: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id, lang) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET lang=excluded.lang",
            (user_id, lang),
        )
        await db.commit()


async def add_booking(
    dt: datetime,
    info: str,
    client_chat_id: int | None,
    client_user_id: int | None,
    client_lang: str | None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO bookings(ts, text, reminded_admin, reminded_client, client_chat_id, client_user_id, client_lang, created_at) "
            "VALUES(?, ?, 0, 0, ?, ?, ?, ?)",
            (
                dt.isoformat(),
                info,
                client_chat_id,
                client_user_id,
                client_lang,
                datetime.now(TZ).isoformat(),
            ),
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


async def create_request(
    dt: datetime,
    service: str,
    name: str,
    phone: str,
    client_id: int,
    chat_id: int,
    lang: str,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO requests(ts, service, name, phone, client_id, chat_id, lang, status, created_at) "
            "VALUES(?,?,?,?,?,?,?, 'pending', ?)",
            (dt.isoformat(), service, name, phone, client_id, chat_id, lang, datetime.now(TZ).isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def get_request(req_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, ts, service, name, phone, client_id, chat_id, lang, status, booking_id, decline_comment, created_at "
            "FROM requests WHERE id=?",
            (req_id,),
        ) as cur:
            return await cur.fetchone()


async def set_request_status(req_id: int, status: str, booking_id: int | None = None, decline_comment: str | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE requests SET status=?, booking_id=?, decline_comment=? WHERE id=?",
            (status, booking_id, decline_comment, req_id),
        )
        await db.commit()


async def list_pending_requests(limit: int = 50):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, ts, service, name, phone, client_id, lang, created_at "
            "FROM requests WHERE status='pending' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ) as cur:
            return await cur.fetchall()


async def list_requests_for_client(client_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, ts, service, status, booking_id, created_at "
            "FROM requests WHERE client_id=? ORDER BY created_at DESC LIMIT ?",
            (client_id, limit),
        ) as cur:
            return await cur.fetchall()


async def latest_request_time(client_id: int) -> datetime | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT created_at FROM requests WHERE client_id=? ORDER BY created_at DESC LIMIT 1",
            (client_id,),
        ) as cur:
            row = await cur.fetchone()
    return datetime.fromisoformat(row[0]) if row else None


async def antispam_seconds_left(client_id: int, minutes: int) -> int:
    last = await latest_request_time(client_id)
    if not last:
        return 0
    now = datetime.now(TZ)
    until = last + timedelta(minutes=minutes)
    if until > now:
        return max(1, int((until - now).total_seconds()))
    return 0


# -------------------- notifications --------------------
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
        async with db.execute(
            """
            SELECT id, ts, text, client_chat_id, client_lang, reminded_admin, reminded_client
            FROM bookings
            WHERE ts >= ? AND ts < ?
            ORDER BY ts ASC
            """,
            (window_start.isoformat(), window_end.isoformat()),
        ) as cur:
            rows = await cur.fetchall()

        for bid, ts, txt, client_chat_id, client_lang, ra, rc in rows:
            dt = datetime.fromisoformat(ts)

            if ra == 0:
                await bot.send_message(ADMIN_ID, f"⏰ Через 1 час: {fmt_dt(dt)} — {txt} (#{bid})")
                await db.execute("UPDATE bookings SET reminded_admin=1 WHERE id=?", (bid,))

            if client_chat_id and rc == 0:
                lang = client_lang or "ru"
                msg = t(
                    lang,
                    f"⏰ Напоминание: через 1 час у вас бронь {fmt_dt(dt)}.\n☎️ SPA: {SPA_PHONE}",
                    f"⏰ Eslatma: 1 soatdan keyin bandingiz bor {fmt_dt(dt)}.\n☎️ SPA: {SPA_PHONE}",
                )
                try:
                    await bot.send_message(int(client_chat_id), msg)
                    await db.execute("UPDATE bookings SET reminded_client=1 WHERE id=?", (bid,))
                except Exception:
                    pass

        await db.commit()


# -------------------- Render HTTP server --------------------
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


# -------------------- keyboards --------------------
def kb_lang():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🇷🇺 Русский"), KeyboardButton(text="🇺🇿 Oʻzbek")]],
        resize_keyboard=True,
    )


def admin_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🕓 Ожидающие заявки"), KeyboardButton(text="📅 Сегодня")],
            [KeyboardButton(text="📆 На дату"), KeyboardButton(text="➕ Добавить бронь")],
            [KeyboardButton(text="🗑 Удалить бронь"), KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
    )


def client_kb(lang: str):
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=t(lang, "📅 Записаться", "📅 Yozilish")),
                KeyboardButton(text=t(lang, "🧾 Мои заявки", "🧾 Mening arizalarim")),
            ],
            [KeyboardButton(text=t(lang, "☎️ Связаться с админом", "☎️ Administrator bilan aloqa"))],
        ],
        resize_keyboard=True,
    )


def cancel_kb(lang: str):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t(lang, "❌ Отмена", "❌ Bekor qilish"))]],
        resize_keyboard=True,
    )


def services_kb(lang: str):
    rows = []
    for s in SERVICE_BTNS:
        show = s if lang == "ru" else SERVICE_MAP_UZ.get(s, s)
        rows.append([KeyboardButton(text=show)])
    rows.append([KeyboardButton(text=t(lang, "❌ Отмена", "❌ Bekor qilish"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def req_inline_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"req:ok:{req_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"req:no:{req_id}"),
            ]
        ]
    )


def client_req_actions_kb(req_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t(lang, "❌ Отменить", "❌ Bekor qilish"), callback_data=f"creq:cancel:{req_id}"),
                InlineKeyboardButton(text=t(lang, "🔁 Перенести", "🔁 Ko‘chirish"), callback_data=f"creq:move:{req_id}"),
            ]
        ]
    )


# -------------------- FSM --------------------
class ClientNewFlow(StatesGroup):
    waiting_date = State()
    waiting_time = State()
    waiting_name = State()
    waiting_phone = State()
    waiting_service = State()


class ClientMoveFlow(StatesGroup):
    waiting_date = State()
    waiting_time = State()


class AdminAddFlow(StatesGroup):
    waiting_date = State()
    waiting_time = State()
    waiting_text = State()


class AdminOnDateFlow(StatesGroup):
    waiting_date = State()


class AdminDeleteFlow(StatesGroup):
    waiting_id = State()


class AdminDeclineFlow(StatesGroup):
    waiting_comment = State()


# -------------------- texts --------------------
def phone_format_text(lang: str) -> str:
    return t(
        lang,
        "⚠️ Номер введён неверно.\nВведите заново в одном из форматов:\n"
        f"✅ {EX_PLUS}\n✅ {EX_998}\n✅ {EX_9}",
        "⚠️ Telefon raqami noto‘g‘ri.\nQuyidagi formatlardan birida kiriting:\n"
        f"✅ {EX_PLUS}\n✅ {EX_998}\n✅ {EX_9}",
    )


def past_date_text(lang: str) -> str:
    return t(
        lang,
        "⚠️ Нельзя выбрать прошедшую дату/время. Введите заново.",
        "⚠️ O‘tgan sana/vaqtni tanlab bo‘lmaydi. Qayta kiriting.",
    )


def work_hours_text(lang: str) -> str:
    return t(
        lang,
        "⚠️ Мы работаем с 09:00 до 22:00. Введите время заново.",
        "⚠️ Ish vaqti 09:00 dan 22:00 gacha. Vaqtni qayta kiriting.",
    )


def wait_text(lang: str) -> str:
    return t(
        lang,
        f"✅ Ваша заявка отправлена.\n⏳ Ожидайте подтверждение администратора.\n☎️ Телефон SPA: {SPA_PHONE}",
        f"✅ Arizangiz yuborildi.\n⏳ Administrator tasdig‘ini kuting.\n☎️ SPA telefoni: {SPA_PHONE}",
    )


def confirmed_text(lang: str, dt: datetime) -> str:
    return t(
        lang,
        f"✅ Ваша бронь подтверждена!\n📅 {fmt_dt(dt)}\n☎️ Телефон SPA: {SPA_PHONE}",
        f"✅ Band qilishingiz tasdiqlandi!\n📅 {fmt_dt(dt)}\n☎️ SPA telefoni: {SPA_PHONE}",
    )


def declined_text(lang: str, comment: str | None) -> str:
    comment = (comment or "").strip()
    if comment:
        return t(
            lang,
            f"❌ К сожалению, бронь отклонена.\nПричина: {comment}\n☎️ Телефон SPA: {SPA_PHONE}",
            f"❌ Afsuski, band qilish rad etildi.\nSabab: {comment}\n☎️ SPA telefoni: {SPA_PHONE}",
        )
    return t(
        lang,
        f"❌ К сожалению, бронь отклонена.\n☎️ Телефон SPA: {SPA_PHONE}",
        f"❌ Afsuski, band qilish rad etildi.\n☎️ SPA telefoni: {SPA_PHONE}",
    )


def antispam_text(lang: str) -> str:
    return t(
        lang,
        "⏳ Слишком часто. Можно отправлять заявку раз в 10 минут. Попробуйте позже.",
        "⏳ Juda tez-tez. 10 daqiqada 1 marta ariza yuborish mumkin. Keyinroq urinib ko‘ring.",
    )


def contact_text(lang: str) -> str:
    return t(
        lang,
        f"☎️ Связаться с администратором:\n{SPA_PHONE}",
        f"☎️ Administrator bilan bog‘lanish:\n{SPA_PHONE}",
    )


# -------------------- bot --------------------
async def run_bot():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set BOT_TOKEN env var.")
    if ADMIN_ID == 0:
        raise RuntimeError("ADMIN_ID is 0. Set ADMIN_ID env var.")

    await init_db()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # scheduler
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
            replace_existing=True,
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

    # -------- /start --------
    @dp.message(Command("start"))
    async def cmd_start(m: Message, state: FSMContext):
        await state.clear()
        if is_admin(m):
            await m.answer("✅ Админ-режим.\nКнопки 👇", reply_markup=admin_kb())
            return
        user_lang = await get_user_lang(m.from_user.id)
        if not user_lang:
            await m.answer("Выберите язык / Tilni tanlang 👇", reply_markup=kb_lang())
            return
        await m.answer(t(user_lang, "Меню 👇", "Menyu 👇"), reply_markup=client_kb(user_lang))

    # -------- language selection --------
    @dp.message(F.text.in_(["🇷🇺 Русский", "🇺🇿 Oʻzbek"]))
    async def choose_lang(m: Message, state: FSMContext):
        if is_admin(m):
            return
        lang = "uz" if m.text == "🇺🇿 Oʻzbek" else "ru"
        await set_user_lang(m.from_user.id, lang)
        await state.clear()
        await m.answer(t(lang, "Язык сохранён ✅", "Til saqlandi ✅"), reply_markup=client_kb(lang))

    # -------- cancel --------
    @dp.message(F.text.in_(["❌ Отмена", "❌ Bekor qilish"]))
    async def cancel_any(m: Message, state: FSMContext):
        await state.clear()
        if is_admin(m):
            await m.answer("Ок ✅", reply_markup=admin_kb())
            return
        lang = await get_user_lang(m.from_user.id) or "ru"
        await m.answer(t(lang, "Ок ✅", "Ok ✅"), reply_markup=client_kb(lang))

    # -------- client: contact admin --------
    @dp.message(F.text.in_(["☎️ Связаться с админом", "☎️ Administrator bilan aloqa"]))
    async def client_contact(m: Message):
        if is_admin(m):
            return
        lang = await get_user_lang(m.from_user.id) or "ru"
        await m.answer(contact_text(lang), reply_markup=client_kb(lang))

    # -------- client: my requests --------
    @dp.message(F.text.in_(["🧾 Мои заявки", "🧾 Mening arizalarim"]))
    async def client_my_requests(m: Message):
        if is_admin(m):
            return
        lang = await get_user_lang(m.from_user.id) or "ru"
        rows = await list_requests_for_client(m.from_user.id, MY_REQUESTS_LIMIT)
        if not rows:
            await m.answer(t(lang, "У вас пока нет заявок.", "Sizda hali arizalar yo‘q."), reply_markup=client_kb(lang))
            return

        def st_icon(st: str) -> str:
            return {"pending": "🕓", "confirmed": "✅", "declined": "❌", "cancelled": "🚫"}.get(st, "•")

        await m.answer(t(lang, "🧾 Ваши заявки:", "🧾 Sizning arizalaringiz:"), reply_markup=client_kb(lang))
        for rid, ts, service, status, booking_id, created_at in rows:
            dt = datetime.fromisoformat(ts)
            line = f"{st_icon(status)} #{rid} — {fmt_dt(dt)} — {service}"
            if status in ("pending", "confirmed"):
                await m.answer(line, reply_markup=client_req_actions_kb(rid, lang))
            else:
                await m.answer(line)

    # -------- client: cancel / move callbacks --------
    @dp.callback_query(F.data.startswith("creq:"))
    async def client_req_action(cb: CallbackQuery, state: FSMContext):
        if not cb.from_user:
            return
        lang = await get_user_lang(cb.from_user.id) or "ru"

        parts = (cb.data or "").split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            await cb.answer("Error", show_alert=True)
            return

        action, rid = parts[1], int(parts[2])
        row = await get_request(rid)
        if not row:
            await cb.answer(t(lang, "Заявка не найдена", "Ariza topilmadi"), show_alert=True)
            return

        (_id, ts, service, name, phone, client_id, chat_id, rlang, status, booking_id, decline_comment, created_at) = row
        if client_id != cb.from_user.id:
            await cb.answer(t(lang, "Нет доступа", "Ruxsat yo‘q"), show_alert=True)
            return

        if status not in ("pending", "confirmed"):
            await cb.answer(t(lang, "Эту заявку нельзя изменить", "Bu arizani o‘zgartirib bo‘lmaydi"), show_alert=True)
            return

        if action == "cancel":
            if booking_id:
                await delete_booking(int(booking_id))
            await set_request_status(rid, "cancelled", None, None)
            await cb.answer(t(lang, "Отменено ✅", "Bekor qilindi ✅"), show_alert=False)
            return

        if action == "move":
            await state.clear()
            await state.set_state(ClientMoveFlow.waiting_date)
            await state.update_data(move_rid=rid)
            await cb.answer(t(lang, "Введите новую дату", "Yangi sanani kiriting"), show_alert=False)
            try:
                await cb.message.answer(
                    t(lang, "Введите новую дату ДД.ММ (например 20.02)", "Yangi sana DD.MM (masalan 20.02)"),
                    reply_markup=cancel_kb(lang),
                )
            except Exception:
                pass
            return

        await cb.answer("OK")

    # -------- client: move flow --------
    @dp.message(ClientMoveFlow.waiting_date)
    async def client_move_date(m: Message, state: FSMContext):
        if is_admin(m):
            return
        lang = await get_user_lang(m.from_user.id) or "ru"
        txt = (m.text or "").strip()

        try:
            d, mo = parse_ddmm(txt)
        except Exception:
            await m.answer(t(lang, "Неверная дата. Пример: 20.02", "Noto‘g‘ri sana. Masalan: 20.02"))
            return

        now = datetime.now(TZ)
        candidate = datetime(now.year, mo, d, 0, 0, tzinfo=TZ)
        if candidate.date() < now.date():
            await m.answer(past_date_text(lang))
            return

        await state.update_data(new_ddmm=txt)
        await state.set_state(ClientMoveFlow.waiting_time)
        await m.answer(t(lang, "Введите новое время ЧЧ:ММ (например 14:00)", "Yangi vaqt HH:MM (masalan 14:00)"))

    @dp.message(ClientMoveFlow.waiting_time)
    async def client_move_time(m: Message, state: FSMContext):
        if is_admin(m):
            return
        lang = await get_user_lang(m.from_user.id) or "ru"
        txt = (m.text or "").strip()

        try:
            tm = parse_hhmm(txt)
        except Exception:
            await m.answer(t(lang, "Неверное время. Пример: 14:00", "Noto‘g‘ri vaqt. Masalan: 14:00"))
            return

        if not in_working_hours(tm):
            await m.answer(work_hours_text(lang))
            return

        data = await state.get_data()
        rid = int(data["move_rid"])
        new_ddmm = data["new_ddmm"]

        # антиспам
        left = await antispam_seconds_left(m.from_user.id, ANTISPAM_MINUTES)
        if left > 0:
            await state.clear()
            await m.answer(antispam_text(lang), reply_markup=client_kb(lang))
            return

        row = await get_request(rid)
        if not row:
            await state.clear()
            await m.answer(t(lang, "Заявка не найдена.", "Ariza topilmadi."), reply_markup=client_kb(lang))
            return

        (_id, ts, service, name, phone, client_id, chat_id, rlang, status, booking_id, decline_comment, created_at) = row
        if client_id != m.from_user.id:
            await state.clear()
            await m.answer(t(lang, "Нет доступа.", "Ruxsat yo‘q."), reply_markup=client_kb(lang))
            return

        new_dt = make_dt_current_year(new_ddmm, txt)
        if new_dt <= datetime.now(TZ):
            await m.answer(past_date_text(lang))
            return

        # отменяем старую (и бронь, если была)
        if booking_id:
            await delete_booking(int(booking_id))
        await set_request_status(rid, "cancelled", None, None)

        # создаём новую pending
        new_req_id = await create_request(new_dt, service, name, phone, m.from_user.id, m.chat.id, lang)

        await state.clear()
        await m.answer(
            t(
                lang,
                f"✅ Перенос создан. Новая заявка #{new_req_id} на {fmt_dt(new_dt)}.\n⏳ Ожидайте подтверждение администратора.",
                f"✅ Ko‘chirildi. Yangi ariza #{new_req_id} {fmt_dt(new_dt)}.\n⏳ Administrator tasdig‘ini kuting.",
            ),
            reply_markup=client_kb(lang),
        )

        await bot.send_message(
            ADMIN_ID,
            f"🆕 Заявка #{new_req_id} (перенос)\n🕒 {fmt_dt(new_dt)}\n👤 Клиент: {m.from_user.full_name} (id {m.from_user.id})\n📝 Услуга: {service}; Имя: {name}; Тел: {phone}",
            reply_markup=req_inline_kb(new_req_id),
        )

    # -------- client: new booking --------
    @dp.message(F.text.in_(["📅 Записаться", "📅 Yozilish"]))
    async def client_book_btn(m: Message, state: FSMContext):
        if is_admin(m):
            return
        lang = await get_user_lang(m.from_user.id)
        if not lang:
            await m.answer("Выберите язык / Tilni tanlang 👇", reply_markup=kb_lang())
            return

        left = await antispam_seconds_left(m.from_user.id, ANTISPAM_MINUTES)
        if left > 0:
            await m.answer(antispam_text(lang), reply_markup=client_kb(lang))
            return

        await state.clear()
        await state.set_state(ClientNewFlow.waiting_date)
        await m.answer(t(lang, "Введите дату ДД.ММ (например 20.02)", "Sana DD.MM (masalan 20.02)"), reply_markup=cancel_kb(lang))

    @dp.message(ClientNewFlow.waiting_date)
    async def client_date(m: Message, state: FSMContext):
        if is_admin(m):
            return
        lang = await get_user_lang(m.from_user.id) or "ru"
        txt = (m.text or "").strip()

        try:
            d, mo = parse_ddmm(txt)
        except Exception:
            await m.answer(t(lang, "Неверная дата. Пример: 20.02", "Noto‘g‘ri sana. Masalan: 20.02"))
            return

        now = datetime.now(TZ)
        candidate = datetime(now.year, mo, d, 0, 0, tzinfo=TZ)
        if candidate.date() < now.date():
            await m.answer(past_date_text(lang))
            return

        await state.update_data(ddmm=txt)
        await state.set_state(ClientNewFlow.waiting_time)
        await m.answer(t(lang, "Введите время ЧЧ:ММ (например 14:00)", "Vaqt HH:MM (masalan 14:00)"))

    @dp.message(ClientNewFlow.waiting_time)
    async def client_time(m: Message, state: FSMContext):
        if is_admin(m):
            return
        lang = await get_user_lang(m.from_user.id) or "ru"
        txt = (m.text or "").strip()

        try:
            tm = parse_hhmm(txt)
        except Exception:
            await m.answer(t(lang, "Неверное время. Пример: 14:00", "Noto‘g‘ri vaqt. Masalan: 14:00"))
            return

        if not in_working_hours(tm):
            await m.answer(work_hours_text(lang))
            return

        data = await state.get_data()
        ddmm = data["ddmm"]
        dt = make_dt_current_year(ddmm, txt)

        if dt <= datetime.now(TZ):
            await m.answer(past_date_text(lang))
            return

        await state.update_data(hhmm=txt)
        await state.set_state(ClientNewFlow.waiting_name)
        await m.answer(t(lang, "Введите имя", "Ismingizni kiriting"))

    @dp.message(ClientNewFlow.waiting_name)
    async def client_name(m: Message, state: FSMContext):
        if is_admin(m):
            return
        lang = await get_user_lang(m.from_user.id) or "ru"
        name = (m.text or "").strip()
        if len(name) < 2:
            await m.answer(t(lang, "Имя слишком короткое. Введите снова.", "Ism juda qisqa. Qayta kiriting."))
            return

        await state.update_data(name=name)
        await state.set_state(ClientNewFlow.waiting_phone)
        await m.answer(
            t(
                lang,
                "Введите номер телефона в одном из форматов:\n"
                f"{EX_PLUS}\n{EX_998}\n{EX_9}",
                "Telefon raqamini kiriting:\n"
                f"{EX_PLUS}\n{EX_998}\n{EX_9}",
            )
        )

    @dp.message(ClientNewFlow.waiting_phone)
    async def client_phone(m: Message, state: FSMContext):
        if is_admin(m):
            return
        lang = await get_user_lang(m.from_user.id) or "ru"

        phone = normalize_uz_phone(m.text or "")
        if phone is None:
            await m.answer(phone_format_text(lang))
            return

        await state.update_data(phone=phone)
        await state.set_state(ClientNewFlow.waiting_service)
        await m.answer(t(lang, "Выберите услугу кнопкой 👇", "Xizmatni tanlang 👇"), reply_markup=services_kb(lang))

    @dp.message(ClientNewFlow.waiting_service)
    async def client_service(m: Message, state: FSMContext):
        if is_admin(m):
            return
        lang = await get_user_lang(m.from_user.id) or "ru"
        raw = (m.text or "").strip()

        service_ru = None
        if raw in SERVICE_MAP_RU:
            service_ru = raw
        else:
            for k_ru, v_uz in SERVICE_MAP_UZ.items():
                if raw == v_uz:
                    service_ru = k_ru
                    break

        if not service_ru:
            await m.answer(t(lang, "Пожалуйста нажмите кнопку услуги 👇", "Iltimos xizmat tugmasini bosing 👇"), reply_markup=services_kb(lang))
            return

        left = await antispam_seconds_left(m.from_user.id, ANTISPAM_MINUTES)
        if left > 0:
            await state.clear()
            await m.answer(antispam_text(lang), reply_markup=client_kb(lang))
            return

        data = await state.get_data()
        ddmm, hhmm = data["ddmm"], data["hhmm"]
        name, phone = data["name"], data["phone"]
        dt = make_dt_current_year(ddmm, hhmm)

        if dt <= datetime.now(TZ):
            await state.clear()
            await m.answer(past_date_text(lang), reply_markup=client_kb(lang))
            return

        req_id = await create_request(dt, service_ru, name, phone, m.from_user.id, m.chat.id, lang)
        await state.clear()
        await m.answer(wait_text(lang), reply_markup=client_kb(lang))

        service_show = service_ru if lang == "ru" else SERVICE_MAP_UZ.get(service_ru, service_ru)
        await bot.send_message(
            ADMIN_ID,
            f"🆕 Заявка #{req_id}\n🕒 {fmt_dt(dt)}\n👤 Клиент: {m.from_user.full_name} (id {m.from_user.id})\n📝 Услуга: {service_show}\n👤 Имя: {name}\n📞 Тел: {phone}",
            reply_markup=req_inline_kb(req_id),
        )

    # -------------------- ADMIN --------------------
    @dp.message(F.text == "ℹ️ Помощь")
    async def admin_help(m: Message):
        if not is_admin(m):
            return
        await m.answer(
            "Админ:\n"
            "🕓 Ожидающие заявки — список заявок от клиентов\n"
            "✅ Подтвердить / ❌ Отклонить (с комментом)\n\n"
            "📅 Сегодня — брони на сегодня\n"
            "📆 На дату — брони по выбранной дате\n"
            "➕ Добавить бронь — админ вручную\n"
            "🗑 Удалить бронь — удалить по ID\n\n"
            "Команды:\n"
            "/time 09:00 — время утреннего списка\n",
            reply_markup=admin_kb(),
        )

    # /time
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

    # pending list
    @dp.message(F.text == "🕓 Ожидающие заявки")
    async def admin_pending(m: Message):
        if not is_admin(m):
            return
        rows = await list_pending_requests(50)
        if not rows:
            await m.answer("Нет ожидающих заявок ✅", reply_markup=admin_kb())
            return
        await m.answer(f"🕓 Ожидающие заявки: {len(rows)}", reply_markup=admin_kb())
        for rid, ts, service, name, phone, client_id, lang, created_at in rows:
            dt = datetime.fromisoformat(ts)
            await m.answer(
                f"🕓 Заявка #{rid}\n🕒 {fmt_dt(dt)}\n👤 client_id: {client_id}\n📝 {service}\n👤 {name}\n📞 {phone}",
                reply_markup=req_inline_kb(rid),
            )

    # confirm/decline callbacks
    @dp.callback_query(F.data.startswith("req:"))
    async def admin_req_action(cb: CallbackQuery, state: FSMContext):
        if not (cb.from_user and cb.from_user.id == ADMIN_ID):
            await cb.answer("Нет доступа", show_alert=True)
            return

        parts = (cb.data or "").split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            await cb.answer("Ошибка", show_alert=True)
            return

        action = parts[1]
        req_id = int(parts[2])

        row = await get_request(req_id)
        if not row:
            await cb.answer("Заявка не найдена", show_alert=True)
            return

        (_id, ts, service, name, phone, client_id, chat_id, lang, status, booking_id, decline_comment, created_at) = row
        dt = datetime.fromisoformat(ts)

        if status != "pending":
            await cb.answer("Уже обработано", show_alert=True)
            return

        if action == "ok":
            info = f"{service}; Имя: {name}; Тел: {phone}"
            bid = await add_booking(dt, info, int(chat_id), int(client_id), lang)
            await set_request_status(req_id, "confirmed", int(bid), None)
            await cb.answer("Подтверждено ✅")
            try:
                await cb.message.answer(f"✅ Подтверждено. Бронь #{bid} на {fmt_dt(dt)}")
            except Exception:
                pass
            try:
                await bot.send_message(int(chat_id), confirmed_text(lang, dt))
            except Exception:
                pass
            return

        if action == "no":
            await state.clear()
            await state.set_state(AdminDeclineFlow.waiting_comment)
            await state.update_data(decline_req_id=req_id)
            await cb.answer("Введите причину отказа", show_alert=False)
            try:
                await cb.message.answer("✍️ Напишите причину отказа (1 сообщением).")
            except Exception:
                pass
            return

        await cb.answer("OK")

    # decline comment
    @dp.message(AdminDeclineFlow.waiting_comment)
    async def admin_decline_comment(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        comment = (m.text or "").strip()
        if len(comment) < 2:
            await m.answer("Комментарий слишком короткий. Напишите причину одним сообщением.")
            return

        data = await state.get_data()
        req_id = int(data["decline_req_id"])

        row = await get_request(req_id)
        if not row:
            await state.clear()
            await m.answer("Заявка не найдена.", reply_markup=admin_kb())
            return

        (_id, ts, service, name, phone, client_id, chat_id, lang, status, booking_id, decline_comment, created_at) = row
        if status != "pending":
            await state.clear()
            await m.answer("Заявка уже обработана.", reply_markup=admin_kb())
            return

        await set_request_status(req_id, "declined", None, comment)
        await state.clear()
        await m.answer(f"❌ Заявка #{req_id} отклонена. Причина отправлена клиенту.", reply_markup=admin_kb())
        try:
            await bot.send_message(int(chat_id), declined_text(lang, comment))
        except Exception:
            pass

    # today bookings
    @dp.message(F.text == "📅 Сегодня")
    async def admin_today(m: Message):
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

    # on date bookings
    @dp.message(F.text == "📆 На дату")
    async def admin_on_date_btn(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        await state.clear()
        await state.set_state(AdminOnDateFlow.waiting_date)
        await m.answer("Введите дату ДД.ММ (например 20.02) или ❌ Отмена")

    @dp.message(AdminOnDateFlow.waiting_date)
    async def admin_on_date_input(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        txt = (m.text or "").strip()
        if txt == "❌ Отмена":
            await state.clear()
            await m.answer("Ок ✅", reply_markup=admin_kb())
            return
        try:
            d, mo = parse_ddmm(txt)
        except Exception:
            await m.answer("Неверная дата. Пример: 20.02")
            return

        now = datetime.now(TZ)
        day = datetime(now.year, mo, d, 0, 0, tzinfo=TZ)
        start, end = day_range(day)

        rows = await list_bookings_between(start, end)
        if not rows:
            await state.clear()
            await m.answer(f"На {txt} броней нет ✅", reply_markup=admin_kb())
            return

        lines = [f"📆 Брони на {txt}:"]
        for bid, ts, txt2 in rows:
            dt = datetime.fromisoformat(ts)
            lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {txt2}")

        await state.clear()
        await m.answer("\n".join(lines), reply_markup=admin_kb())

    # add booking (admin)
    @dp.message(F.text == "➕ Добавить бронь")
    async def admin_add_btn(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        await state.clear()
        await state.set_state(AdminAddFlow.waiting_date)
        await m.answer("Введите дату ДД.ММ (например 20.02) или ❌ Отмена")

    @dp.message(AdminAddFlow.waiting_date)
    async def admin_add_date(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        txt = (m.text or "").strip()
        if txt == "❌ Отмена":
            await state.clear()
            await m.answer("Ок ✅", reply_markup=admin_kb())
            return
        try:
            d, mo = parse_ddmm(txt)
        except Exception:
            await m.answer("Неверная дата. Пример: 20.02")
            return

        # запрет прошлой даты (по дню)
        now = datetime.now(TZ)
        candidate = datetime(now.year, mo, d, 0, 0, tzinfo=TZ)
        if candidate.date() < now.date():
            await m.answer("⚠️ Прошедшую дату нельзя. Введите снова.")
            return

        await state.update_data(ddmm=txt)
        await state.set_state(AdminAddFlow.waiting_time)
        await m.answer("Введите время ЧЧ:ММ (например 14:00)")

    @dp.message(AdminAddFlow.waiting_time)
    async def admin_add_time(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        txt = (m.text or "").strip()
        if txt == "❌ Отмена":
            await state.clear()
            await m.answer("Ок ✅", reply_markup=admin_kb())
            return
        try:
            tm = parse_hhmm(txt)
        except Exception:
            await m.answer("Неверное время. Пример: 14:00")
            return

        if not in_working_hours(tm):
            await m.answer("⚠️ Мы работаем 09:00–22:00. Введите время заново.")
            return

        await state.update_data(hhmm=txt)
        await state.set_state(AdminAddFlow.waiting_text)
        await m.answer("Введите текст брони (например: Массаж; Имя; Телефон)")

    @dp.message(AdminAddFlow.waiting_text)
    async def admin_add_text(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        text = (m.text or "").strip()
        if text == "❌ Отмена":
            await state.clear()
            await m.answer("Ок ✅", reply_markup=admin_kb())
            return
        if not text:
            await m.answer("Текст не должен быть пустым.")
            return

        data = await state.get_data()
        ddmm, hhmm = data["ddmm"], data["hhmm"]
        dt = make_dt_current_year(ddmm, hhmm)
        if dt <= datetime.now(TZ):
            await m.answer("⚠️ Прошедшее время нельзя. Введите заново (дата/время).")
            return

        bid = await add_booking(dt, text, None, None, None)
        await state.clear()
        await m.answer(f"✅ Добавлено: #{bid} — {fmt_dt(dt)} — {text}", reply_markup=admin_kb())

    # delete booking (admin)
    @dp.message(F.text == "🗑 Удалить бронь")
    async def admin_del_btn(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        await state.clear()
        await state.set_state(AdminDeleteFlow.waiting_id)
        await m.answer("Введите ID брони (например 12) или ❌ Отмена")

    @dp.message(AdminDeleteFlow.waiting_id)
    async def admin_del_id(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        txt = (m.text or "").strip()
        if txt == "❌ Отмена":
            await state.clear()
            await m.answer("Ок ✅", reply_markup=admin_kb())
            return
        if not txt.isdigit():
            await m.answer("Введите число (ID брони). Например: 12")
            return
        bid = int(txt)
        ok = await delete_booking(bid)
        await state.clear()
        if ok:
            await m.answer(f"✅ Бронь #{bid} удалена.", reply_markup=admin_kb())
        else:
            await m.answer(f"⚠️ Бронь #{bid} не найдена.", reply_markup=admin_kb())

    # fallback
    @dp.message()
    async def fallback(m: Message):
        if is_admin(m):
            await m.answer("Нажмите кнопку меню или /start.", reply_markup=admin_kb())
            return
        lang = await get_user_lang(m.from_user.id)
        if not lang:
            await m.answer("Выберите язык / Tilni tanlang 👇", reply_markup=kb_lang())
        else:
            await m.answer(t(lang, "Нажмите /start", "/start ni bosing"), reply_markup=client_kb(lang))

    await dp.start_polling(bot)


async def main():
    await init_db()
    await asyncio.gather(run_web_server(), run_bot())


if __name__ == "__main__":
    asyncio.run(main())
