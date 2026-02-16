import os
import re
import asyncio
from datetime import datetime, timedelta, time as dtime

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
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
    # If date already passed - use next year
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
        # запрет одинакового времени (одинакового ts)
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_ts_unique ON bookings(ts)")
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
    # returns (ok, id_or_error)
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO bookings(ts, text, reminded, created_at) VALUES(?,?,0,?)",
                (dt.isoformat(), info, datetime.now(TZ).isoformat())
            )
            await db.commit()
            return True, cur.lastrowid
    except aiosqlite.IntegrityError:
        return False, "busy"

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


# ---------- Notifications ----------
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


# ---------- UI / FSM ----------
class AddFlow(StatesGroup):
    waiting_date = State()
    waiting_time = State()
    waiting_text = State()

def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить бронь"), KeyboardButton(text="📅 Сегодня")],
            [KeyboardButton(text="📆 На дату"), KeyboardButton(text="🗑 Удалить")],
            [KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True
    )

def cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
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

    scheduler = AsyncIOScheduler(timezone=TZ)

    def schedule_morning_job(morning_hhmm: str):
        # remove old job if exists
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

    # periodic reminders
    scheduler.add_job(send_one_hour_reminders, "interval", minutes=1, args=[bot])

    # morning summary time from DB (or default)
    morning_time = (await get_setting("morning_time")) or MORNING_TIME_DEFAULT
    try:
        parse_hhmm(morning_time)
    except Exception:
        morning_time = MORNING_TIME_DEFAULT
        await set_setting("morning_time", morning_time)

    schedule_morning_job(morning_time)
    scheduler.start()

    # ---- Commands ----
    @dp.message(Command("start"))
    async def cmd_start(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        await state.clear()
        await m.answer(
            "✅ Бот бронирования.\n\n"
            "Работа через кнопки 👇\n\n"
            "Команды (если нужно):\n"
            "/add ДД.ММ ЧЧ:ММ текст\n"
            "/today\n"
            "/list ДД.ММ\n"
            "/del ID\n"
            "/time HH:MM\n\n"
            "🚫 Запрет пересечения: нельзя ставить 2 брони на одно и то же время.",
            reply_markup=main_kb()
        )

    @dp.message(Command("today"))
    async def cmd_today(m: Message):
        if not is_admin(m):
            return
        today = datetime.now(TZ)
        start, end = day_range(today)
        rows = await list_bookings_between(start, end)
        if not rows:
            await m.answer("Сегодня броней нет ✅", reply_markup=main_kb())
            return
        lines = ["📅 Брони на сегодня:"]
        for bid, ts, txt in rows:
            dt = datetime.fromisoformat(ts)
            lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {txt}")
        await m.answer("\n".join(lines), reply_markup=main_kb())

    @dp.message(Command("list"))
    async def cmd_list(m: Message):
        if not is_admin(m):
            return
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) != 2:
            await m.answer("Формат: /list 20.02", reply_markup=main_kb())
            return

        ddmm = parts[1].strip()
        try:
            d, mo = parse_ddmm(ddmm)
            now = datetime.now(TZ)
            target = datetime(now.year, mo, d, 0, 0, tzinfo=TZ)
        except Exception:
            await m.answer("Неверная дата. Пример: /list 20.02", reply_markup=main_kb())
            return

        start, end = day_range(target)
        rows = await list_bookings_between(start, end)

        if not rows:
            await m.answer(f"На {ddmm} броней нет ✅", reply_markup=main_kb())
            return

        lines = [f"📅 Брони на {ddmm}:"]
        for bid, ts, txt in rows:
            dt = datetime.fromisoformat(ts)
            lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {txt}")
        await m.answer("\n".join(lines), reply_markup=main_kb())

    @dp.message(Command("del"))
    async def cmd_del(m: Message):
        if not is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit():
            await m.answer("Формат: /del 12", reply_markup=main_kb())
            return
        ok = await delete_booking(int(parts[1]))
        await m.answer("🗑 Удалено" if ok else "Не найдено", reply_markup=main_kb())

    @dp.message(Command("time"))
    async def cmd_time(m: Message):
        if not is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) != 2:
            await m.answer("Формат: /time 09:00", reply_markup=main_kb())
            return
        try:
            mt = parts[1].strip()
            parse_hhmm(mt)
        except Exception:
            await m.answer("Неверное время. Пример: /time 09:00", reply_markup=main_kb())
            return
        await set_setting("morning_time", mt)
        schedule_morning_job(mt)  # apply immediately
        await m.answer(f"✅ Утренний отчёт теперь в {mt}", reply_markup=main_kb())

    @dp.message(Command("add"))
    async def cmd_add(m: Message):
        if not is_admin(m):
            return
        mm = re.match(r"^/add\s+(\d{1,2}\.\d{1,2})\s+(\d{1,2}:\d{2})\s+(.+)$", (m.text or "").strip())
        if not mm:
            await m.answer("Формат: /add 20.02 14:00 Текст", reply_markup=main_kb())
            return
        ddmm, hhmm, text = mm.group(1), mm.group(2), mm.group(3)
        try:
            dt = make_dt(ddmm, hhmm)
        except Exception:
            await m.answer("Неверная дата/время. Пример: /add 20.02 14:00 Текст", reply_markup=main_kb())
            return

        ok, res = await add_booking(dt, text)
        if not ok and res == "busy":
            await m.answer(f"⚠️ На {dt.strftime('%d.%m')} в {dt.strftime('%H:%M')} уже есть бронь.", reply_markup=main_kb())
            return
        await m.answer(f"✅ Добавлено: #{res} — {dt.strftime('%d.%m %H:%M')} — {text}", reply_markup=main_kb())

    # ---- Buttons ----
    @dp.message(F.text == "ℹ️ Помощь")
    async def help_btn(m: Message):
        if not is_admin(m):
            return
        await m.answer(
            "Кнопки:\n"
            "➕ Добавить бронь — пошаговое добавление\n"
            "📅 Сегодня — список броней\n"
            "📆 На дату — список на дату\n"
            "🗑 Удалить — удалить по ID\n\n"
            "Команды:\n"
            "/add ДД.ММ ЧЧ:ММ текст\n"
            "/today\n"
            "/list ДД.ММ\n"
            "/del ID\n"
            "/time HH:MM\n\n"
            "🚫 Запрет пересечения: нельзя ставить 2 брони на одно и то же время.",
            reply_markup=main_kb()
        )

    @dp.message(F.text == "📅 Сегодня")
    async def today_btn(m: Message):
        await cmd_today(m)

    @dp.message(F.text == "➕ Добавить бронь")
    async def add_btn(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        await state.clear()
        await state.set_state(AddFlow.waiting_date)
        await m.answer("Введите дату ДД.ММ (например 20.02) или нажмите ❌ Отмена", reply_markup=cancel_kb())

    @dp.message(F.text == "📆 На дату")
    async def list_date_btn(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        await state.clear()
        await state.update_data(mode="list_only")
        await state.set_state(AddFlow.waiting_date)
        await m.answer("Введите дату ДД.ММ (например 20.02) или нажмите ❌ Отмена", reply_markup=cancel_kb())

    @dp.message(F.text == "🗑 Удалить")
    async def del_btn(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        await state.clear()
        await state.update_data(mode="delete")
        await m.answer("Введите ID брони для удаления (пример: 12) или ❌ Отмена", reply_markup=cancel_kb())

    @dp.message(F.text == "❌ Отмена")
    async def cancel_any(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        await state.clear()
        await m.answer("Ок, отменено ✅", reply_markup=main_kb())

    # ---- FSM handlers ----
    @dp.message(AddFlow.waiting_date)
    async def fsm_date(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        txt = (m.text or "").strip()
        data = await state.get_data()
        mode = data.get("mode")

        # If delete mode - should not be here
        if mode == "delete":
            await state.clear()
            await m.answer("Ошибка режима. Нажмите 🗑 Удалить ещё раз.", reply_markup=main_kb())
            return

        try:
            parse_ddmm(txt)
        except Exception:
            await m.answer("Неверная дата. Пример: 20.02")
            return

        # FIXED: list-only mode WITHOUT creating fake Message
        if mode == "list_only":
            await state.clear()
            d, mo = parse_ddmm(txt)
            now = datetime.now(TZ)
            target = datetime(now.year, mo, d, 0, 0, tzinfo=TZ)
            start, end = day_range(target)
            rows = await list_bookings_between(start, end)

            if not rows:
                await m.answer(f"На {txt} броней нет ✅", reply_markup=main_kb())
                return

            lines = [f"📅 Брони на {txt}:"]
            for bid, ts, t2 in rows:
                dt = datetime.fromisoformat(ts)
                lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {t2}")
            await m.answer("\n".join(lines), reply_markup=main_kb())
            return

        # add flow
        await state.update_data(ddmm=txt)
        await state.set_state(AddFlow.waiting_time)
        await m.answer("Введите время ЧЧ:ММ (например 14:00) или ❌ Отмена", reply_markup=cancel_kb())

    @dp.message(AddFlow.waiting_time)
    async def fsm_time(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        txt = (m.text or "").strip()
        try:
            parse_hhmm(txt)
        except Exception:
            await m.answer("Неверное время. Пример: 14:00")
            return
        await state.update_data(hhmm=txt)
        await state.set_state(AddFlow.waiting_text)
        await m.answer("Введите текст брони (услуга/имя/телефон и т.д.) или ❌ Отмена", reply_markup=cancel_kb())

    @dp.message(AddFlow.waiting_text)
    async def fsm_text(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        text = (m.text or "").strip()
        if not text:
            await m.answer("Текст не должен быть пустым. Напишите описание брони.")
            return

        data = await state.get_data()
        ddmm = data.get("ddmm")
        hhmm = data.get("hhmm")

        try:
            dt = make_dt(ddmm, hhmm)
        except Exception:
            await state.clear()
            await m.answer("Ошибка даты/времени. Начните заново: ➕ Добавить бронь", reply_markup=main_kb())
            return

        ok, res = await add_booking(dt, text)
        await state.clear()

        if not ok and res == "busy":
            await m.answer(
                f"⚠️ На {dt.strftime('%d.%m')} в {dt.strftime('%H:%M')} уже есть бронь.\nВыберите другое время.",
                reply_markup=main_kb()
            )
            return

        await m.answer(f"✅ Добавлено: #{res} — {dt.strftime('%d.%m %H:%M')} — {text}", reply_markup=main_kb())

    # delete mode handler (catch-all)
    @dp.message()
    async def fallback(m: Message, state: FSMContext):
        if not is_admin(m):
            return
        data = await state.get_data()
        if data.get("mode") == "delete":
            txt = (m.text or "").strip()
            if not txt.isdigit():
                await m.answer("Введите только ID цифрами (пример: 12) или ❌ Отмена", reply_markup=cancel_kb())
                return
            ok = await delete_booking(int(txt))
            await state.clear()
            await m.answer("🗑 Удалено" if ok else "Не найдено", reply_markup=main_kb())
            return

        await m.answer("Нажмите кнопку или /start для меню.", reply_markup=main_kb())

    await dp.start_polling(bot)


async def main():
    await asyncio.gather(
        run_web_server(),
        run_bot()
    )

if __name__ == "__main__":
    asyncio.run(main())
