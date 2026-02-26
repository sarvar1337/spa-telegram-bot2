import os
import re
import asyncio
import hashlib
from datetime import datetime, timedelta, date as ddate, time as dtime

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

def fmt_dt(iso: str) -> str:
    dt = datetime.fromisoformat(iso).astimezone(TZ)
    return dt.strftime("%d.%m %H:%M")

def parse_ddmm(s: str) -> tuple[int, int]:
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{2})\.(\d{2})", s)
    if not m:
        raise ValueError("bad dd.mm")
    dd = int(m.group(1))
    mm = int(m.group(2))
    if not (1 <= dd <= 31 and 1 <= mm <= 12):
        raise ValueError("bad dd.mm")
    return dd, mm

def parse_hhmm(s: str) -> tuple[int, int]:
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{2}):(\d{2})", s)
    if not m:
        raise ValueError("bad hh:mm")
    hh = int(m.group(1))
    mi = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mi <= 59):
        raise ValueError("bad hh:mm")
    return hh, mi

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
            [KeyboardButton(text="💳 Только долги")],
            [KeyboardButton(text="🚪 Выйти")],
        ],
        resize_keyboard=True
    )

def kb_spa():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить бронь"), KeyboardButton(text="🗑 Удалить бронь")],
            [KeyboardButton(text="📅 Брони на сегодня"), KeyboardButton(text="📆 Брони на дату")],
            [KeyboardButton(text="➕ Счёт: SPA (фолио)"), KeyboardButton(text="📄 Фолио по номеру")],
            [KeyboardButton(text="💳 Только долги")],
            [KeyboardButton(text="🚪 Выйти")],
        ],
        resize_keyboard=True
    )

def ik_folio_actions(fid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Оплатил", callback_data=f"folio:pay:{fid}"),
                InlineKeyboardButton(text="❌ Не оплатил", callback_data=f"folio:unpay:{fid}"),
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"folio:del:{fid}"),
                InlineKeyboardButton(text="🕘 История", callback_data=f"folio:hist:{fid}"),
            ],
        ]
    )

def ik_delete_confirm(fid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"folio:del_yes:{fid}"),
                InlineKeyboardButton(text="❌ Нет", callback_data=f"folio:del_no:{fid}"),
            ]
        ]
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

class SpaAddBookingFlow(StatesGroup):
    waiting_date = State()
    waiting_time = State()
    waiting_text = State()

class SpaBookingsByDateFlow(StatesGroup):
    waiting_date = State()

