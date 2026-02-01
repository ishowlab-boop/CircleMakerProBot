import os
import time
import asyncio
import tempfile
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

import imageio_ffmpeg

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------- Config ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")

TARGET_SIZE = 640
MAX_SECONDS = 60

DB_PATH = os.getenv("DB_PATH", "credits.db")

CREDITS_PER_VIDEO = int(os.getenv("CREDITS_PER_VIDEO", "1"))
CREDITS_PER_VOICE = int(os.getenv("CREDITS_PER_VOICE", str(CREDITS_PER_VIDEO)))

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()  # e.g. "@YourChannel"
FREE_CREDITS = int(os.getenv("FREE_CREDITS", "2"))

SUPPORT_BOT_LINK = os.getenv("SUPPORT_BOT_LINK", "").strip()  # optional
ADMIN_CONTACTS = os.getenv("ADMIN_CONTACTS", "").strip()      # e.g. "https://t.me/AriyanFix,@admin2"

ADMIN_IDS = set()
_admin_raw = os.getenv("ADMIN_IDS", "").strip()
if _admin_raw:
    ADMIN_IDS = {int(x.strip()) for x in _admin_raw.split(",") if x.strip().isdigit()}

# Admin selected user (admin_id -> selected_user_id)
ADMIN_SELECTED: dict[int, int] = {}

BOT_PUBLIC_LINK_CACHE: str | None = None

# ---------------- DB ----------------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute(
    "CREATE TABLE IF NOT EXISTS users ("
    "user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, last_seen INTEGER NOT NULL)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS credits ("
    "user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, expires_at INTEGER)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS freebies (user_id INTEGER PRIMARY KEY, claimed INTEGER NOT NULL DEFAULT 0)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS stats ("
    "user_id INTEGER PRIMARY KEY, videos_made INTEGER NOT NULL DEFAULT 0, voices_made INTEGER NOT NULL DEFAULT 0)"
)
db.commit()
db_lock = asyncio.Lock()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def now_ts() -> int:
    return int(time.time())


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


async def upsert_user(update: Update) -> None:
    u = update.effective_user
    if not u:
        return
    async with db_lock:
        db.execute(
            "INSERT OR REPLACE INTO users(user_id, username, first_name, last_name, last_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (u.id, u.username, u.first_name, u.last_name, now_ts()),
        )
        db.execute("INSERT OR IGNORE INTO credits(user_id, balance, expires_at) VALUES (?, 0, NULL)", (u.id,))
        db.execute("INSERT OR IGNORE INTO freebies(user_id, claimed) VALUES (?, 0)", (u.id,))
        db.execute("INSERT OR IGNORE INTO stats(user_id, videos_made, voices_made) VALUES (?, 0, 0)", (u.id,))
        db.commit()


