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
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

# ---------------- ENV ----------------
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
TZ_NAME = (os.getenv("TZ") or "Asia/Tashkent").strip()
MORNING_TIME = (os.getenv("MORNING_TIME") or "09:00").strip()

PIN_RECEPTION = (os.getenv("PIN_RECEPTION") or "").strip()  # 10 digits
PIN_SPA = (os.getenv("PIN_SPA") or "").strip()              # 10 digits

PORT = int(os.getenv("PORT", "10000"))

TZ = ZoneInfo(TZ_NAME)
DB_PATH = "hotel_bot.db"

ROLE_RECEPTION = "reception"
ROLE_SPA = "spa"

CAT_RESTAURANT = "restaurant"
CAT_LAUNDRY = "laundry"
CAT_SPA = "spa"

OPEN_TIME = dtime(9, 0)
CLOSE_TIME = dtime(22, 0)

PHONE_RECEPTION = "+998906338900"   # бронь комнаты
PHONE_SPA = "+998916768900"         # бронь SPA

# ---------------- i18n ----------------
TXT = {
    "ru": {
        "hello_choose_lang": "Здравствуйте! 👋\nВыберите язык / Tilni tanlang:",
        "menu": "Выберите действие:",
        "btn_room": "🏨 Бронь комнаты",
        "btn_spa": "🧖 Запись в SPA",
        "btn_contact": "📞 Связь с админом",
        "btn_my": "📄 Мои заявки",
        "contact_room": f"📞 Администратор по брони комнат: {PHONE_RECEPTION}",
        "contact_spa": f"📞 Администратор SPA: {PHONE_SPA}",
        "room_start": "🏨 Бронь комнаты\nНапишите заявку одним сообщением (ФИО, даты, пожелания, телефон).",
        "spa_start": "🧖 Запись в SPA\nВведите дату ДД.ММ (например 20.02):",
        "spa_time": "Введите время ЧЧ:ММ (например 14:00):",
        "spa_text": "Напишите детали заявки (услуга/имя/телефон/комната если есть):",
        "sent_room": f"✅ Заявка отправлена администратору.\nЕсли срочно — звоните: {PHONE_RECEPTION}",
        "sent_spa": f"✅ Заявка отправлена администратору.\nОжидайте подтверждение.\nЕсли срочно — звоните: {PHONE_SPA}",
        "bad_date": "❌ Неверная дата. Пример: 20.02",
        "past_date": "⚠️ Прошедшую дату нельзя. Введите снова:",
        "bad_time": "❌ Неверное время. Пример: 14:00",
        "work_hours": "⚠️ Рабочие часы 09:00–22:00. Введите время заново:",
        "empty": "❌ Сообщение пустое. Введите снова:",
        "my_none": "У вас пока нет заявок.",
        "my_title": "📄 Ваши заявки:",
        "admin_panel_reception": "✅ Панель: Ресепшен",
        "admin_panel_spa": "✅ Панель: SPA",
        "logout": "Вы вышли ✅",
        "need_room": "Введите номер комнаты (например 101):",
        "need_amount": "Введите сумму (только цифры), например 50000:",
        "need_note": "Комментарий (или '-' если не нужно):",
        "folio_enter_room": "Введите номер комнаты:",
        "bad_room": "❌ Неверный номер комнаты. Введите только цифры (например 101):",
        "bad_amount": "❌ Неверная сумма. Введите только цифры (например 50000):",
        "folio_empty": "Записей нет.",
        "today_none": "Сегодня броней нет ✅",
        "ask_ddmm": "Введите дату ДД.ММ (например 20.02):",
        "ask_delete_id": "Введите ID брони (например 12):",
        "deleted": "✅ Удалено",
        "not_found": "⚠️ Не найдено",
        "only_number": "❌ Нужно число. Например 12:",
        "added_booking": "✅ Бронь добавлена:",
        "on_date_none": "На эту дату броней нет ✅",
    },
    "uz": {
        "hello_choose_lang": "Assalomu alaykum! 👋\nTilni tanlang / Выберите язык:",
        "menu": "Amalni tanlang:",
        "btn_room": "🏨 Xona bron qilish",
        "btn_spa": "🧖 SPA yozilish",
        "btn_contact": "📞 Administrator bilan aloqa",
        "btn_my": "📄 Mening arizalarim",
        "contact_room": f"📞 Xona bo‘yicha administrator: {PHONE_RECEPTION}",
        "contact_spa": f"📞 SPA administrator: {PHONE_SPA}",
        "room_start": "🏨 Xona bron qilish\nArizani bitta xabarda yozing (F.I.Sh, sanalar, telefon).",
        "spa_start": "🧖 SPA yozilish\nSana kiriting DD.MM (masalan 20.02):",
        "spa_time": "Vaqt kiriting HH:MM (masalan 14:00):",
        "spa_text": "Ariza tafsilotlarini yozing (xizmat/ism/telefon/xona bo‘lsa):",
        "sent_room": f"✅ Ariza yuborildi.\nShoshilinch bo‘lsa qo‘ng‘iroq qiling: {PHONE_RECEPTION}",
        "sent_spa": f"✅ Ariza yuborildi.\nTasdiqlashni kuting.\nShoshilinch bo‘lsa qo‘ng‘iroq qiling: {PHONE_SPA}",
        "bad_date": "❌ Sana noto‘g‘ri. Masalan: 20.02",
        "past_date": "⚠️ O‘tgan sanani kiritib bo‘lmaydi. Qaytadan kiriting:",
        "bad_time": "❌ Vaqt noto‘g‘ri. Masalan: 14:00",
        "work_hours": "⚠️ Ish vaqti 09:00–22:00. Qaytadan kiriting:",
        "empty": "❌ Bo‘sh. Qaytadan kiriting:",
        "my_none": "Hozircha arizalar yo‘q.",
        "my_title": "📄 Arizalaringiz:",
        "admin_panel_reception": "✅ Panel: Resepshen",
        "admin_panel_spa": "✅ Panel: SPA",
        "logout": "Chiqdingiz ✅",
        "need_room": "Xona raqamini kiriting (masalan 101):",
        "need_amount": "Summani kiriting (faqat raqam), masalan 50000:",
        "need_note": "Izoh (yoki '-' bo‘lsa kerak emas):",
        "folio_enter_room": "Xona raqamini kiriting:",
        "bad_room": "❌ Xona raqami noto‘g‘ri. Faqat raqam (masalan 101):",
        "bad_amount": "❌ Summa noto‘g‘ri. Faqat raqam (masalan 50000):",
        "folio_empty": "Yozuvlar yo‘q.",
        "today_none": "Bugun bronlar yo‘q ✅",
        "ask_ddmm": "Sana kiriting DD.MM (masalan 20.02):",
        "ask_delete_id": "Bron ID kiriting (masalan 12):",
        "deleted": "✅ O‘chirildi",
        "not_found": "⚠️ Topilmadi",
        "only_number": "❌ Raqam bo‘lishi kerak. Masalan 12:",
        "added_booking": "✅ Bron qo‘shildi:",
        "on_date_none": "Bu sanada bron yo‘q ✅",
    }
}

