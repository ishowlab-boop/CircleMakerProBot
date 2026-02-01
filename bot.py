import os
import time
import asyncio
import tempfile
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

import imageio_ffmpeg

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
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

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")

TARGET_SIZE = 640
MAX_SECONDS = 60

DB_PATH = os.getenv("DB_PATH", "credits.db")

# ✅ Only video costs credits
CREDITS_PER_VIDEO = int(os.getenv("CREDITS_PER_VIDEO", "1"))
FREE_CREDITS = int(os.getenv("FREE_CREDITS", "2"))

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()      # e.g. "@YourMainChannel"
VOICE_SUPPORT_LINK = os.getenv("VOICE_SUPPORT_LINK", "").strip()  # e.g. "@VoiceChannel" or https://t.me/...
ADMIN_CONTACTS = os.getenv("ADMIN_CONTACTS", "").strip()          # e.g. "https://t.me/AriyanFix,@admin2"

ADMIN_IDS = set()
_admin_raw = os.getenv("ADMIN_IDS", "").strip()
if _admin_raw:
    ADMIN_IDS = {int(x.strip()) for x in _admin_raw.split(",") if x.strip().isdigit()}

# Admin selected user (admin_id -> selected_user_id)
ADMIN_SELECTED: dict[int, int] = {}

# Reply keyboard labels (2x2 like screenshot)
BTN_MODEL = "🧠 MODEL SUPPORT"
BTN_VOICE = "🎙 VOICE SUPPORT"
BTN_ADMIN = "🧑‍💼 ADMIN CONTACT"
BTN_CHANNEL = "📣 CHANNEL"

# =========================
# DB
# =========================
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db_lock = asyncio.Lock()

db.execute(
    "CREATE TABLE IF NOT EXISTS users ("
    "user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, last_seen INTEGER NOT NULL)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS credits ("
    "user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, "
    "valid_from INTEGER, expires_at INTEGER)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS freebies (user_id INTEGER PRIMARY KEY, claimed INTEGER NOT NULL DEFAULT 0)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS stats ("
    "user_id INTEGER PRIMARY KEY, videos_made INTEGER NOT NULL DEFAULT 0, "
    "voices_made INTEGER NOT NULL DEFAULT 0)"
)
db.commit()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def now_ts() -> int:
    return int(time.time())


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%A, %d %b %Y")


def reply_menu() -> ReplyKeyboardMarkup:
    keyboard = [
        [BTN_MODEL, BTN_VOICE],
        [BTN_ADMIN, BTN_CHANNEL],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def to_tme_url(x: str) -> str:
    x = (x or "").strip()
    if not x:
        return ""
    if x.startswith("http"):
        return x
    if x.startswith("@"):
        return f"https://t.me/{x.lstrip('@')}"
    return f"https://t.me/{x}"


def parse_links(raw: str) -> list[str]:
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        u = to_tme_url(part)
        if u:
            out.append(u)
    return out


def channel_url() -> str:
    return to_tme_url(REQUIRED_CHANNEL) if REQUIRED_CHANNEL else ""


def voice_support_url() -> str:
    return to_tme_url(VOICE_SUPPORT_LINK) if VOICE_SUPPORT_LINK else ""


def admin_inline_kb() -> InlineKeyboardMarkup | None:
    links = parse_links(ADMIN_CONTACTS)
    if not links:
        return None
    rows = [[InlineKeyboardButton(f"👤 Admin {i}", url=link)] for i, link in enumerate(links, start=1)]
    return InlineKeyboardMarkup(rows)


def channel_inline_kb() -> InlineKeyboardMarkup | None:
    url = channel_url()
    if not url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("📣 Open Channel", url=url)]])


def voice_inline_kb() -> InlineKeyboardMarkup | None:
    url = voice_support_url()
    if not url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎙 Voice Support Channel", url=url)]])


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
        db.execute(
            "INSERT OR IGNORE INTO credits(user_id, balance, valid_from, expires_at) VALUES (?, 0, NULL, NULL)",
            (u.id,),
        )
        db.execute("INSERT OR IGNORE INTO freebies(user_id, claimed) VALUES (?, 0)", (u.id,))
        db.execute("INSERT OR IGNORE INTO stats(user_id, videos_made, voices_made) VALUES (?, 0, 0)", (u.id,))
        db.commit()


