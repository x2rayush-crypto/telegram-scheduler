import os
import sqlite3
import asyncio
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import telebot
from telebot import types
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

TZ = ZoneInfo("Asia/Kolkata")
DB = "scheduler.db"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

SESSION_STRING = os.getenv("SESSION_STRING")

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH
)

# --------------------------------------------------
# DATABASE
# --------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            message TEXT NOT NULL,
            send_at TEXT NOT NULL,
            status TEXT DEFAULT 'pending'
        )
    """)

    conn.commit()
    conn.close()


def add_job(user_id, message, send_at):
    conn = sqlite3.connect(DB)

    cur = conn.cursor()

    cur.execute("""
        INSERT INTO jobs
        (user_id, message, send_at)
        VALUES (?, ?, ?)
    """, (
        str(user_id),
        message,
        send_at
    ))

    job_id = cur.lastrowid

    conn.commit()
    conn.close()

    return job_id


def get_pending_jobs():
    conn = sqlite3.connect(DB)

    rows = conn.execute("""
        SELECT id, user_id, message, send_at
        FROM jobs
        WHERE status='pending'
        ORDER BY send_at
    """).fetchall()

    conn.close()

    return rows


def mark_sent(job_id):
    conn = sqlite3.connect(DB)

    conn.execute(
        "UPDATE jobs SET status='sent' WHERE id=?",
        (job_id,)
    )

    conn.commit()
    conn.close()


def mark_failed(job_id):
    conn = sqlite3.connect(DB)

    conn.execute(
        "UPDATE jobs SET status='failed' WHERE id=?",
        (job_id,)
    )

    conn.commit()
    conn.close()


def cancel_job(job_id):
    conn = sqlite3.connect(DB)

    cur = conn.cursor()

    cur.execute("""
        UPDATE jobs
        SET status='cancelled'
        WHERE id=? AND status='pending'
    """, (job_id,))

    changed = cur.rowcount

    conn.commit()
    conn.close()

    return changed


# --------------------------------------------------
# ADMIN CHECK
# --------------------------------------------------

def is_admin(message):
    return message.from_user.id == ADMIN_ID


# --------------------------------------------------
# TELEGRAM ACCOUNT
# --------------------------------------------------

async def resolve_recipient(user_id):
    user_id = str(user_id).strip()

    # Username
    if user_id.startswith("@"):
        return await client.get_entity(user_id)

    # Numeric ID
    target_id = int(user_id)

    async for dialog in client.iter_dialogs():

        if dialog.id == target_id:
            return dialog.entity

    raise ValueError(
        "Recipient not found in your Telegram dialogs. "
        "Open the chat and manually message the person first."
    )


# --------------------------------------------------
# SENDING
# --------------------------------------------------

async def send_job(job_id, user_id, message, send_at):

    try:

        target = datetime.fromisoformat(send_at)

        # Precise local waiting
        while True:

            now = datetime.now(TZ)

            remaining = (
                target - now
            ).total_seconds()

            if remaining <= 0:
                break

            if remaining > 1:
                await asyncio.sleep(
                    remaining - 0.2
                )
            else:
                await asyncio.sleep(0.01)

        print(
            f"[SENDING] Job {job_id} "
            f"at {datetime.now(TZ).strftime('%H:%M:%S.%f')}"
        )

        entity = await resolve_recipient(user_id)

        await client.send_message(
            entity,
            message
        )

        mark_sent(job_id)

        print(
            f"[SENT] Job {job_id}"
        )

    except Exception as e:

        mark_failed(job_id)

        print(
            f"[ERROR] Job {job_id}: {e}"
        )


async def scheduler():

    running = {}

    while True:

        jobs = get_pending_jobs()

        for job_id, user_id, message, send_at in jobs:

            if job_id not in running:

                running[job_id] = asyncio.create_task(
                    send_job(
                        job_id,
                        user_id,
                        message,
                        send_at
                    )
                )

        finished = []

        for job_id, task in running.items():

            if task.done():
                finished.append(job_id)

        for job_id in finished:

            del running[job_id]

        await asyncio.sleep(0.1)


async def telegram_worker():

    print("Starting Telegram account...")

    await client.start()

    me = await client.get_me()

    print(
        f"Logged in as: "
        f"{me.first_name or ''} "
        f"{me.last_name or ''}"
    )

    print("Personal Telegram scheduler is running.")

    await scheduler()


def start_telegram_worker():

    asyncio.run(
        telegram_worker()
    )


# --------------------------------------------------
# BOT UI
# --------------------------------------------------

def main_keyboard():

    keyboard = types.InlineKeyboardMarkup()

    keyboard.row(
        types.InlineKeyboardButton(
            "➕ Schedule Message",
            callback_data="schedule"
        )
    )

    keyboard.row(
        types.InlineKeyboardButton(
            "📋 Pending Messages",
            callback_data="pending"
        ),
        types.InlineKeyboardButton(
            "❌ Cancel Message",
            callback_data="cancel"
        )
    )

    return keyboard


# --------------------------------------------------
# START
# --------------------------------------------------

@bot.message_handler(commands=["start"])
def start(message):

    if not is_admin(message):
        bot.reply_to(
            message,
            "⛔ Access denied."
        )
        return

    bot.send_message(
        message.chat.id,
        "<b>🤖 Telegram DM Scheduler</b>\n\n"
        "Messages will be sent from your personal "
        "Telegram account.",
        reply_markup=main_keyboard()
    )


# --------------------------------------------------
# CALLBACKS
# --------------------------------------------------

@bot.callback_query_handler(
    func=lambda call: call.data == "schedule"
)
def schedule_button(call):

    if call.from_user.id != ADMIN_ID:
        return

    msg = bot.send_message(
        call.message.chat.id,
        "👤 Send the recipient Telegram ID\n"
        "or @username:"
    )

    bot.register_next_step_handler(
        msg,
        get_recipient
    )


def get_recipient(message):

    if not is_admin(message):
        return

    user_id = message.text.strip()

    msg = bot.send_message(
        message.chat.id,
        "💬 Send the message you want to schedule:"
    )

    bot.register_next_step_handler(
        msg,
        lambda m: get_message(
            m,
            user_id
        )
    )


def get_message(message, user_id):

    if not is_admin(message):
        return

    text = message.text

    msg = bot.send_message(
        message.chat.id,
        "🕐 Enter date & time in IST:\n\n"
        "<code>YYYY-MM-DD HH:MM:SS</code>\n\n"
        "Example:\n"
        "<code>2026-10-08 11:30:00</code>"
    )

    bot.register_next_step_handler(
        msg,
        lambda m: get_time(
            m,
            user_id,
            text
        )
    )


def get_time(message, user_id, text):

    if not is_admin(message):
        return

    try:

        target = datetime.strptime(
            message.text.strip(),
            "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=TZ)

        if target <= datetime.now(TZ):

            bot.reply_to(
                message,
                "❌ That time has already passed."
            )
            return

        send_at = target.isoformat()

        job_id = add_job(
            user_id,
            text,
            send_at
        )

        bot.send_message(
            message.chat.id,
            f"✅ <b>Scheduled successfully</b>\n\n"
            f"🆔 Job ID: <code>{job_id}</code>\n"
            f"👤 Recipient: <code>{user_id}</code>\n"
            f"🕐 Time: <code>{target.strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
            f"🇮🇳 Timezone: IST"
        )

    except ValueError:

        bot.send_message(
            message.chat.id,
            "❌ Invalid format.\n\n"
            "Use:\n"
            "<code>YYYY-MM-DD HH:MM:SS</code>"
        )


# --------------------------------------------------
# PENDING
# --------------------------------------------------

@bot.callback_query_handler(
    func=lambda call: call.data == "pending"
)
def pending_button(call):

    if call.from_user.id != ADMIN_ID:
        return

    jobs = get_pending_jobs()

    if not jobs:

        bot.send_message(
            call.message.chat.id,
            "📭 No pending messages."
        )

        return

    text = "<b>📋 Pending Messages</b>\n\n"

    for job_id, user_id, message, send_at in jobs:

        dt = datetime.fromisoformat(send_at)

        text += (
            f"🆔 <code>{job_id}</code>\n"
            f"👤 <code>{user_id}</code>\n"
            f"🕐 {dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"💬 {message[:60]}\n\n"
        )

    bot.send_message(
        call.message.chat.id,
        text
    )


# --------------------------------------------------
# CANCEL
# --------------------------------------------------

@bot.callback_query_handler(
    func=lambda call: call.data == "cancel"
)
def cancel_button(call):

    if call.from_user.id != ADMIN_ID:
        return

    msg = bot.send_message(
        call.message.chat.id,
        "❌ Send the Job ID to cancel:"
    )

    bot.register_next_step_handler(
        msg,
        cancel_message
    )


def cancel_message(message):

    if not is_admin(message):
        return

    try:

        job_id = int(message.text.strip())

        if cancel_job(job_id):

            bot.send_message(
                message.chat.id,
                f"✅ Job <code>{job_id}</code> cancelled."
            )

        else:

            bot.send_message(
                message.chat.id,
                "❌ Job not found or already processed."
            )

    except ValueError:

        bot.send_message(
            message.chat.id,
            "❌ Invalid Job ID."
        )


# --------------------------------------------------
# RUN
# --------------------------------------------------

if __name__ == "__main__":

    init_db()

    worker = threading.Thread(
        target=start_telegram_worker,
        daemon=True
    )

    worker.start()

    print("Control bot is starting...")

    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30
    )
