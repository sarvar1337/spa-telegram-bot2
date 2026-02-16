import os
import re
import asyncio
from datetime import datetime, timedelta, time as dtime

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# мини-веб сервер для Render (чтобы был открытый порт)
from aiohttp import web

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TZ_NAME = os.getenv("TZ", "Asia/Tashkent").strip()
MORNING_TIME_DEFAULT = os.getenv("MORNING_TIME", "09:00").strip()

# Render задаёт порт через переменную PORT
PORT = int(os.getenv("PORT", "10000"))

TZ = ZoneInfo(TZ_NAME)
DB_PATH = "bookings.db"


def parse_hhmm(s: str) -> dtime:
    if not re.match(r"^\d{1,2}:\d{2}$", s):
        raise ValueError("Bad HH:MM format")
    hh, mm = map(int, s.split(":"))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("Bad HH:MM value")
    return dtime(hour=hh, minute=mm)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,               -- ISO datetime with TZ
            text TEXT NOT NULL,
            reminded INTEGER DEFAULT 0,     -- 0/1 for 1-hour reminder sent
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


def is_admin(msg: Message) -> bool:
    return bool(msg.from_user and msg.from_user.id == ADMIN_ID)


def parse_add_command(text: str):
    # /add DD.MM HH:MM any text...
    m = re.match(r"^/add\s+(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})\s+(.+)$", text.strip())
    if not m:
        return None
    d, mo, hh, mm, info = m.groups()

    now = datetime.now(TZ)
    year = now.year

    # если дата прошла — считаем следующий год
    dt = datetime(year, int(mo), int(d), int(hh), int(mm), tzinfo=TZ)
    if dt < now - timedelta(days=1):
        dt = dt.replace(year=year + 1)

    return dt, info


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


def day_range(day: datetime):
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=TZ)
    end = start + timedelta(days=1)
    return start, end


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

    # окно около 1 часа вперёд (чтобы не промахнуться)
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


# -------- Web server for Render --------

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


# -------- Bot --------

async def run_bot():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set BOT_TOKEN env var.")
    if ADMIN_ID == 0:
        raise RuntimeError("ADMIN_ID is 0. Set ADMIN_ID env var.")

    await init_db()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    scheduler = AsyncIOScheduler(timezone=TZ)

    def schedule_morning_job(morning_hhmm: str):
        """(Re)create morning summary job with given HH:MM."""
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
    # validate; if broken, fallback
    try:
        parse_hhmm(morning_time)
    except Exception:
        morning_time = MORNING_TIME_DEFAULT
        await set_setting("morning_time", morning_time)

    schedule_morning_job(morning_time)
    scheduler.start()

    @dp.message(Command("start"))
    async def start(m: Message):
        if not is_admin(m):
            return
        await m.answer(
            "✅ Бот бронирования запущен.\n\n"
            "Команды:\n"
            "/add ДД.ММ ЧЧ:ММ текст — добавить бронь\n"
            "/today — брони на сегодня\n"
            "/list ДД.ММ — брони на дату\n"
            "/del ID — удалить бронь\n"
            "/time HH:MM — время утреннего отчёта\n"
        )

    @dp.message(Command("add"))
    async def cmd_add(m: Message):
        if not is_admin(m):
            return
        parsed = parse_add_command(m.text or "")
        if not parsed:
            await m.answer("Формат: /add 16.02 14:00 Текст брони")
            return
        dt, info = parsed
        bid = await add_booking(dt, info)
        await m.answer(f"✅ Добавлено: #{bid} — {dt.strftime('%d.%m %H:%M')} — {info}")

    @dp.message(Command("today"))
    async def cmd_today(m: Message):
        if not is_admin(m):
            return
        today = datetime.now(TZ)
        start, end = day_range(today)
        rows = await list_bookings_between(start, end)

        if not rows:
            await m.answer("Сегодня броней нет ✅")
            return

        lines = ["📅 Брони на сегодня:"]
        for bid, ts, txt in rows:
            dt = datetime.fromisoformat(ts)
            lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {txt}")
        await m.answer("\n".join(lines))

    @dp.message(Command("list"))
    async def cmd_list(m: Message):
        if not is_admin(m):
            return

        parts = (m.text or "").split(maxsplit=1)
        if len(parts) != 2 or not re.match(r"^\d{1,2}\.\d{1,2}$", parts[1].strip()):
            await m.answer("Формат: /list 20.02")
            return

        day_s, month_s = parts[1].strip().split(".")
        day, month = int(day_s), int(month_s)

        now = datetime.now(TZ)
        year = now.year

        try:
            target = datetime(year, month, day, tzinfo=TZ)
        except ValueError:
            await m.answer("Неверная дата ❌")
            return

        start, end = day_range(target)
        rows = await list_bookings_between(start, end)

        if not rows:
            await m.answer(f"На {day:02d}.{month:02d} броней нет ✅")
            return

        lines = [f"📅 Брони на {day:02d}.{month:02d}:"]
        for bid, ts, txt in rows:
            dt = datetime.fromisoformat(ts)
            lines.append(f"#{bid} — {dt.strftime('%H:%M')} — {txt}")
        await m.answer("\n".join(lines))

    @dp.message(Command("del"))
    async def cmd_del(m: Message):
        if not is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit():
            await m.answer("Формат: /del 12")
            return
        ok = await delete_booking(int(parts[1]))
        await m.answer("🗑 Удалено" if ok else "Не найдено")

    @dp.message(Command("time"))
    async def cmd_time(m: Message):
        if not is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) != 2:
            await m.answer("Формат: /time 09:00")
            return
        try:
            mt = parts[1].strip()
            parse_hhmm(mt)
        except Exception:
            await m.answer("Неверный формат времени. Пример: /time 09:00")
            return

        await set_setting("morning_time", mt)
        # применяем сразу без перезапуска
        schedule_morning_job(mt)
        await m.answer(f"✅ Утренний отчёт теперь в {mt}")

    await dp.start_polling(bot)


async def main():
    await asyncio.gather(
        run_web_server(),
        run_bot()
    )


if __name__ == "__main__":
    asyncio.run(main())
