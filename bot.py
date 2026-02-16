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

# --- NEW: мини-веб сервер для Render (чтобы был открытый порт)
from aiohttp import web

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TZ_NAME = os.getenv("TZ", "Asia/Tashkent")
MORNING_TIME = os.getenv("MORNING_TIME", "09:00")

# Render отдаёт порт через переменную PORT
PORT = int(os.getenv("PORT", "10000"))

TZ = ZoneInfo(TZ_NAME)
DB_PATH = "bookings.db"

def parse_morning_time(s: str) -> dtime:
    hh, mm = map(int, s.split(":"))
    return dtime(hour=hh, minute=mm)

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
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)
        await db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('morning_time', ?)", (MORNING_TIME,))
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
    return msg.from_user and msg.from_user.id == ADMIN_ID

def parse_add_command(text: str):
    m = re.match(r"^/add\s+(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})\s+(.+)$", text.strip())
    if not m:
        return None
    d, mo, hh, mm, info = m.groups()
    now = datetime.now(TZ)
    year = now.year
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

async def list_bookings_for_day(day: datetime):
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=TZ)
    end = start + timedelta(days=1)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, ts, text FROM bookings WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
            (start.isoformat(), end.isoformat())
        ) as cur:
            return await cur.fetchall()

async def delete_booking(bid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM bookings WHERE id=?", (bid,))
        await db.commit()
        return cur.rowcount > 0

async def send_today_summary(bot: Bot):
    today = datetime.now(TZ)
    rows = await list_bookings_for_day(today)
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
            await bot.send_message(ADMIN_ID, f"⏰ Через 1 час: {dt.strftime('%d.%m %H:%M')} — {txt} (#{bid})")
            await db.execute("UPDATE bookings SET reminded=1 WHERE id=?", (bid,))
        await db.commit()

# --- NEW: веб-ручка здоровья
async def handle_root(request):
    return web.Response(text="OK")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle_root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

async def run_bot():
    await init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def start(m: Message):
        if not is_admin(m):
            return
        await m.answer(
            "Я бот-бронирование.\n\n"
            "Команды:\n"
            "/add ДД.ММ ЧЧ:ММ текст — добавить бронь\n"
            "/today — брони на сегодня\n"
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
        rows = await list_bookings_for_day(today)
        if not rows:
            await m.answer("Сегодня броней нет ✅")
            return
        lines = ["📅 Брони на сегодня:"]
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
        if len(parts) != 2 or not re.match(r"^\d{1,2}:\d{2}$", parts[1]):
            await m.answer("Формат: /time 09:00")
            return
        await set_setting("morning_time", parts[1])
        await m.answer(f"✅ Утренний отчёт: {parts[1]} (вступит после перезапуска)")

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(send_one_hour_reminders, "interval", minutes=1, args=[bot])
    mt = parse_morning_time(await get_setting("morning_time") or MORNING_TIME)
    scheduler.add_job(send_today_summary, "cron", hour=mt.hour, minute=mt.minute, args=[bot])
    scheduler.start()

    await dp.start_polling(bot)

async def main():
    # запускаем веб и бота параллельно
    await asyncio.gather(
        run_web_server(),
        run_bot()
    )

if __name__ == "__main__":
    asyncio.run(main())
