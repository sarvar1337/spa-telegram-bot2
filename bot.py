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

PHONE_RECEPTION = "+998906338900"   # бронь комнаты
PHONE_SPA = "+998916768900"         # бронь SPA

OPEN_TIME = dtime(9, 0)
CLOSE_TIME = dtime(22, 0)


# ---------------- i18n (client only) ----------------
TXT = {
    "ru": {
        "hello_choose_lang": "Здравствуйте! 👋\nВыберите язык / Tilni tanlang:",
        "btn_room": "🏨 Бронь комнаты",
        "btn_spa": "🧖 Бронь SPA",
        "room_phone": f"🏨 Бронь комнаты:\n📞 {PHONE_RECEPTION}",
        "spa_phone": f"🧖 Бронь SPA:\n📞 {PHONE_SPA}",
        "menu_client": "Выберите, что нужно:",
    },
    "uz": {
        "hello_choose_lang": "Assalomu alaykum! 👋\nTilni tanlang / Выберите язык:",
        "btn_room": "🏨 Xona bron",
        "btn_spa": "🧖 SPA bron",
        "room_phone": f"🏨 Xona bron:\n📞 {PHONE_RECEPTION}",
        "spa_phone": f"🧖 SPA bron:\n📞 {PHONE_SPA}",
        "menu_client": "Keraklisini tanlang:",
    }
}

def t(lang: str, key: str) -> str:
    return TXT.get(lang, TXT["ru"]).get(key, key)


# ---------------- helpers ----------------
def now_tz() -> datetime:
    return datetime.now(TZ)

def is_10_digits(s: str) -> bool:
    return bool(re.fullmatch(r"\d{10}", (s or "").strip()))

def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

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
        keyboard=[[KeyboardButton(text="🇷🇺 Русский"), KeyboardButton(text="🇺🇿 O‘zbek")]],
        resize_keyboard=True
    )

def kb_client(lang: str):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(lang, "btn_spa")), KeyboardButton(text=t(lang, "btn_room"))],
        ],
        resize_keyboard=True
    )

def kb_reception():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Счёт: Ресторан"), KeyboardButton(text="➕ Счёт: Прачка")],
            [KeyboardButton(text="➕ Счёт: SPA (фолио)"), KeyboardButton(text="📄 Фолио по номеру")],
            [KeyboardButton(text="✅ Оплатил (по ID)"), KeyboardButton(text="❌ Не оплатил (по ID)")],
            [KeyboardButton(text="🗑 Удалить счёт (по ID)")],
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
            [KeyboardButton(text="✅ Оплатил (по ID)"), KeyboardButton(text="❌ Не оплатил (по ID)")],
            [KeyboardButton(text="🗑 Удалить счёт (по ID)")],
            [KeyboardButton(text="🚪 Выйти")],
        ],
        resize_keyboard=True
    )


# ---------------- FSM ----------------
class ChooseLangFlow(StatesGroup):
    waiting_lang = State()

class FolioAddFlow(StatesGroup):
    waiting_room = State()
    waiting_amount = State()
    waiting_note = State()

class FolioViewFlow(StatesGroup):
    waiting_room = State()

class FolioPaidFlow(StatesGroup):
    waiting_id = State()

class FolioUnpaidFlow(StatesGroup):
    waiting_id = State()

class FolioDeleteFlow(StatesGroup):
    waiting_id = State()


# ---------------- DB ----------------
async def init_db():
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

        # миграция: добавим paid, если таблица старая
        try:
            await db.execute("ALTER TABLE folio ADD COLUMN paid INTEGER DEFAULT 0")
        except Exception:
            pass

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
        async with db.execute("SELECT user_id FROM users WHERE role IS NOT NULL") as cur:
            return [int(r[0]) for r in await cur.fetchall()]