class SpaDeleteBookingFlow(StatesGroup):
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

        # folio migrations
        try:
            await db.execute("ALTER TABLE folio ADD COLUMN paid INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE folio ADD COLUMN paid_at TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE folio ADD COLUMN paid_by_user INTEGER")
        except Exception:
            pass

        await db.execute("""
        CREATE TABLE IF NOT EXISTS folio_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folio_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            by_role TEXT,
            by_user INTEGER,
            at TEXT NOT NULL,
            note TEXT
        )""")

        # SPA bookings
        await db.execute("""
        CREATE TABLE IF NOT EXISTS spa_bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts TEXT NOT NULL,
            text TEXT NOT NULL,
            created_by_user INTEGER,
            created_at TEXT NOT NULL,
            reminded_1h INTEGER DEFAULT 0
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
        async with db.execute("SELECT user_id FROM users WHERE role IS NOT NULL") as cur:
            return [int(r[0]) for r in await cur.fetchall()]

# ------- folio helpers -------
async def folio_event_add(folio_id: int, action: str, by_role: str | None, by_user: int | None, note: str | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO folio_events(folio_id, action, by_role, by_user, at, note) VALUES(?,?,?,?,?,?)",
            (folio_id, action, by_role, by_user, now_tz().isoformat(), note),
        )
        await db.commit()

async def folio_add(room: str, category: str, amount: int, note: str | None, created_by_role: str, created_by_user: int | None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO folio(room,category,amount,note,created_by_role,created_by_user,created_at,paid,paid_at,paid_by_user) "
            "VALUES(?,?,?,?,?,?,?,0,NULL,NULL)",
            (room, category, amount, note, created_by_role, created_by_user, now_tz().isoformat()),
        )
        fid = cur.lastrowid
        await db.commit()
    await folio_event_add(fid, "create", created_by_role, created_by_user, note)
    return fid

async def folio_get(fid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, room, category, amount, note, created_by_role, created_by_user, created_at, COALESCE(paid,0), paid_at, paid_by_user "
            "FROM folio WHERE id=?",
            (fid,),
        ) as cur:
            return await cur.fetchone()

async def folio_list_by_room(room: str, limit: int = 200):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, category, amount, note, created_by_role, created_at, COALESCE(paid,0), paid_at, paid_by_user "
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

async def folio_set_paid(fid: int, paid: int, by_role: str, by_user: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        if paid:
            cur = await db.execute(
                "UPDATE folio SET paid=1, paid_at=?, paid_by_user=? WHERE id=?",
                (now_tz().isoformat(), by_user, fid),
            )
        else:
            cur = await db.execute(
                "UPDATE folio SET paid=0, paid_at=NULL, paid_by_user=NULL WHERE id=?",
                (fid,),
            )
        await db.commit()
        ok = cur.rowcount > 0
    if ok:
        await folio_event_add(fid, "pay" if paid else "unpay", by_role, by_user, None)
    return ok

async def folio_delete(fid: int, by_role: str, by_user: int) -> bool:
    row = await folio_get(fid)
    if not row:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM folio WHERE id=?", (fid,))
        await db.commit()
        ok = cur.rowcount > 0
    if ok:
        await folio_event_add(fid, "delete", by_role, by_user, None)
    return ok

async def folio_unpaid_rooms(limit: int = 120):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT room, SUM(amount) as total "
            "FROM folio WHERE COALESCE(paid,0)=0 GROUP BY room HAVING total>0 ORDER BY total DESC LIMIT ?",
            (limit,),
        ) as cur:
            return await cur.fetchall()

async def folio_history(fid: int, limit: int = 15):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT action, by_role, by_user, at, note FROM folio_events WHERE folio_id=? ORDER BY at DESC LIMIT ?",
            (fid, limit),
        ) as cur:
            return await cur.fetchall()

# ------- SPA bookings helpers -------
async def spa_booking_add(start_ts: datetime, text: str, created_by_user: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO spa_bookings(start_ts, text, created_by_user, created_at, reminded_1h) VALUES(?,?,?,?,0)",
            (start_ts.isoformat(), text, created_by_user, now_tz().isoformat()),
        )
        bid = cur.lastrowid
        await db.commit()
        return bid

async def spa_booking_delete(bid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM spa_bookings WHERE id=?", (bid,))
        await db.commit()
        return cur.rowcount > 0

async def spa_bookings_between(dt_from: datetime, dt_to: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, start_ts, text FROM spa_bookings WHERE start_ts>=? AND start_ts<? ORDER BY start_ts ASC",
            (dt_from.isoformat(), dt_to.isoformat()),
        ) as cur:
            return await cur.fetchall()

async def spa_bookings_due_1h(now: datetime):
    # окно 1 минуту около (now + 1 час)
    t1 = (now + timedelta(hours=1) - timedelta(seconds=30))
    t2 = (now + timedelta(hours=1) + timedelta(seconds=30))
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, start_ts, text FROM spa_bookings "
            "WHERE reminded_1h=0 AND start_ts>=? AND start_ts<?",
            (t1.isoformat(), t2.isoformat()),
        ) as cur:
            return await cur.fetchall()

async def spa_booking_mark_reminded(bid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE spa_bookings SET reminded_1h=1 WHERE id=?", (bid,))
        await db.commit()

# ---------------- Render Web server ----------------
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

# ---------------- reports / reminders ----------------
async def send_debt_report(bot: Bot, title: str):
    admins = await list_admins(ROLE_RECEPTION)
    if not admins:
        return
    rows = await folio_unpaid_rooms(limit=200)
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

async def spa_morning_bookings(bot: Bot):
    admins = await list_admins(ROLE_SPA)
    if not admins:
        return
    now = now_tz()
    today = now.date()
    dt_from = datetime(today.year, today.month, today.day, 0, 0, tzinfo=TZ)
    dt_to = dt_from + timedelta(days=1)
    rows = await spa_bookings_between(dt_from, dt_to)
    if not rows:
        msg = "🧖 SPA: На сегодня броней нет."
    else:
        lines = ["🧖 SPA: Брони на сегодня:"]
        for bid, start_ts, text in rows:
            dt = datetime.fromisoformat(start_ts).astimezone(TZ)
            lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {text}")
        msg = "\n".join(lines)
    for uid in admins:
        try:
            await bot.send_message(uid, msg)
        except Exception:
            pass

async def spa_1h_reminder_tick(bot: Bot):
    now = now_tz()
    admins = await list_admins(ROLE_SPA)
    if not admins:
        return
    rows = await spa_bookings_due_1h(now)
    if not rows:
        return
    for bid, start_ts, text in rows:
        dt = datetime.fromisoformat(start_ts).astimezone(TZ)
        msg = f"⏰ Напоминание (за 1 час)\n🧖 SPA бронь #{bid}\nВремя: {dt.strftime('%d.%m %H:%M')}\n{text}"
        for uid in admins:
            try:
                await bot.send_message(uid, msg)
            except Exception:
                pass
        await spa_booking_mark_reminded(int(bid))

# ---------------- Main bot ----------------
async def run_bot():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN пустой (Render Environment).")

    await init_db()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(send_debt_report, "cron", hour=9, minute=0, args=[bot, "Отчёт по долгам (09:00)"], id="debt_9", replace_existing=True)
    scheduler.add_job(send_debt_report, "cron", hour=19, minute=0, args=[bot, "Отчёт по долгам (19:00)"], id="debt_19", replace_existing=True)

    scheduler.add_job(spa_morning_bookings, "cron", hour=9, minute=0, args=[bot], id="spa_morning", replace_existing=True)
    scheduler.add_job(spa_1h_reminder_tick, "interval", seconds=30, args=[bot], id="spa_1h_tick", replace_existing=True)
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
        await show_client_or_lang(m, state)

    @dp.message(F.text == "🚪 Выйти")
    async def admin_logout(m: Message, state: FSMContext):
        await state.clear()
        await clear_role(m.from_user.id)
        await show_client_or_lang(m, state)

    # ---- Folio inline callbacks ----
    @dp.callback_query(F.data.startswith("folio:"))
    async def folio_cb(q: CallbackQuery):
        role = await get_role(q.from_user.id)
        if role not in (ROLE_RECEPTION, ROLE_SPA):
            await q.answer("Нет доступа", show_alert=True)
            return
        parts = q.data.split(":")
        action = parts[1]
        fid = int(parts[2])

        if action == "pay":
            ok = await folio_set_paid(fid, 1, role, q.from_user.id)
            await q.answer("✅ Оплачено" if ok else "⚠️ Не найден", show_alert=not ok)
            return
        if action == "unpay":
            ok = await folio_set_paid(fid, 0, role, q.from_user.id)
            await q.answer("❌ Не оплачено" if ok else "⚠️ Не найден", show_alert=not ok)
            return
        if action == "del":
            await q.message.reply(f"Удалить счёт #{fid}?", reply_markup=ik_delete_confirm(fid))
            await q.answer()
            return
        if action == "del_yes":
            ok = await folio_delete(fid, role, q.from_user.id)
            await q.message.edit_text("🗑 Удалено" if ok else "⚠️ Не найден")
            await q.answer()
            return
        if action == "del_no":
            await q.message.edit_text("❌ Удаление отменено.")
            await q.answer()
            return
        if action == "hist":
            hist = await folio_history(fid, limit=15)
            if not hist:
                await q.answer("Истории нет", show_alert=True)
                return
            lines = [f"🕘 История счёта #{fid}:"]
            for act, by_role, by_user, at, note in hist:
                dt = fmt_dt(at)
                who = f"{by_role or '-'} / {by_user or '-'}"
                extra = f" — {note}" if note else ""
                lines.append(f"{dt} | {act} | {who}{extra}")
            await q.message.reply("\n".join(lines))
            await q.answer()
            return

        await q.answer()

    # ---- Routes ----
    async def send_only_debts(m: Message, role: str):
        rows = await folio_unpaid_rooms(limit=200)
        if not rows:
            await m.answer("✅ Неоплаченных долгов нет.", reply_markup=kb_reception() if role == ROLE_RECEPTION else kb_spa())
            return
        lines = ["💳 Только долги (неоплачено):"]
        for room, total in rows:
            lines.append(f"• Комната {room}: {int(total)}")
        lines.append("\nЧтобы посмотреть детали — «📄 Фолио по номеру» → введите комнату.")
        await m.answer("\n".join(lines), reply_markup=kb_reception() if role == ROLE_RECEPTION else kb_spa())

    async def send_folio_room_view(m: Message, room: str, role: str):
        rows = await folio_list_by_room(room)
        total = await folio_total_by_room(room)
        unpaid = await folio_unpaid_total_by_room(room)

        if not rows:
            await m.answer(
                f"📄 Комната {room}: записей нет.\n💳 ДОЛГ: {unpaid}\nИтого: {total}",
                reply_markup=kb_reception() if role == ROLE_RECEPTION else kb_spa()
            )
            return

        await m.answer(
            f"📄 Фолио комнаты {room}\n💳 ДОЛГ: {unpaid}\nИтого: {total}",
            reply_markup=kb_reception() if role == ROLE_RECEPTION else kb_spa()
        )
        for fid, cat, amt, note, by_role, created_at, paid, paid_at, paid_by_user in rows:
            status = "✅ ОПЛ" if int(paid) == 1 else "❌ ДОЛГ"
            base = f"#{fid} | {status} | {cat} | {amt} | {fmt_dt(created_at)}"
            if note:
                base += f" — {note}"
            if int(paid) == 1 and paid_at:
                base += f"\nОплачено: {fmt_dt(paid_at)} (user {paid_by_user})"
            await m.answer(base, reply_markup=ik_folio_actions(int(fid)))

    async def reception_routes(m: Message, state: FSMContext):
        txt = (m.text or "").strip()

        if txt == "💳 Только долги":
            await state.clear()
            await send_only_debts(m, ROLE_RECEPTION)
            return

        if txt in ("➕ Счёт: Ресторан", "➕ Счёт: Прачка", "➕ Счёт: SPA (фолио)"):
            cat = CAT_RESTAURANT if txt.endswith("Ресторан") else CAT_LAUNDRY if txt.endswith("Прачка") else CAT_SPA
            await state.clear()
            await state.set_state(FolioAddFlow.waiting_room)
            await state.update_data(category=cat, created_by_role=ROLE_RECEPTION)
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
            room = data["room"]
            category = data["category"]
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
                f"💳 ДОЛГ: {unpaid}\nИтого: {total}",
                reply_markup=kb_reception()
            )
            return

        if st == FolioViewFlow.waiting_room.state:
            try:
                room = parse_room(m.text)
            except Exception:
                await m.answer("❌ Неверный номер комнаты. Только цифры (например 706):")
                return
            await state.clear()
            await send_folio_room_view(m, room, ROLE_RECEPTION)
            return

        await m.answer("✅ Панель: Ресепшен", reply_markup=kb_reception())

    async def spa_routes(m: Message, state: FSMContext):
        txt = (m.text or "").strip()

        # ---- SPA bookings ----
        if txt == "➕ Добавить бронь":
            await state.clear()
            await state.set_state(SpaAddBookingFlow.waiting_date)
            await m.answer("Введите дату ДД.ММ (например 20.02):")
            return

        if txt == "📅 Брони на сегодня":
            await state.clear()
            now = now_tz()
            today = now.date()
            dt_from = datetime(today.year, today.month, today.day, 0, 0, tzinfo=TZ)
            dt_to = dt_from + timedelta(days=1)
            rows = await spa_bookings_between(dt_from, dt_to)
            if not rows:
                await m.answer("🧖 На сегодня броней нет.", reply_markup=kb_spa())
            else:
                lines = ["🧖 Брони на сегодня:"]
                for bid, start_ts, text in rows:
                    dt = datetime.fromisoformat(start_ts).astimezone(TZ)
                    lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {text}")
                lines.append("\n🗑 Удаление: нажмите «Удалить бронь» и введите ID.")
                await m.answer("\n".join(lines), reply_markup=kb_spa())
            return

        if txt == "📆 Брони на дату":
            await state.clear()
            await state.set_state(SpaBookingsByDateFlow.waiting_date)
            await m.answer("Введите дату ДД.ММ (например 20.02):")
            return

        if txt == "🗑 Удалить бронь":
            await state.clear()
            await state.set_state(SpaDeleteBookingFlow.waiting_id)
            await m.answer("Введите ID брони для удаления (например 12):")
            return

        # ---- common buttons ----
        if txt == "💳 Только долги":
            await state.clear()
            await send_only_debts(m, ROLE_SPA)
            return

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

        # SPA add booking flow
        if st == SpaAddBookingFlow.waiting_date.state:
            try:
                dd, mm = parse_ddmm(m.text)
            except Exception:
                await m.answer("❌ Неверно. Введите дату ДД.ММ (например 20.02):")
                return
            y = now_tz().year
            # если месяц уже прошёл — считаем следующий год
            today = now_tz().date()
            guess = ddate(y, mm, dd)
            if guess < today:
                guess = ddate(y + 1, mm, dd)
            await state.update_data(book_date=guess.isoformat())
            await state.set_state(SpaAddBookingFlow.waiting_time)
            await m.answer("Введите время ЧЧ:ММ (например 14:00):")
            return

        if st == SpaAddBookingFlow.waiting_time.state:
            try:
                hh, mi = parse_hhmm(m.text)
            except Exception:
                await m.answer("❌ Неверно. Введите время ЧЧ:ММ (например 14:00):")
                return
            data = await state.get_data()
            d = ddate.fromisoformat(data["book_date"])
            start_ts = datetime(d.year, d.month, d.day, hh, mi, tzinfo=TZ)
            await state.update_data(start_ts=start_ts.isoformat())
            await state.set_state(SpaAddBookingFlow.waiting_text)
            await m.answer("Введите текст брони (имя/услуга/телефон и т.д.):")
            return

        if st == SpaAddBookingFlow.waiting_text.state:
            data = await state.get_data()
            start_ts = datetime.fromisoformat(data["start_ts"]).astimezone(TZ)
            text = (m.text or "").strip()
            if len(text) < 2:
                await m.answer("❌ Слишком коротко. Введите текст брони:")
                return
            bid = await spa_booking_add(start_ts, text, m.from_user.id)
            await state.clear()
            await m.answer(f"✅ Бронь добавлена: #{bid}\n{start_ts.strftime('%d.%m %H:%M')} — {text}", reply_markup=kb_spa())
            return

        # SPA bookings by date
        if st == SpaBookingsByDateFlow.waiting_date.state:
            try:
                dd, mm = parse_ddmm(m.text)
            except Exception:
                await m.answer("❌ Неверно. Введите дату ДД.ММ (например 20.02):")
                return
            y = now_tz().year
            target = ddate(y, mm, dd)
            # если это уже прошло — всё равно показываем за этот год (по просьбе админа),
            # но можно сделать иначе. Сейчас оставим так.
            dt_from = datetime(target.year, target.month, target.day, 0, 0, tzinfo=TZ)
            dt_to = dt_from + timedelta(days=1)
            rows = await spa_bookings_between(dt_from, dt_to)
            await state.clear()
            if not rows:
                await m.answer(f"🧖 На {target.strftime('%d.%m')} броней нет.", reply_markup=kb_spa())
            else:
                lines = [f"🧖 Брони на {target.strftime('%d.%m')}:"]
                for bid, start_ts, text in rows:
                    dt = datetime.fromisoformat(start_ts).astimezone(TZ)
                    lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {text}")
                lines.append("\n🗑 Удаление: «Удалить бронь» → ID.")
                await m.answer("\n".join(lines), reply_markup=kb_spa())
            return

        # SPA delete booking
        if st == SpaDeleteBookingFlow.waiting_id.state:
            s = (m.text or "").strip()
            if not s.isdigit():
                await m.answer("❌ Нужно число (ID). Например 12:")
                return
            ok = await spa_booking_delete(int(s))
            await state.clear()
            await m.answer("🗑 Бронь удалена." if ok else "⚠️ ID не найден.", reply_markup=kb_spa())
            return

        # SPA folio flows
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
            room = data["room"]
            amount = int(data["amount"])
            note = (m.text or "").strip()
            if note in ("-", ""):
                note = None
            fid = await folio_add(room, CAT_SPA, amount, note, ROLE_SPA, m.from_user.id)
            total = await folio_total_by_room(room)
            unpaid = await folio_unpaid_total_by_room(room)
            await state.clear()
            await m.answer(
                f"✅ Добавлено (#{fid})\nКомната: {room}\nКатегория: spa\nСумма: {amount}\n"
                f"💳 ДОЛГ: {unpaid}\nИтого: {total}",
                reply_markup=kb_spa()
            )
            return

        if st == FolioViewFlow.waiting_room.state:
            try:
                room = parse_room(m.text)
            except Exception:
                await m.answer("❌ Неверный номер комнаты. Только цифры (например 706):")
                return
            await state.clear()
            await send_folio_room_view(m, room, ROLE_SPA)
            return

        await m.answer("✅ Панель: SPA", reply_markup=kb_spa())

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