async def cleanup_if_expired(user_id: int) -> None:
    async with db_lock:
        cur = db.execute("SELECT balance, expires_at FROM credits WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            db.execute("INSERT OR IGNORE INTO credits(user_id, balance, expires_at) VALUES (?, 0, NULL)", (user_id,))
            db.commit()
            return
        exp = row[1]
        if exp is not None and now_ts() >= int(exp):
            db.execute("UPDATE credits SET balance=0, expires_at=NULL WHERE user_id=?", (user_id,))
            db.commit()


async def db_get_balance(user_id: int) -> tuple[int, int | None]:
    await cleanup_if_expired(user_id)
    async with db_lock:
        cur = db.execute("SELECT balance, expires_at FROM credits WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row is None:
            db.execute("INSERT OR IGNORE INTO credits(user_id, balance, expires_at) VALUES (?, 0, NULL)", (user_id,))
            db.commit()
            return 0, None
        return int(row[0]), (int(row[1]) if row[1] is not None else None)


async def db_add_credits(user_id: int, amount: int, days_valid: int | None = None) -> tuple[int, int | None]:
    await cleanup_if_expired(user_id)
    new_exp = None
    if days_valid is not None and days_valid > 0:
        new_exp = now_ts() + days_valid * 86400

    async with db_lock:
        db.execute("INSERT OR IGNORE INTO credits(user_id, balance, expires_at) VALUES (?, 0, NULL)", (user_id,))
        db.execute("UPDATE credits SET balance = balance + ? WHERE user_id=?", (amount, user_id))

        if new_exp is not None:
            cur = db.execute("SELECT expires_at FROM credits WHERE user_id=?", (user_id,))
            row = cur.fetchone()
            cur_exp = int(row[0]) if row and row[0] is not None else 0
            final_exp = max(cur_exp, new_exp)
            db.execute("UPDATE credits SET expires_at=? WHERE user_id=?", (final_exp, user_id))

        db.commit()

    return await db_get_balance(user_id)


async def db_deduct_credits_if_possible(user_id: int, amount: int) -> bool:
    await cleanup_if_expired(user_id)
    async with db_lock:
        cur = db.execute("SELECT balance FROM credits WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row is None:
            db.execute("INSERT OR IGNORE INTO credits(user_id, balance, expires_at) VALUES (?, 0, NULL)", (user_id,))
            db.commit()
            return False

        bal = int(row[0])
        if bal < amount:
            return False

        db.execute("UPDATE credits SET balance = balance - ? WHERE user_id=?", (amount, user_id))
        db.commit()
        return True


async def freebies_is_claimed(user_id: int) -> bool:
    async with db_lock:
        cur = db.execute("SELECT claimed FROM freebies WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row is None:
            db.execute("INSERT OR IGNORE INTO freebies(user_id, claimed) VALUES (?, 0)", (user_id,))
            db.commit()
            return False
        return int(row[0]) == 1


async def freebies_mark_claimed(user_id: int) -> None:
    async with db_lock:
        db.execute("INSERT OR IGNORE INTO freebies(user_id, claimed) VALUES (?, 0)", (user_id,))
        db.execute("UPDATE freebies SET claimed=1 WHERE user_id=?", (user_id,))
        db.commit()


async def stats_inc_video(user_id: int) -> None:
    async with db_lock:
        db.execute("INSERT OR IGNORE INTO stats(user_id, videos_made, voices_made) VALUES (?, 0, 0)", (user_id,))
        db.execute("UPDATE stats SET videos_made = videos_made + 1 WHERE user_id=?", (user_id,))
        db.commit()


async def stats_inc_voice(user_id: int) -> None:
    async with db_lock:
        db.execute("INSERT OR IGNORE INTO stats(user_id, videos_made, voices_made) VALUES (?, 0, 0)", (user_id,))
        db.execute("UPDATE stats SET voices_made = voices_made + 1 WHERE user_id=?", (user_id,))
        db.commit()


async def stats_get(user_id: int) -> tuple[int, int]:
    async with db_lock:
        cur = db.execute("SELECT videos_made, voices_made FROM stats WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            db.execute("INSERT OR IGNORE INTO stats(user_id, videos_made, voices_made) VALUES (?, 0, 0)", (user_id,))
            db.commit()
            return 0, 0
        return int(row[0]), int(row[1])


async def is_user_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return False
    try:
        member = await context.bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        return member.status in ("creator", "administrator", "member")
    except TelegramError:
        return False


# ---------------- ffmpeg helpers ----------------
async def run_cmd(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode("utf-8", errors="ignore"))


def ffmpeg_bin() -> str:
    return os.getenv("FFMPEG_PATH") or imageio_ffmpeg.get_ffmpeg_exe()


def build_ffmpeg_video_cmd(inp: str, outp: str) -> list[str]:
    vf = (
        f"scale={TARGET_SIZE}:{TARGET_SIZE}:force_original_aspect_ratio=increase,"
        f"crop={TARGET_SIZE}:{TARGET_SIZE},format=yuv420p"
    )
    return [
        ffmpeg_bin(), "-y",
        "-i", inp,
        "-t", str(MAX_SECONDS),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        outp
    ]


def build_ffmpeg_voice_cmd(inp: str, outp: str) -> list[str]:
    return [
        ffmpeg_bin(), "-y",
        "-i", inp,
        "-vn",
        "-c:a", "libopus",
        "-b:a", "48k",
        "-vbr", "on",
        outp
    ]


# ---------------- URL Buttons ----------------
def channel_url() -> str:
    return f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}" if REQUIRED_CHANNEL else ""


def parse_links(raw: str) -> list[str]:
    items = []
    for x in raw.split(","):
        x = x.strip()
        if not x:
            continue
        if x.startswith("http"):
            items.append(x)
        elif x.startswith("@"):
            items.append(f"https://t.me/{x.lstrip('@')}")
        else:
            items.append(f"https://t.me/{x}")
    return items


async def get_bot_public_link(context: ContextTypes.DEFAULT_TYPE) -> str:
    global BOT_PUBLIC_LINK_CACHE
    if BOT_PUBLIC_LINK_CACHE:
        return BOT_PUBLIC_LINK_CACHE
    me = await context.bot.get_me()
    if me.username:
        BOT_PUBLIC_LINK_CACHE = f"https://t.me/{me.username}"
    else:
        BOT_PUBLIC_LINK_CACHE = "Bot username not set"
    return BOT_PUBLIC_LINK_CACHE


async def start_links_kb(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    rows = []

    # 1) Channel
    if REQUIRED_CHANNEL:
        rows.append([InlineKeyboardButton("📣 Channel", url=channel_url())])

    # 2) Support bot or this bot
    if SUPPORT_BOT_LINK:
        rows.append([InlineKeyboardButton("🆘 Support Bot", url=SUPPORT_BOT_LINK)])
    else:
        rows.append([InlineKeyboardButton("🤖 Bot Link", url=await get_bot_public_link(context))])

    # 3) Admin contacts (can be multiple)
    admins = parse_links(ADMIN_CONTACTS)
    if not admins:
        admins = []
    for i, link in enumerate(admins, start=1):
        rows.append([InlineKeyboardButton(f"🧑‍💻 Admin Contact {i}", url=link)])

    # Menu buttons (callback) for features
    rows.append([InlineKeyboardButton("🎁 Get Free 2 Credits", callback_data="M:FREE")])
    rows.append([InlineKeyboardButton("👤 My Status", callback_data="M:ME")])
    if is_admin_user := False:
        pass

    return InlineKeyboardMarkup(rows)


# ---------------- Commands ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    user_id = update.effective_user.id

    text = (
        "✅ Welcome!\n"
        "ভিডিও পাঠালে আমি Circle Video Note বানিয়ে দেব (max 60s)\n"
        "Voice/Audio পাঠালে আমি Voice Message বানিয়ে দেব\n\n"
        f"🎥 Video cost: {CREDITS_PER_VIDEO} credit\n"
        f"🎧 Voice cost: {CREDITS_PER_VOICE} credit\n"
        "👇 নিচের বাটনে ক্লিক করুন"
    )
    kb = await start_links_kb(context)
    await update.message.reply_text(text, reply_markup=kb)

    # Admin hint
    if is_admin(user_id):
        await update.message.reply_text("Admin commands: /users, /grant <amount> <days>, /grantto <id> <amount> <days>")


async def free_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    await do_free_reply(update, context, update.effective_user.id)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    await send_status(update, context, update.effective_user.id)


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        await update.message.reply_text("Admin না হলে ব্যবহার করা যাবে না।")
        return

    async with db_lock:
        cur = db.execute("SELECT user_id, username, first_name FROM users ORDER BY last_seen DESC LIMIT 12")
        rows = cur.fetchall()

    if not rows:
        await update.message.reply_text("এখনো কোনো user নেই।")
        return

    kb = []
    for uid, username, first_name in rows:
        label = f"{uid}"
        if username:
            label += f"  @{username}"
        elif first_name:
            label += f"  {first_name}"
        kb.append([InlineKeyboardButton(label, callback_data=f"SEL:{uid}")])

    await update.message.reply_text("User select করুন:", reply_markup=InlineKeyboardMarkup(kb))


async def grant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        await update.message.reply_text("Admin না হলে ব্যবহার করা যাবে না।")
        return

    if admin_id not in ADMIN_SELECTED:
        await update.message.reply_text("আগে /users দিয়ে user select করুন।")
        return

    if len(context.args) < 1:
        await update.message.reply_text("Use: /grant <amount> <days(optional)>\nExample: /grant 10 30")
        return

    try:
        amount = int(context.args[0])
        days = int(context.args[1]) if len(context.args) >= 2 else None
    except ValueError:
        await update.message.reply_text("amount/days সংখ্যা হতে হবে।")
        return

    if amount <= 0:
        await update.message.reply_text("amount কমপক্ষে 1 হতে হবে।")
        return

    uid = ADMIN_SELECTED[admin_id]
    bal, exp = await db_add_credits(uid, amount, days_valid=days)
    await update.message.reply_text(f"✅ Granted {uid}\nCredits: {bal}\nValidity: {fmt_ts(exp) if exp else 'No expiry'}")


async def grantto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        await update.message.reply_text("Admin না হলে ব্যবহার করা যাবে না।")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Use: /grantto <user_id> <amount> <days(optional)>\nExample: /grantto 123 10 30")
        return

    try:
        uid = int(context.args[0])
        amount = int(context.args[1])
        days = int(context.args[2]) if len(context.args) >= 3 else None
    except ValueError:
        await update.message.reply_text("user_id/amount/days সংখ্যা হতে হবে।")
        return

    if amount <= 0:
        await update.message.reply_text("amount কমপক্ষে 1 হতে হবে।")
        return

    bal, exp = await db_add_credits(uid, amount, days_valid=days)
    await update.message.reply_text(f"✅ Granted {uid}\nCredits: {bal}\nValidity: {fmt_ts(exp) if exp else 'No expiry'}")


# ---------------- Callback actions ----------------
async def send_status(update_or_query, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    bal, exp = await db_get_balance(user_id)
    vids, voices = await stats_get(user_id)

    msg = f"👤 My Status\nCredits: {bal}\nVideos made: {vids}\nVoices made: {voices}"
    if exp is not None:
        msg += f"\nValidity: {fmt_ts(exp)}"

    await update_or_query.message.reply_text(msg)


async def do_free_reply(update_or_query, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if not REQUIRED_CHANNEL:
        await update_or_query.message.reply_text("Free system সেট করা নেই (REQUIRED_CHANNEL নাই)।")
        return

    if await freebies_is_claimed(user_id):
        await update_or_query.message.reply_text("✅ আপনি আগেই Free credits নিয়েছেন।")
        await send_status(update_or_query, context, user_id)
        return

    subscribed = await is_user_subscribed(context, user_id)
    if not subscribed:
        await update_or_query.message.reply_text(
            f"ফ্রি পেতে আগে Join করুন:\n{REQUIRED_CHANNEL}\nJoin করে আবার /free দিন।\n\n"
            f"Note: Bot কে channel-admin দিতে হবে, না হলে check কাজ নাও করতে পারে।"
        )
        return

    await db_add_credits(user_id, FREE_CREDITS, days_valid=None)
    await freebies_mark_claimed(user_id)
    await update_or_query.message.reply_text(f"🎁 আপনি {FREE_CREDITS} Free credits পেলেন!")
    await send_status(update_or_query, context, user_id)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    user_id = q.from_user.id
    data = q.data or ""

    if data.startswith("SEL:"):
        if not is_admin(user_id):
            await q.edit_message_text("Admin না হলে ব্যবহার করা যাবে না।")
            return
        uid = int(data.split(":", 1)[1])
        ADMIN_SELECTED[user_id] = uid
        bal, exp = await db_get_balance(uid)
        vids, voices = await stats_get(uid)
        await q.edit_message_text(
            f"✅ Selected: {uid}\nCredits: {bal}\nVideos: {vids}\nVoices: {voices}\n"
            f"Validity: {fmt_ts(exp) if exp else 'No expiry'}\n\n"
            "এখন দিন:\n/grant 10 30  অথবা /grant 5"
        )
        return

    if data == "M:FREE":
        await do_free_reply(q, context, user_id)
        return

    if data == "M:ME":
        await send_status(q, context, user_id)
        return


# ---------------- Video handler ----------------
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    msg = update.message
    if not msg:
        return
    user_id = update.effective_user.id

    ok = await db_deduct_credits_if_possible(user_id, CREDITS_PER_VIDEO)
    if not ok:
        bal, exp = await db_get_balance(user_id)
        text = f"ক্রেডিট কম!\nCredits: {bal}\nNeed: {CREDITS_PER_VIDEO}"
        if exp is not None:
            text += f"\nValidity: {fmt_ts(exp)}"
        if REQUIRED_CHANNEL:
            text += f"\nফ্রি পেতে: {REQUIRED_CHANNEL} Join করে /free"
        await msg.reply_text(text)
        return

    file_id = msg.video.file_id if msg.video else None
    if not file_id and msg.document and (msg.document.mime_type or "").startswith("video/"):
        file_id = msg.document.file_id

    if not file_id:
        await db_add_credits(user_id, CREDITS_PER_VIDEO)  # refund
        await msg.reply_text("ভিডিও ফাইল পাঠান (video বা video document)।")
        return

    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.UPLOAD_VIDEO_NOTE)

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        inp = str(td_path / "in.mp4")
        outp = str(td_path / "out.mp4")

        try:
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(custom_path=inp)

            cmd = build_ffmpeg_video_cmd(inp, outp)
            await run_cmd(cmd)

            with open(outp, "rb") as f:
                await msg.reply_video_note(video_note=f, length=TARGET_SIZE)

            await stats_inc_video(user_id)

        except Exception as e:
            await db_add_credits(user_id, CREDITS_PER_VIDEO)  # refund
            await msg.reply_text(f"কনভার্ট সমস্যা: {e}")


# ---------------- Voice handler ----------------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    msg = update.message
    if not msg:
        return
    user_id = update.effective_user.id

    ok = await db_deduct_credits_if_possible(user_id, CREDITS_PER_VOICE)
    if not ok:
        bal, exp = await db_get_balance(user_id)
        text = f"ক্রেডিট কম!\nCredits: {bal}\nNeed: {CREDITS_PER_VOICE}"
        if exp is not None:
            text += f"\nValidity: {fmt_ts(exp)}"
        if REQUIRED_CHANNEL:
            text += f"\nফ্রি পেতে: {REQUIRED_CHANNEL} Join করে /free"
        await msg.reply_text(text)
        return

    file_id = None
    if msg.voice:
        file_id = msg.voice.file_id
    elif msg.audio:
        file_id = msg.audio.file_id
    elif msg.document and (msg.document.mime_type or "").startswith("audio/"):
        file_id = msg.document.file_id

    if not file_id:
        await db_add_credits(user_id, CREDITS_PER_VOICE)  # refund
        await msg.reply_text("Voice/Audio পাঠান।")
        return

    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.UPLOAD_VOICE)

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        inp = str(td_path / "in_audio")
        outp = str(td_path / "out.ogg")

        try:
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(custom_path=inp)

            cmd = build_ffmpeg_voice_cmd(inp, outp)
            await run_cmd(cmd)

            with open(outp, "rb") as f:
                await msg.reply_voice(voice=f)

            await stats_inc_voice(user_id)

        except Exception as e:
            await db_add_credits(user_id, CREDITS_PER_VOICE)  # refund
            await msg.reply_text(f"Voice convert সমস্যা: {e}")


# ---------------- Main ----------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var সেট করুন।")
    if REQUIRED_CHANNEL and not REQUIRED_CHANNEL.startswith("@"):
        raise RuntimeError("REQUIRED_CHANNEL অবশ্যই @ দিয়ে শুরু (যেমন @MyChannel)")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("free", free_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    # Admin commands
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("grant", grant_cmd))
    app.add_handler(CommandHandler("grantto", grantto_cmd))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_callback))

    # Media handlers
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.AUDIO, handle_voice))

    app.run_polling()


if __name__ == "__main__":
    main()