async def cleanup_if_expired(user_id: int) -> None:
    async with db_lock:
        cur = db.execute("SELECT expires_at FROM credits WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return
        exp = row[0]
        if exp is not None and now_ts() >= int(exp):
            db.execute("UPDATE credits SET balance=0, valid_from=NULL, expires_at=NULL WHERE user_id=?", (user_id,))
            db.commit()


async def db_get_credit(user_id: int) -> tuple[int, int | None, int | None]:
    await cleanup_if_expired(user_id)
    async with db_lock:
        cur = db.execute("SELECT balance, valid_from, expires_at FROM credits WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return 0, None, None
        return int(row[0]), (int(row[1]) if row[1] is not None else None), (int(row[2]) if row[2] is not None else None)


async def db_add_credits(user_id: int, amount: int, days_valid: int | None = None) -> tuple[int, int | None, int | None]:
    await cleanup_if_expired(user_id)
    new_exp = None
    if days_valid is not None and days_valid > 0:
        new_exp = now_ts() + days_valid * 86400

    async with db_lock:
        db.execute(
            "INSERT OR IGNORE INTO credits(user_id, balance, valid_from, expires_at) VALUES (?, 0, NULL, NULL)",
            (user_id,),
        )
        db.execute("UPDATE credits SET balance = balance + ? WHERE user_id=?", (amount, user_id))

        if new_exp is not None:
            db.execute("UPDATE credits SET valid_from = COALESCE(valid_from, ?) WHERE user_id=?", (now_ts(), user_id))
            cur = db.execute("SELECT expires_at FROM credits WHERE user_id=?", (user_id,))
            row = cur.fetchone()
            cur_exp = int(row[0]) if row and row[0] is not None else 0
            db.execute("UPDATE credits SET expires_at=? WHERE user_id=?", (max(cur_exp, new_exp), user_id))

        db.commit()

    return await db_get_credit(user_id)


async def db_deduct_video_credit(user_id: int) -> bool:
    await cleanup_if_expired(user_id)
    async with db_lock:
        cur = db.execute("SELECT balance FROM credits WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return False
        bal = int(row[0])
        if bal < CREDITS_PER_VIDEO:
            return False
        db.execute("UPDATE credits SET balance = balance - ? WHERE user_id=?", (CREDITS_PER_VIDEO, user_id))
        db.commit()
        return True


async def freebies_is_claimed(user_id: int) -> bool:
    async with db_lock:
        cur = db.execute("SELECT claimed FROM freebies WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
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


# =========================
# FFMPEG
# =========================
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


# =========================
# USER TEXTS
# =========================
async def send_status(update: Update, user_id: int):
    credits, vfrom, exp = await db_get_credit(user_id)
    videos, voices = await stats_get(user_id)

    lines = [
        f"💳 Credits: {credits}",
        f"🎬 Videos made: {videos}",
        f"🎧 Voices made: {voices}",
    ]
    if vfrom is not None and exp is not None:
        lines.append(f"✅ Start: {fmt_date(vfrom)}")
        lines.append(f"⏳ End: {fmt_date(exp)}")

    await update.message.reply_text("\n".join(lines), reply_markup=reply_menu())


async def do_free(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if not REQUIRED_CHANNEL:
        await update.message.reply_text("⚠️ Channel not configured.", reply_markup=reply_menu())
        return

    if await freebies_is_claimed(user_id):
        await update.message.reply_text("✅ You already claimed free credits.", reply_markup=reply_menu())
        await send_status(update, user_id)
        return

    if not await is_user_subscribed(context, user_id):
        kb = channel_inline_kb()
        await update.message.reply_text(
            f"🎁 Free credits পেতে আগে channel join করুন: {REQUIRED_CHANNEL}\nJoin করে আবার /free দিন।",
            reply_markup=kb or reply_menu(),
        )
        return

    await db_add_credits(user_id, FREE_CREDITS, days_valid=None)
    await freebies_mark_claimed(user_id)
    await update.message.reply_text(f"🎁 Added {FREE_CREDITS} free credits!", reply_markup=reply_menu())
    await send_status(update, user_id)


# =========================
# COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    text = (
        "✅ Welcome!\n\n"
        "🎬 Send a video → I’ll return a Circle Video Note (max 60s)\n"
        "🎙 Voice Support button → opens our Voice Channel\n\n"
        f"💳 Video cost: {CREDITS_PER_VIDEO} credit\n"
        "🎁 Free credits: /free\n"
        "📊 Status: /status"
    )
    await update.message.reply_text(text, reply_markup=reply_menu())


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    await send_status(update, update.effective_user.id)


async def free_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    await do_free(update, context, update.effective_user.id)


# =========================
# ADMIN PANEL
# =========================
async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        await update.message.reply_text("⛔ Admin only.", reply_markup=reply_menu())
        return

    async with db_lock:
        cur = db.execute("SELECT user_id, username, first_name FROM users ORDER BY last_seen DESC LIMIT 12")
        rows = cur.fetchall()

    if not rows:
        await update.message.reply_text("No users yet.", reply_markup=reply_menu())
        return

    kb = []
    for uid, username, first_name in rows:
        label = f"{uid}"
        if username:
            label += f"  @{username}"
        elif first_name:
            label += f"  {first_name}"
        kb.append([InlineKeyboardButton(label, callback_data=f"SEL:{uid}")])

    await update.message.reply_text("Select a user:", reply_markup=InlineKeyboardMarkup(kb))


async def grant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        await update.message.reply_text("⛔ Admin only.", reply_markup=reply_menu())
        return

    if admin_id not in ADMIN_SELECTED:
        await update.message.reply_text("First /users দিয়ে user select করুন।", reply_markup=reply_menu())
        return

    if len(context.args) < 1:
        await update.message.reply_text("Use: /grant <amount> <days(optional)>\nExample: /grant 10 30", reply_markup=reply_menu())
        return

    try:
        amount = int(context.args[0])
        days = int(context.args[1]) if len(context.args) >= 2 else None
    except ValueError:
        await update.message.reply_text("amount/days must be numbers.", reply_markup=reply_menu())
        return

    uid = ADMIN_SELECTED[admin_id]
    _, vfrom, exp = await db_add_credits(uid, amount, days_valid=days)

    msg = f"✅ Granted user: {uid}\n💳 Credits added: {amount}"
    if vfrom is not None and exp is not None:
        msg += f"\n✅ Start: {fmt_date(vfrom)}\n⏳ End: {fmt_date(exp)}"
    await update.message.reply_text(msg, reply_markup=reply_menu())


async def grantto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        await update.message.reply_text("⛔ Admin only.", reply_markup=reply_menu())
        return

    if len(context.args) < 2:
        await update.message.reply_text("Use: /grantto <user_id> <amount> <days(optional)>", reply_markup=reply_menu())
        return

    try:
        uid = int(context.args[0])
        amount = int(context.args[1])
        days = int(context.args[2]) if len(context.args) >= 3 else None
    except ValueError:
        await update.message.reply_text("user_id/amount/days must be numbers.", reply_markup=reply_menu())
        return

    _, vfrom, exp = await db_add_credits(uid, amount, days_valid=days)

    msg = f"✅ Granted user: {uid}\n💳 Credits added: {amount}"
    if vfrom is not None and exp is not None:
        msg += f"\n✅ Start: {fmt_date(vfrom)}\n⏳ End: {fmt_date(exp)}"
    await update.message.reply_text(msg, reply_markup=reply_menu())


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    admin_id = q.from_user.id
    if not is_admin(admin_id):
        await q.edit_message_text("Admin only.")
        return

    data = q.data or ""
    if data.startswith("SEL:"):
        uid = int(data.split(":", 1)[1])
        ADMIN_SELECTED[admin_id] = uid

        credits, vfrom, exp = await db_get_credit(uid)
        videos, voices = await stats_get(uid)

        msg = f"Selected: {uid}\n💳 Credits: {credits}\n🎬 Videos: {videos}\n🎧 Voices: {voices}"
        if vfrom is not None and exp is not None:
            msg += f"\n✅ Start: {fmt_date(vfrom)}\n⏳ End: {fmt_date(exp)}"
        msg += "\n\nNow use:\n/grant 10 30   or   /grant 5"
        await q.edit_message_text(msg)


# =========================
# MENU BUTTON HANDLER
# =========================
async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    txt = (update.message.text or "").strip()

    if txt == BTN_MODEL:
        msg = (
            "🧠 MODEL SUPPORT\n\n"
            "✅ Video → Circle Video Note (max 60s)\n\n"
            f"💳 Video cost: {CREDITS_PER_VIDEO} credit\n"
            "🎁 Free credits: /free\n"
            "📊 Status: /status"
        )
        await update.message.reply_text(msg, reply_markup=reply_menu())
        return

    if txt == BTN_VOICE:
        kb = voice_inline_kb()
        if kb:
            await update.message.reply_text(
                "🎙 VOICE SUPPORT\n\n👇 Voice Channel এ যেতে বাটনে ক্লিক করুন:",
                reply_markup=kb
            )
        else:
            await update.message.reply_text(
                "VOICE_SUPPORT_LINK সেট করা নেই।",
                reply_markup=reply_menu()
            )
        return

    if txt == BTN_ADMIN:
        kb = admin_inline_kb()
        if kb:
            await update.message.reply_text("🧑‍💼 ADMIN CONTACT", reply_markup=kb)
        else:
            await update.message.reply_text("ADMIN_CONTACTS সেট করা নেই।", reply_markup=reply_menu())
        return

    if txt == BTN_CHANNEL:
        kb = channel_inline_kb()
        if kb:
            await update.message.reply_text("📣 CHANNEL", reply_markup=kb)
        else:
            await update.message.reply_text("REQUIRED_CHANNEL সেট করা নেই।", reply_markup=reply_menu())
        return

    await update.message.reply_text("Menu থেকে অপশন সিলেক্ট করুন।", reply_markup=reply_menu())


# =========================
# VIDEO (PAID)
# =========================
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    msg = update.message
    if not msg:
        return
    user_id = update.effective_user.id

    ok = await db_deduct_video_credit(user_id)
    if not ok:
        credits, _, _ = await db_get_credit(user_id)
        text = f"❌ Credits low!\n💳 Credits: {credits}\n🎬 Need: {CREDITS_PER_VIDEO}"
        if REQUIRED_CHANNEL:
            text += f"\n\n🎁 Free credits: Join {REQUIRED_CHANNEL} then /free"
        await msg.reply_text(text, reply_markup=reply_menu())
        return

    file_id = msg.video.file_id if msg.video else None
    if not file_id and msg.document and (msg.document.mime_type or "").startswith("video/"):
        file_id = msg.document.file_id

    if not file_id:
        await db_add_credits(user_id, CREDITS_PER_VIDEO)  # refund
        await msg.reply_text("Send a video file.", reply_markup=reply_menu())
        return

    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.UPLOAD_VIDEO_NOTE)

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        inp = str(td_path / "in.mp4")
        outp = str(td_path / "out.mp4")

        try:
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(custom_path=inp)

            await run_cmd(build_ffmpeg_video_cmd(inp, outp))

            with open(outp, "rb") as f:
                await msg.reply_video_note(video_note=f, length=TARGET_SIZE)

            await stats_inc_video(user_id)

        except Exception as e:
            await db_add_credits(user_id, CREDITS_PER_VIDEO)  # refund
            await msg.reply_text(f"Convert error: {e}", reply_markup=reply_menu())


# =========================
# VOICE (FREE)
# =========================
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    msg = update.message
    if not msg:
        return
    user_id = update.effective_user.id

    file_id = None
    if msg.voice:
        file_id = msg.voice.file_id
    elif msg.audio:
        file_id = msg.audio.file_id
    elif msg.document and (msg.document.mime_type or "").startswith("audio/"):
        file_id = msg.document.file_id

    if not file_id:
        await msg.reply_text("Send voice/audio.", reply_markup=reply_menu())
        return

    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.UPLOAD_VOICE)

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        inp = str(td_path / "in_audio")
        outp = str(td_path / "out.ogg")

        try:
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(custom_path=inp)

            await run_cmd(build_ffmpeg_voice_cmd(inp, outp))

            with open(outp, "rb") as f:
                await msg.reply_voice(voice=f)

            await stats_inc_voice(user_id)

        except Exception as e:
            await msg.reply_text(f"Voice convert error: {e}", reply_markup=reply_menu())


# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var সেট করুন।")
    if REQUIRED_CHANNEL and not REQUIRED_CHANNEL.startswith("@"):
        raise RuntimeError("REQUIRED_CHANNEL অবশ্যই @ দিয়ে শুরু (যেমন @MyChannel)")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("free", free_cmd))

    # Admin
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("grant", grant_cmd))
    app.add_handler(CommandHandler("grantto", grantto_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))

    # Reply keyboard clicks
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_click))

    # Media
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.AUDIO, handle_voice))

    app.run_polling()


if __name__ == "__main__":
    main()