def t(lang: str, key: str) -> str:
    lang = lang if lang in TXT else "ru"
    return TXT[lang].get(key, key)

# ---------------- helpers ----------------
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

# ---------------- Keyboards ----------------
def kb_lang():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🇷🇺 Русский"), KeyboardButton(text="🇺🇿 O‘zbek")],
        ],
        resize_keyboard=True
    )

def kb_client(lang: str):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(lang, "btn_room")), KeyboardButton(text=t(lang, "btn_spa"))],
            [KeyboardButton(text=t(lang, "btn_my")), KeyboardButton(text=t(lang, "btn_contact"))],
        ],
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
class ChooseLangFlow(StatesGroup):
    waiting_lang = State()

class RoomRequestFlow(StatesGroup):
    waiting_text = State()

class SpaRequestFlow(StatesGroup):
    waiting_date = State()
    waiting_time = State()
    waiting_text = State()

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
    # pins must be 10 digits
    if not (is_10_digits(PIN_RECEPTION) and is_10_digits(PIN_SPA)):
        raise RuntimeError("PIN_RECEPTION и PIN_SPA должны быть ровно 10 цифр (Render Environment).")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            role TEXT,
            created_at TEXT
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS pins (
            role TEXT PRIMARY KEY,
            pin_hash TEXT NOT NULL
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS prefs (
            user_id INTEGER PRIMARY KEY,
            lang TEXT NOT NULL
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS client_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,           -- room/spa
            ts TEXT NOT NULL,             -- desired datetime or created time
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
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

        await db.execute("INSERT OR REPLACE INTO pins(role, pin_hash) VALUES(?,?)", (ROLE_RECEPTION, sha256(PIN_RECEPTION)))
        await db.execute("INSERT OR REPLACE INTO pins(role, pin_hash) VALUES(?,?)", (ROLE_SPA, sha256(PIN_SPA)))
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
        await db.execute("UPDATE users SET role=NULL WHERE user_id=?", (user_id,))
        await db.commit()

async def check_pin(pin: str) -> str | None:
    h = sha256(pin.strip())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT role FROM pins WHERE pin_hash=?", (h,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

async def set_lang(user_id: int, lang: str):
    lang = lang if lang in ("ru", "uz") else "ru"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO prefs(user_id, lang) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET lang=excluded.lang",
            (user_id, lang),
        )
        await db.commit()

async def get_lang(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT lang FROM prefs WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

async def list_admins(role: str | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if role:
            async with db.execute("SELECT user_id FROM users WHERE role=?", (role,)) as cur:
                return [int(r[0]) for r in await cur.fetchall()]
        else:
            async with db.execute("SELECT user_id FROM users WHERE role IS NOT NULL") as cur:
                return [int(r[0]) for r in await cur.fetchall()]

async def add_client_request(user_id: int, kind: str, ts: str, text: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO client_requests(user_id, kind, ts, text, created_at) VALUES(?,?,?,?,?)",
            (user_id, kind, ts, text, now_tz().isoformat()),
        )
        await db.commit()
        return cur.lastrowid

async def list_my_requests(user_id: int, limit: int = 30):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, kind, ts, text, created_at FROM client_requests WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
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

# ---------------- Notifications ----------------
async def send_today_bookings(bot: Bot):
    admins = await list_admins()  # кто уже вошёл как админ хотя бы раз
    if not admins:
        return

    today = now_tz()
    start, end = day_range(today)
    rows = await list_bookings_between(start, end)

    msg = "📅 Сегодня броней SPA нет ✅"
    if rows:
        lines = ["📅 Брони SPA на сегодня:"]
        for bid, ts, txt in rows:
            dt = datetime.fromisoformat(ts)
            lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {txt}")
        msg = "\n".join(lines)

    for uid in admins:
        try:
            await bot.send_message(uid, msg)
        except Exception:
            pass

async def send_one_hour_reminders(bot: Bot):
    admins = await list_admins()
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
        msg = f"⏰ Через 1 час бронь SPA: {dt.strftime('%d.%m %H:%M')} — {txt} (#{bid})"
        for uid in admins:
            try:
                await bot.send_message(uid, msg)
            except Exception:
                pass
        await mark_booking_reminded(int(bid))

# ---------------- Render Web server (for Web Service) ----------------
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

# ---------------- Main bot ----------------
async def run_bot():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN пустой (Render Environment).")

    await init_db()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    scheduler = AsyncIOScheduler(timezone=TZ)
    mt = parse_hhmm(MORNING_TIME)
    scheduler.add_job(send_today_bookings, "cron", hour=mt.hour, minute=mt.minute, args=[bot], id="morning", replace_existing=True)
    scheduler.add_job(send_one_hour_reminders, "interval", minutes=1, args=[bot], id="reminders", replace_existing=True)
    scheduler.start()

    async def show_client_menu(m: Message, lang: str):
        await m.answer(t(lang, "menu"), reply_markup=kb_client(lang))

    async def ensure_lang_or_ask(m: Message, state: FSMContext) -> str | None:
        lang = await get_lang(m.from_user.id)
        if lang:
            return lang
        await state.clear()
        await state.set_state(ChooseLangFlow.waiting_lang)
        await m.answer(TXT["ru"]["hello_choose_lang"], reply_markup=kb_lang())
        return None

    async def admin_or_client_menu(m: Message, state: FSMContext):
        role = await get_role(m.from_user.id)
        if role == ROLE_RECEPTION:
            await m.answer(TXT["ru"]["admin_panel_reception"], reply_markup=kb_reception())
            return
        if role == ROLE_SPA:
            await m.answer(TXT["ru"]["admin_panel_spa"], reply_markup=kb_spa())
            return

        lang = await ensure_lang_or_ask(m, state)
        if not lang:
            return
        await show_client_menu(m, lang)

    # /start
    @dp.message(Command("start"))
    async def start(m: Message, state: FSMContext):
        await state.clear()
        await admin_or_client_menu(m, state)

    # language selection
    @dp.message(ChooseLangFlow.waiting_lang)
    async def choose_lang(m: Message, state: FSMContext):
        text = (m.text or "").strip()
        if "Рус" in text:
            lang = "ru"
        elif "O‘z" in text or "Uz" in text or "ўз" in text.lower():
            lang = "uz"
        else:
            await m.answer(TXT["ru"]["hello_choose_lang"], reply_markup=kb_lang())
            return

        await set_lang(m.from_user.id, lang)
        await state.clear()
        await show_client_menu(m, lang)

    # Admin secret PIN: send 10 digits anytime (no hints for clients)
    @dp.message(F.text.regexp(r"^\d{10}$"))
    async def secret_pin(m: Message, state: FSMContext):
        pin = (m.text or "").strip()
        role = await check_pin(pin)
        if role:
            await set_role(m.from_user.id, role)
            await state.clear()
            if role == ROLE_RECEPTION:
                await m.answer(TXT["ru"]["admin_panel_reception"], reply_markup=kb_reception())
            else:
                await m.answer(TXT["ru"]["admin_panel_spa"], reply_markup=kb_spa())
            return
        # если это просто 10 цифр (не PIN) — ведём как обычное сообщение:
        await admin_or_client_menu(m, state)

    # Admin logout
    @dp.message(F.text == "🚪 Выйти")
    async def admin_logout(m: Message, state: FSMContext):
        await state.clear()
        await clear_role(m.from_user.id)
        # вернём в клиентское меню
        lang = await ensure_lang_or_ask(m, state)
        if not lang:
            return
        await m.answer(t(lang, "logout"), reply_markup=kb_client(lang))

    # ---------------- CLIENT MENU ----------------
    @dp.message()
    async def router(m: Message, state: FSMContext):
        role = await get_role(m.from_user.id)

        # ---------- ADMIN ROUTES ----------
        if role == ROLE_RECEPTION:
            return await reception_routes(m, state)
        if role == ROLE_SPA:
            return await spa_routes(m, state)

        # ---------- CLIENT ROUTES ----------
        lang = await ensure_lang_or_ask(m, state)
        if not lang:
            return

        txt = (m.text or "").strip()

        if txt == t(lang, "btn_contact"):
            # общая связь (для комнаты)
            await m.answer(t(lang, "contact_room"), reply_markup=kb_client(lang))
            return

        if txt == t(lang, "btn_room"):
            await state.clear()
            await state.set_state(RoomRequestFlow.waiting_text)
            await m.answer(t(lang, "room_start"))
            return

        if txt == t(lang, "btn_spa"):
            await state.clear()
            await state.set_state(SpaRequestFlow.waiting_date)
            await m.answer(t(lang, "spa_start"))
            return

        if txt == t(lang, "btn_my"):
            rows = await list_my_requests(m.from_user.id)
            if not rows:
                await m.answer(t(lang, "my_none"), reply_markup=kb_client(lang))
                return
            lines = [t(lang, "my_title")]
            for rid, kind, ts, text2, created_at in rows:
                kind_name = "SPA" if kind == "spa" else ("ROOM" if kind == "room" else kind)
                lines.append(f"#{rid} [{kind_name}] {ts} — {text2}")
            await m.answer("\n".join(lines), reply_markup=kb_client(lang))
            return

        # if none matched -> show menu
        await show_client_menu(m, lang)

    # ---------------- CLIENT FLOWS ----------------
    @dp.message(RoomRequestFlow.waiting_text)
    async def room_request_text(m: Message, state: FSMContext):
        lang = await get_lang(m.from_user.id) or "ru"
        text = (m.text or "").strip()
        if not text:
            await m.answer(t(lang, "empty"))
            return

        rid = await add_client_request(m.from_user.id, "room", now_tz().strftime("%d.%m %H:%M"), text)

        # forward to reception admins (who have logged in at least once)
        admins = await list_admins(ROLE_RECEPTION)
        fwd = f"🏨 ROOM REQUEST #{rid}\nFrom: {m.from_user.id}\nText: {text}"
        for uid in admins:
            try:
                await m.bot.send_message(uid, fwd)
            except Exception:
                pass

        await state.clear()
        await m.answer(t(lang, "sent_room"), reply_markup=kb_client(lang))

    @dp.message(SpaRequestFlow.waiting_date)
    async def spa_req_date(m: Message, state: FSMContext):
        lang = await get_lang(m.from_user.id) or "ru"
        ddmm = (m.text or "").strip()
        try:
            d, mo = parse_ddmm(ddmm)
        except Exception:
            await m.answer(t(lang, "bad_date"))
            return

        n = now_tz()
        day = datetime(n.year, mo, d, 0, 0, tzinfo=TZ)
        if day.date() < n.date():
            await m.answer(t(lang, "past_date"))
            return

        await state.update_data(ddmm=ddmm)
        await state.set_state(SpaRequestFlow.waiting_time)
        await m.answer(t(lang, "spa_time"))

    @dp.message(SpaRequestFlow.waiting_time)
    async def spa_req_time(m: Message, state: FSMContext):
        lang = await get_lang(m.from_user.id) or "ru"
        hhmm = (m.text or "").strip()
        try:
            tm = parse_hhmm(hhmm)
        except Exception:
            await m.answer(t(lang, "bad_time"))
            return

        if not in_working_hours(tm):
            await m.answer(t(lang, "work_hours"))
            return

        data = await state.get_data()
        dt = make_dt_current_year(data["ddmm"], hhmm)
        if dt <= now_tz():
            await m.answer(t(lang, "work_hours"))
            return

        await state.update_data(hhmm=hhmm)
        await state.set_state(SpaRequestFlow.waiting_text)
        await m.answer(t(lang, "spa_text"))

    @dp.message(SpaRequestFlow.waiting_text)
    async def spa_req_text(m: Message, state: FSMContext):
        lang = await get_lang(m.from_user.id) or "ru"
        text = (m.text or "").strip()
        if not text:
            await m.answer(t(lang, "empty"))
            return

        data = await state.get_data()
        dt = make_dt_current_year(data["ddmm"], data["hhmm"])
        rid = await add_client_request(m.from_user.id, "spa", dt.strftime("%d.%m %H:%M"), text)

        # forward to SPA admins
        admins = await list_admins(ROLE_SPA)
        fwd = f"🧖 SPA REQUEST #{rid}\nWhen: {dt.strftime('%d.%m %H:%M')}\nFrom: {m.from_user.id}\nText: {text}\nPhone: {PHONE_SPA}"
        for uid in admins:
            try:
                await m.bot.send_message(uid, fwd)
            except Exception:
                pass

        await state.clear()
        await m.answer(t(lang, "sent_spa"), reply_markup=kb_client(lang))

    # ---------------- ADMIN ROUTES ----------------
    async def reception_routes(m: Message, state: FSMContext):
        txt = (m.text or "").strip()

        if txt == "➕ Счёт: Ресторан":
            await state.clear()
            await state.set_state(FolioAddFlow.waiting_room)
            await state.update_data(category=CAT_RESTAURANT, created_by_role=ROLE_RECEPTION)
            await m.answer(TXT["ru"]["need_room"])
            return

        if txt == "➕ Счёт: Прачка":
            await state.clear()
            await state.set_state(FolioAddFlow.waiting_room)
            await state.update_data(category=CAT_LAUNDRY, created_by_role=ROLE_RECEPTION)
            await m.answer(TXT["ru"]["need_room"])
            return

        if txt == "➕ Счёт: SPA (фолио)":
            await state.clear()
            await state.set_state(FolioAddFlow.waiting_room)
            await state.update_data(category=CAT_SPA, created_by_role=ROLE_RECEPTION)
            await m.answer(TXT["ru"]["need_room"])
            return

        if txt == "📄 Фолио по номеру":
            await state.clear()
            await state.set_state(FolioViewFlow.waiting_room)
            await m.answer(TXT["ru"]["folio_enter_room"])
            return

        # Folio add flow
        if await state.get_state() == FolioAddFlow.waiting_room.state:
            try:
                room = parse_room(m.text)
            except Exception:
                await m.answer(TXT["ru"]["bad_room"])
                return
            await state.update_data(room=room)
            await state.set_state(FolioAddFlow.waiting_amount)
            await m.answer(TXT["ru"]["need_amount"])
            return

        if await state.get_state() == FolioAddFlow.waiting_amount.state:
            try:
                amount = parse_amount(m.text)
            except Exception:
                await m.answer(TXT["ru"]["bad_amount"])
                return
            await state.update_data(amount=amount)
            await state.set_state(FolioAddFlow.waiting_note)
            await m.answer(TXT["ru"]["need_note"])
            return

        if await state.get_state() == FolioAddFlow.waiting_note.state:
            data = await state.get_data()
            category = data["category"]
            room = data["room"]
            amount = int(data["amount"])
            note = (m.text or "").strip()
            if note == "-" or note == "":
                note = None
            fid = await folio_add(room, category, amount, note, ROLE_RECEPTION, m.from_user.id)
            total = await folio_total_by_room(room)
            await state.clear()
            await m.answer(f"✅ Добавлено (#{fid})\nКомната: {room}\nКатегория: {category}\nСумма: {amount}\nИтого по комнате: {total}", reply_markup=kb_reception())
            return

        # Folio view
        if await state.get_state() == FolioViewFlow.waiting_room.state:
            try:
                room = parse_room(m.text)
            except Exception:
                await m.answer(TXT["ru"]["bad_room"])
                return
            rows = await folio_list_by_room(room)
            total = await folio_total_by_room(room)
            await state.clear()
            if not rows:
                await m.answer(f"Комната {room}: {TXT['ru']['folio_empty']}\nИтого: {total}", reply_markup=kb_reception())
                return
            lines = [f"📄 Фолио комнаты {room} (итого: {total})"]
            for fid, cat, amt, note, by_role, created_at in rows:
                dt = datetime.fromisoformat(created_at).astimezone(TZ)
                extra = f" — {note}" if note else ""
                lines.append(f"#{fid} | {cat} | {amt} | {dt.strftime('%d.%m %H:%M')} ({by_role}){extra}")
            await m.answer("\n".join(lines), reply_markup=kb_reception())
            return

        # default
        await m.answer(TXT["ru"]["admin_panel_reception"], reply_markup=kb_reception())

    async def spa_routes(m: Message, state: FSMContext):
        txt = (m.text or "").strip()

        if txt == "➕ Добавить бронь":
            await state.clear()
            await state.set_state(SpaAddBookingFlow.waiting_date)
            await m.answer(TXT["ru"]["ask_ddmm"])
            return

        if txt == "📅 Сегодня":
            today = now_tz()
            start, end = day_range(today)
            rows = await list_bookings_between(start, end)
            if not rows:
                await m.answer(TXT["ru"]["today_none"], reply_markup=kb_spa())
                return
            lines = ["📅 Брони на сегодня:"]
            for bid, ts, text2 in rows:
                dt = datetime.fromisoformat(ts)
                lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {text2}")
            await m.answer("\n".join(lines), reply_markup=kb_spa())
            return

        if txt == "📆 Брони на дату":
            await state.clear()
            await state.set_state(SpaOnDateFlow.waiting_date)
            await m.answer(TXT["ru"]["ask_ddmm"])
            return

        if txt == "🗑 Удалить бронь":
            await state.clear()
            await state.set_state(SpaDeleteBookingFlow.waiting_id)
            await m.answer(TXT["ru"]["ask_delete_id"])
            return

        if txt == "➕ Счёт: SPA (фолио)":
            await state.clear()
            await state.set_state(FolioAddFlow.waiting_room)
            await state.update_data(category=CAT_SPA, created_by_role=ROLE_SPA)
            await m.answer(TXT["ru"]["need_room"])
            return

        if txt == "📄 Фолио по номеру":
            await state.clear()
            await state.set_state(FolioViewFlow.waiting_room)
            await m.answer(TXT["ru"]["folio_enter_room"])
            return

        # Spa add booking flow
        if await state.get_state() == SpaAddBookingFlow.waiting_date.state:
            ddmm = (m.text or "").strip()
            try:
                d, mo = parse_ddmm(ddmm)
            except Exception:
                await m.answer(TXT["ru"]["bad_date"])
                return
            n = now_tz()
            day = datetime(n.year, mo, d, 0, 0, tzinfo=TZ)
            if day.date() < n.date():
                await m.answer(TXT["ru"]["past_date"])
                return
            await state.update_data(ddmm=ddmm)
            await state.set_state(SpaAddBookingFlow.waiting_time)
            await m.answer("Введите время ЧЧ:ММ (например 14:00):")
            return

        if await state.get_state() == SpaAddBookingFlow.waiting_time.state:
            hhmm = (m.text or "").strip()
            try:
                tm = parse_hhmm(hhmm)
            except Exception:
                await m.answer(TXT["ru"]["bad_time"])
                return
            if not in_working_hours(tm):
                await m.answer(TXT["ru"]["work_hours"])
                return
            data = await state.get_data()
            dt = make_dt_current_year(data["ddmm"], hhmm)
            if dt <= now_tz():
                await m.answer(TXT["ru"]["work_hours"])
                return
            await state.update_data(hhmm=hhmm)
            await state.set_state(SpaAddBookingFlow.waiting_text)
            await m.answer("Введите текст брони (пример: Массаж; Имя; Телефон):")
            return

        if await state.get_state() == SpaAddBookingFlow.waiting_text.state:
            text2 = (m.text or "").strip()
            if not text2:
                await m.answer(TXT["ru"]["empty"])
                return
            data = await state.get_data()
            dt = make_dt_current_year(data["ddmm"], data["hhmm"])
            bid = await add_booking(dt, text2, m.from_user.id)
            await state.clear()
            await m.answer(f"{TXT['ru']['added_booking']} #{bid} — {dt.strftime('%d.%m %H:%M')} — {text2}", reply_markup=kb_spa())
            return

        # Spa on date flow
        if await state.get_state() == SpaOnDateFlow.waiting_date.state:
            ddmm = (m.text or "").strip()
            try:
                d, mo = parse_ddmm(ddmm)
            except Exception:
                await m.answer(TXT["ru"]["bad_date"])
                return
            n = now_tz()
            day = datetime(n.year, mo, d, 0, 0, tzinfo=TZ)
            start, end = day_range(day)
            rows = await list_bookings_between(start, end)
            await state.clear()
            if not rows:
                await m.answer(TXT["ru"]["on_date_none"], reply_markup=kb_spa())
                return
            lines = [f"📆 Брони на {ddmm}:"]
            for bid, ts, text2 in rows:
                dt = datetime.fromisoformat(ts)
                lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {text2}")
            await m.answer("\n".join(lines), reply_markup=kb_spa())
            return

        # Spa delete flow
        if await state.get_state() == SpaDeleteBookingFlow.waiting_id.state:
            s = (m.text or "").strip()
            if not s.isdigit():
                await m.answer(TXT["ru"]["only_number"])
                return
            ok = await delete_booking(int(s))
            await state.clear()
            await m.answer(TXT["ru"]["deleted"] if ok else TXT["ru"]["not_found"], reply_markup=kb_spa())
            return

        # Folio add/view flows (same as reception but role=SPA)
        if await state.get_state() == FolioAddFlow.waiting_room.state:
            try:
                room = parse_room(m.text)
            except Exception:
                await m.answer(TXT["ru"]["bad_room"])
                return
            await state.update_data(room=room)
            await state.set_state(FolioAddFlow.waiting_amount)
            await m.answer(TXT["ru"]["need_amount"])
            return

        if await state.get_state() == FolioAddFlow.waiting_amount.state:
            try:
                amount = parse_amount(m.text)
            except Exception:
                await m.answer(TXT["ru"]["bad_amount"])
                return
            await state.update_data(amount=amount)
            await state.set_state(FolioAddFlow.waiting_note)
            await m.answer(TXT["ru"]["need_note"])
            return

        if await state.get_state() == FolioAddFlow.waiting_note.state:
            data = await state.get_data()
            category = data["category"]
            room = data["room"]
            amount = int(data["amount"])
            note = (m.text or "").strip()
            if note == "-" or note == "":
                note = None
            fid = await folio_add(room, category, amount, note, ROLE_SPA, m.from_user.id)
            total = await folio_total_by_room(room)
            await state.clear()
            await m.answer(f"✅ Добавлено (#{fid})\nКомната: {room}\nКатегория: {category}\nСумма: {amount}\nИтого по комнате: {total}", reply_markup=kb_spa())
            return

        if await state.get_state() == FolioViewFlow.waiting_room.state:
            try:
                room = parse_room(m.text)
            except Exception:
                await m.answer(TXT["ru"]["bad_room"])
                return
            rows = await folio_list_by_room(room)
            total = await folio_total_by_room(room)
            await state.clear()
            if not rows:
                await m.answer(f"Комната {room}: {TXT['ru']['folio_empty']}\nИтого: {total}", reply_markup=kb_spa())
                return
            lines = [f"📄 Фолио комнаты {room} (итого: {total})"]
            for fid, cat, amt, note, by_role, created_at in rows:
                dt = datetime.fromisoformat(created_at).astimezone(TZ)
                extra = f" — {note}" if note else ""
                lines.append(f"#{fid} | {cat} | {amt} | {dt.strftime('%d.%m %H:%M')} ({by_role}){extra}")
            await m.answer("\n".join(lines), reply_markup=kb_spa())
            return

        # default
        await m.answer(TXT["ru"]["admin_panel_spa"], reply_markup=kb_spa())

    await dp.start_polling(bot)

async def main():
    await asyncio.gather(run_web_server(), run_bot())

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN пустой.")
    asyncio.run(main())