async def folio_add(room: str, category: str, amount: int, note: str | None, created_by_role: str, created_by_user: int | None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO folio(room,category,amount,note,created_by_role,created_by_user,created_at,paid) VALUES(?,?,?,?,?,?,?,0)",
            (room, category, amount, note, created_by_role, created_by_user, now_tz().isoformat()),
        )
        await db.commit()
        return cur.lastrowid

async def folio_list_by_room(room: str, limit: int = 100):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, category, amount, note, created_by_role, created_at, COALESCE(paid,0) "
            "FROM folio WHERE room=? ORDER BY created_at DESC LIMIT ?",
            (room, limit),
        ) as cur:
            return await cur.fetchall()

async def folio_total_by_room(room: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(SUM(amount),0) FROM folio WHERE room=?", (room,)) as cur:
            row = await cur.fetchone()
            return int(row[0] or 0)

async def folio_unpaid_total_by_room(room: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(SUM(amount),0) FROM folio WHERE room=? AND COALESCE(paid,0)=0", (room,)) as cur:
            row = await cur.fetchone()
            return int(row[0] or 0)

async def folio_set_paid(fid: int, paid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("UPDATE folio SET paid=? WHERE id=?", (1 if paid else 0, fid))
        await db.commit()
        return cur.rowcount > 0

async def folio_delete(fid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM folio WHERE id=?", (fid,))
        await db.commit()
        return cur.rowcount > 0

async def folio_unpaid_rooms(limit: int = 50):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT room, SUM(amount) as total "
            "FROM folio WHERE COALESCE(paid,0)=0 GROUP BY room HAVING total>0 ORDER BY total DESC LIMIT ?",
            (limit,),
        ) as cur:
            return await cur.fetchall()


# ---------------- Debt reminders ----------------
async def send_debt_report(bot: Bot, title: str):
    admins = await list_admins(ROLE_RECEPTION)  # долги нужны ресепшену (главному)
    if not admins:
        return

    rows = await folio_unpaid_rooms(limit=80)
    if not rows:
        msg = f"💳 {title}\n✅ Неоплаченных долгов нет."
    else:
        lines = [f"💳 {title}", "Неоплаченные долги по комнатам:"]
        for room, total in rows:
            lines.append(f"• Комната {room}: {int(total)}")
        msg = "\n".join(lines)

    for uid in admins:
        try:
            await bot.send_message(uid, msg)
        except Exception:
            pass


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

    # Scheduler: 09:00 и 19:00 долг-репорт
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(send_debt_report, "cron", hour=9, minute=0, args=[bot, "Отчёт по долгам (09:00)"], id="debt_9", replace_existing=True)
    scheduler.add_job(send_debt_report, "cron", hour=19, minute=0, args=[bot, "Отчёт по долгам (19:00)"], id="debt_19", replace_existing=True)
    scheduler.start()

    async def show_client_or_lang(m: Message, state: FSMContext):
        role = await get_role(m.from_user.id)
        if role == ROLE_RECEPTION:
            await m.answer("✅ Панель: Ресепшен", reply_markup=kb_reception())
            return
        if role == ROLE_SPA:
            await m.answer("✅ Панель: SPA", reply_markup=kb_spa())
            return

        lang = await get_lang(m.from_user.id)
        if not lang:
            await state.clear()
            await state.set_state(ChooseLangFlow.waiting_lang)
            await m.answer(TXT["ru"]["hello_choose_lang"], reply_markup=kb_lang())
            return
        await m.answer(t(lang, "menu_client"), reply_markup=kb_client(lang))

    @dp.message(Command("start"))
    async def start(m: Message, state: FSMContext):
        await state.clear()
        await show_client_or_lang(m, state)

    # выбор языка 1 раз
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
        await m.answer(t(lang, "menu_client"), reply_markup=kb_client(lang))

    # секретный PIN (админ вводит 10 цифр)
    @dp.message(F.text.regexp(r"^\d{10}$"))
    async def secret_pin(m: Message, state: FSMContext):
        pin = (m.text or "").strip()
        role = await check_pin(pin)
        if role:
            await set_role(m.from_user.id, role)
            await state.clear()
            if role == ROLE_RECEPTION:
                await m.answer("✅ Панель: Ресепшен", reply_markup=kb_reception())
            else:
                await m.answer("✅ Панель: SPA", reply_markup=kb_spa())
            return
        # если это не PIN — просто покажем меню
        await show_client_or_lang(m, state)

    # выход админа
    @dp.message(F.text == "🚪 Выйти")
    async def admin_logout(m: Message, state: FSMContext):
        await state.clear()
        await clear_role(m.from_user.id)
        await show_client_or_lang(m, state)

    # ---------------- ADMIN actions ----------------
    async def admin_common_routes(m: Message, state: FSMContext, role: str):
        # mark paid
        if m.text == "✅ Оплатил (по ID)":
            await state.clear()
            await state.set_state(FolioPaidFlow.waiting_id)
            await m.answer("Введите ID счёта (например 4):")
            return True

        if m.text == "❌ Не оплатил (по ID)":
            await state.clear()
            await state.set_state(FolioUnpaidFlow.waiting_id)
            await m.answer("Введите ID счёта (например 4):")
            return True

        if m.text == "🗑 Удалить счёт (по ID)":
            await state.clear()
            await state.set_state(FolioDeleteFlow.waiting_id)
            await m.answer("Введите ID счёта для удаления (например 4):")
            return True

        st = await state.get_state()
        if st == FolioPaidFlow.waiting_id.state:
            s = (m.text or "").strip()
            if not s.isdigit():
                await m.answer("Нужно число ID. Например 4:")
                return True
            ok = await folio_set_paid(int(s), 1)
            await state.clear()
            await m.answer("✅ Отмечено как ОПЛАЧЕНО" if ok else "⚠️ ID не найден",
                           reply_markup=kb_reception() if role == ROLE_RECEPTION else kb_spa())
            return True

        if st == FolioUnpaidFlow.waiting_id.state:
            s = (m.text or "").strip()
            if not s.isdigit():
                await m.answer("Нужно число ID. Например 4:")
                return True
            ok = await folio_set_paid(int(s), 0)
            await state.clear()
            await m.answer("✅ Отмечено как НЕ ОПЛАЧЕНО" if ok else "⚠️ ID не найден",
                           reply_markup=kb_reception() if role == ROLE_RECEPTION else kb_spa())
            return True

        if st == FolioDeleteFlow.waiting_id.state:
            s = (m.text or "").strip()
            if not s.isdigit():
                await m.answer("Нужно число ID. Например 4:")
                return True
            ok = await folio_delete(int(s))
            await state.clear()
            await m.answer("🗑 Удалено" if ok else "⚠️ ID не найден",
                           reply_markup=kb_reception() if role == ROLE_RECEPTION else kb_spa())
            return True

        return False

    async def reception_routes(m: Message, state: FSMContext):
        if await admin_common_routes(m, state, ROLE_RECEPTION):
            return

        txt = (m.text or "").strip()

        if txt == "➕ Счёт: Ресторан":
            await state.clear()
            await state.set_state(FolioAddFlow.waiting_room)
            await state.update_data(category=CAT_RESTAURANT, created_by_role=ROLE_RECEPTION)
            await m.answer("Введите номер комнаты (например 706):")
            return

        if txt == "➕ Счёт: Прачка":
            await state.clear()
            await state.set_state(FolioAddFlow.waiting_room)
            await state.update_data(category=CAT_LAUNDRY, created_by_role=ROLE_RECEPTION)
            await m.answer("Введите номер комнаты (например 706):")
            return

        if txt == "➕ Счёт: SPA (фолио)":
            await state.clear()
            await state.set_state(FolioAddFlow.waiting_room)
            await state.update_data(category=CAT_SPA, created_by_role=ROLE_RECEPTION)
            await m.answer("Введите номер комнаты (например 706):")
            return

        if txt == "📄 Фолио по номеру":
            await state.clear()
            await state.set_state(FolioViewFlow.waiting_room)
            await m.answer("Введите номер комнаты:")
            return

        st = await state.get_state()

        if st == FolioAddFlow.waiting_room.state:
            try:
                room = parse_room(m.text)
            except Exception:
                await m.answer("❌ Неверный номер комнаты. Только цифры (например 706):")
                return
            await state.update_data(room=room)
            await state.set_state(FolioAddFlow.waiting_amount)
            await m.answer("Введите сумму (только цифры), например 50000:")
            return

        if st == FolioAddFlow.waiting_amount.state:
            try:
                amount = parse_amount(m.text)
            except Exception:
                await m.answer("❌ Неверная сумма. Только цифры (например 50000):")
                return
            await state.update_data(amount=amount)
            await state.set_state(FolioAddFlow.waiting_note)
            await m.answer("Комментарий (или '-' если не нужно):")
            return

        if st == FolioAddFlow.waiting_note.state:
            data = await state.get_data()
            category = data["category"]
            room = data["room"]
            amount = int(data["amount"])
            note = (m.text or "").strip()
            if note in ("-", ""):
                note = None
            fid = await folio_add(room, category, amount, note, ROLE_RECEPTION, m.from_user.id)
            total = await folio_total_by_room(room)
            unpaid = await folio_unpaid_total_by_room(room)
            await state.clear()
            await m.answer(
                f"✅ Добавлено (#{fid})\nКомната: {room}\nКатегория: {category}\nСумма: {amount}\n"
                f"ДОЛГ (неоплачено): {unpaid}\nИтого по комнате: {total}",
                reply_markup=kb_reception()
            )
            return

        if st == FolioViewFlow.waiting_room.state:
            try:
                room = parse_room(m.text)
            except Exception:
                await m.answer("❌ Неверный номер комнаты. Только цифры (например 706):")
                return
            rows = await folio_list_by_room(room)
            total = await folio_total_by_room(room)
            unpaid = await folio_unpaid_total_by_room(room)
            await state.clear()
            if not rows:
                await m.answer(f"📄 Комната {room}: записей нет.\nДОЛГ (неоплачено): {unpaid}\nИтого: {total}", reply_markup=kb_reception())
                return
            lines = [f"📄 Фолио комнаты {room}", f"ДОЛГ (неоплачено): {unpaid}", f"Итого: {total}", ""]
            for fid, cat, amt, note, by_role, created_at, paid in rows:
                dt = datetime.fromisoformat(created_at).astimezone(TZ)
                stt = "✅ ОПЛ" if int(paid) == 1 else "❌ ДОЛГ"
                extra = f" — {note}" if note else ""
                lines.append(f"#{fid} | {stt} | {cat} | {amt} | {dt.strftime('%d.%m %H:%M')} ({by_role}){extra}")
            await m.answer("\n".join(lines), reply_markup=kb_reception())
            return

        await m.answer("✅ Панель: Ресепшен", reply_markup=kb_reception())

    async def spa_routes(m: Message, state: FSMContext):
        if await admin_common_routes(m, state, ROLE_SPA):
            return
        # Тут оставляем только фолио, если хочешь — добавим обратно брони SPA отдельно
        txt = (m.text or "").strip()

        if txt == "➕ Счёт: SPA (фолио)":
            await state.clear()
            await state.set_state(FolioAddFlow.waiting_room)
            await state.update_data(category=CAT_SPA, created_by_role=ROLE_SPA)
            await m.answer("Введите номер комнаты (например 706):")
            return

        if txt == "📄 Фолио по номеру":
            await state.clear()
            await state.set_state(FolioViewFlow.waiting_room)
            await m.answer("Введите номер комнаты:")
            return

        st = await state.get_state()

        if st == FolioAddFlow.waiting_room.state:
            try:
                room = parse_room(m.text)
            except Exception:
                await m.answer("❌ Неверный номер комнаты. Только цифры (например 706):")
                return
            await state.update_data(room=room)
            await state.set_state(FolioAddFlow.waiting_amount)
            await m.answer("Введите сумму (только цифры), например 50000:")
            return

        if st == FolioAddFlow.waiting_amount.state:
            try:
                amount = parse_amount(m.text)
            except Exception:
                await m.answer("❌ Неверная сумма. Только цифры (например 50000):")
                return
            await state.update_data(amount=amount)
            await state.set_state(FolioAddFlow.waiting_note)
            await m.answer("Комментарий (или '-' если не нужно):")
            return

        if st == FolioAddFlow.waiting_note.state:
            data = await state.get_data()
            category = data["category"]
            room = data["room"]
            amount = int(data["amount"])
            note = (m.text or "").strip()
            if note in ("-", ""):
                note = None
            fid = await folio_add(room, category, amount, note, ROLE_SPA, m.from_user.id)
            total = await folio_total_by_room(room)
            unpaid = await folio_unpaid_total_by_room(room)
            await state.clear()
            await m.answer(
                f"✅ Добавлено (#{fid})\nКомната: {room}\nКатегория: {category}\nСумма: {amount}\n"
                f"ДОЛГ (неоплачено): {unpaid}\nИтого по комнате: {total}",
                reply_markup=kb_spa()
            )
            return

        if st == FolioViewFlow.waiting_room.state:
            try:
                room = parse_room(m.text)
            except Exception:
                await m.answer("❌ Неверный номер комнаты. Только цифры (например 706):")
                return
            rows = await folio_list_by_room(room)
            total = await folio_total_by_room(room)
            unpaid = await folio_unpaid_total_by_room(room)
            await state.clear()
            if not rows:
                await m.answer(f"📄 Комната {room}: записей нет.\nДОЛГ (неоплачено): {unpaid}\nИтого: {total}", reply_markup=kb_spa())
                return
            lines = [f"📄 Фолио комнаты {room}", f"ДОЛГ (неоплачено): {unpaid}", f"Итого: {total}", ""]
            for fid, cat, amt, note, by_role, created_at, paid in rows:
                dt = datetime.fromisoformat(created_at).astimezone(TZ)
                stt = "✅ ОПЛ" if int(paid) == 1 else "❌ ДОЛГ"
                extra = f" — {note}" if note else ""
                lines.append(f"#{fid} | {stt} | {cat} | {amt} | {dt.strftime('%d.%m %H:%M')} ({by_role}){extra}")
            await m.answer("\n".join(lines), reply_markup=kb_spa())
            return

        await m.answer("✅ Панель: SPA", reply_markup=kb_spa())

    # общий роутер сообщений
    @dp.message()
    async def router(m: Message, state: FSMContext):
        role = await get_role(m.from_user.id)
        if role == ROLE_RECEPTION:
            await reception_routes(m, state)
            return
        if role == ROLE_SPA:
            await spa_routes(m, state)
            return

        lang = await get_lang(m.from_user.id)
        if not lang:
            await state.clear()
            await state.set_state(ChooseLangFlow.waiting_lang)
            await m.answer(TXT["ru"]["hello_choose_lang"], reply_markup=kb_lang())
            return

        text = (m.text or "").strip()
        if text == t(lang, "btn_room"):
            await m.answer(t(lang, "room_phone"), reply_markup=kb_client(lang))
            return
        if text == t(lang, "btn_spa"):
            await m.answer(t(lang, "spa_phone"), reply_markup=kb_client(lang))
            return

        await m.answer(t(lang, "menu_client"), reply_markup=kb_client(lang))

    await dp.start_polling(bot)

async def main():
    await asyncio.gather(run_web_server(), run_bot())

if __name__ == "__main__":
    asyncio.run(main())