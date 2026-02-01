import os
import re
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
# CONFIG (defaults included)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

DB_PATH = os.getenv("DB_PATH", "credits.db")

TARGET_SIZE = 640
MAX_SECONDS = 60

CREDITS_PER_VIDEO = int(os.getenv("CREDITS_PER_VIDEO", "1"))
FREE_CREDITS = int(os.getenv("FREE_CREDITS", "2"))

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@iuo82828").strip()
VOICE_SUPPORT_LINK = os.getenv("VOICE_SUPPORT_LINK", "https://t.me/ariyanvoice").strip()
MODEL_SUPPORT_LINK = os.getenv("MODEL_SUPPORT_LINK", "https://modelboxbd.com").strip()
ADMIN_CONTACTS = os.getenv("ADMIN_CONTACTS", "https://t.me/AriyanFix").strip()

ADMIN_IDS = set()
_admin_raw = os.getenv("ADMIN_IDS", "").strip()
if _admin_raw:
    ADMIN_IDS = {int(x.strip()) for x in _admin_raw.split(",") if x.strip().isdigit()}


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def now_ts() -> int:
    return int(time.time())


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%A, %d %b %Y")


def parse_int(text: str) -> int:
    nums = re.findall(r"\d+", text or "")
    if not nums:
        raise ValueError("No number found")
    return int(nums[0])


# =========================
# UI: Menu buttons (emoji exactly)
# =========================
BTN_MODEL = "🧠 MODEL SUPPORT"
BTN_VOICE = "🎙 VOICE SUPPORT"
BTN_ADMIN_CONTACT = "🧑‍💼 ADMIN CONTACT"
BTN_CHANNEL = "📣 CHANNEL"
BTN_USAGE = "📊 USAGE"

# accept both emoji/non-emoji just in case
BTN_MODEL_ALT = "MODEL SUPPORT"
BTN_VOICE_ALT = "VOICE SUPPORT"
BTN_ADMIN_ALT = "ADMIN CONTACT"
BTN_CHANNEL_ALT = "CHANNEL"
BTN_USAGE_ALT = "USAGE"


def reply_menu() -> ReplyKeyboardMarkup:
    keyboard = [
        [BTN_MODEL, BTN_VOICE],
        [BTN_ADMIN_CONTACT, BTN_CHANNEL],
        [BTN_USAGE],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def to_url(x: str) -> str:
    x = (x or "").strip()
    if not x:
        return ""
    if x.startswith("http://") or x.startswith("https://"):
        return x
    if x.startswith("@"):
        return f"https://t.me/{x.lstrip('@')}"
    if "." in x and " " not in x:
        return f"https://{x}"
    return x


def parse_links(raw: str) -> list[str]:
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        u = to_url(part)
        if u:
            out.append(u)
    return out


def kb_url_button(title: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(title, url=url)]])


def kb_admin_contacts() -> InlineKeyboardMarkup:
    links = parse_links(ADMIN_CONTACTS)
    rows = [[InlineKeyboardButton(f"👤 Admin {i}", url=link)] for i, link in enumerate(links, start=1)]
    return InlineKeyboardMarkup(rows)


def kb_channel() -> InlineKeyboardMarkup:
    return kb_url_button("📣 Open Channel", to_url(REQUIRED_CHANNEL))


def kb_voice_support() -> InlineKeyboardMarkup:
    return kb_url_button("🎙 Open Voice Support", to_url(VOICE_SUPPORT_LINK))


def kb_model_support() -> InlineKeyboardMarkup:
    return kb_url_button("🧠 Open Model Support", to_url(MODEL_SUPPORT_LINK))


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
db.execute("CREATE TABLE IF NOT EXISTS freebies (user_id INTEGER PRIMARY KEY, claimed INTEGER NOT NULL DEFAULT 0)")
db.execute(
    "CREATE TABLE IF NOT EXISTS stats ("
    "user_id INTEGER PRIMARY KEY, videos_made INTEGER NOT NULL DEFAULT 0, "
    "voices_made INTEGER NOT NULL DEFAULT 0)"
)
db.commit()


async def ensure_user(user_id: int):
    async with db_lock:
        db.execute("INSERT OR IGNORE INTO credits(user_id, balance, valid_from, expires_at) VALUES (?, 0, NULL, NULL)", (user_id,))
        db.execute("INSERT OR IGNORE INTO freebies(user_id, claimed) VALUES (?, 0)", (user_id,))
        db.execute("INSERT OR IGNORE INTO stats(user_id, videos_made, voices_made) VALUES (?, 0, 0)", (user_id,))
        db.commit()


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
        db.commit()
    await ensure_user(u.id)


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
    await ensure_user(user_id)
    await cleanup_if_expired(user_id)
    async with db_lock:
        cur = db.execute("SELECT balance, valid_from, expires_at FROM credits WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return 0, None, None
        bal = int(row[0])
        vfrom = int(row[1]) if row[1] is not None else None
        exp = int(row[2]) if row[2] is not None else None
        return bal, vfrom, exp


async def db_add_credits(user_id: int, amount: int) -> None:
    await ensure_user(user_id)
    async with db_lock:
        db.execute("UPDATE credits SET balance = balance + ? WHERE user_id=?", (amount, user_id))
        db.commit()


async def db_remove_credits(user_id: int, amount: int) -> None:
    await ensure_user(user_id)
    async with db_lock:
        cur = db.execute("SELECT balance FROM credits WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        bal = int(row[0]) if row else 0
        new_bal = max(0, bal - amount)
        db.execute("UPDATE credits SET balance=? WHERE user_id=?", (new_bal, user_id))
        db.commit()


async def db_set_validity(user_id: int, days: int) -> None:
    await ensure_user(user_id)
    start = now_ts()
    end = start + days * 86400
    async with db_lock:
        db.execute("UPDATE credits SET valid_from=?, expires_at=? WHERE user_id=?", (start, end, user_id))
        db.commit()


async def db_remove_validity(user_id: int) -> None:
    await ensure_user(user_id)
    async with db_lock:
        db.execute("UPDATE credits SET valid_from=NULL, expires_at=NULL WHERE user_id=?", (user_id,))
        db.commit()


async def db_deduct_video_credit(user_id: int) -> bool:
    await ensure_user(user_id)
    await cleanup_if_expired(user_id)
    async with db_lock:
        cur = db.execute("SELECT balance FROM credits WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        bal = int(row[0]) if row else 0
        if bal < CREDITS_PER_VIDEO:
            return False
        db.execute("UPDATE credits SET balance = balance - ? WHERE user_id=?", (CREDITS_PER_VIDEO, user_id))
        db.commit()
        return True


async def freebies_is_claimed(user_id: int) -> bool:
    await ensure_user(user_id)
    async with db_lock:
        cur = db.execute("SELECT claimed FROM freebies WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return bool(row and int(row[0]) == 1)


async def freebies_mark_claimed(user_id: int) -> None:
    await ensure_user(user_id)
    async with db_lock:
        db.execute("UPDATE freebies SET claimed=1 WHERE user_id=?", (user_id,))
        db.commit()


async def stats_inc_video(user_id: int) -> None:
    await ensure_user(user_id)
    async with db_lock:
        db.execute("UPDATE stats SET videos_made = videos_made + 1 WHERE user_id=?", (user_id,))
        db.commit()


async def stats_inc_voice(user_id: int) -> None:
    await ensure_user(user_id)
    async with db_lock:
        db.execute("UPDATE stats SET voices_made = voices_made + 1 WHERE user_id=?", (user_id,))
        db.commit()


async def stats_get(user_id: int) -> tuple[int, int]:
    await ensure_user(user_id)
    async with db_lock:
        cur = db.execute("SELECT videos_made, voices_made FROM stats WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return 0, 0
        return int(row[0]), int(row[1])


async def list_users(limit: int = 50):
    async with db_lock:
        cur = db.execute(
            "SELECT u.user_id, u.username, COALESCE(c.balance,0) "
            "FROM users u LEFT JOIN credits c ON u.user_id=c.user_id "
            "ORDER BY u.last_seen DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
    return [{"id": int(uid), "username": username, "credits": int(bal or 0)} for uid, username, bal in rows]


async def list_premium_users(limit: int = 50):
    t = now_ts()
    async with db_lock:
        cur = db.execute(
            "SELECT u.user_id, u.username, COALESCE(c.balance,0), c.valid_from, c.expires_at "
            "FROM users u JOIN credits c ON u.user_id=c.user_id "
            "WHERE c.expires_at IS NOT NULL AND c.expires_at > ? "
            "ORDER BY c.expires_at ASC LIMIT ?",
            (t, limit),
        )
        rows = cur.fetchall()
    return [{"id": int(uid), "username": username, "credits": int(bal or 0), "vfrom": vfrom, "exp": exp}
            for uid, username, bal, vfrom, exp in rows]


async def is_user_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
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
# USER FEATURES
# =========================
async def send_usage(update: Update, user_id: int):
    credits, vfrom, exp = await db_get_credit(user_id)
    videos, voices = await stats_get(user_id)
    lines = [
        "📊 USAGE",
        f"🎬 Videos made: {videos}",
        f"🎧 Voices made: {voices}",
        f"💳 Credits: {credits}",
    ]
    if vfrom is not None and exp is not None:
        lines.append(f"✅ Start: {fmt_date(vfrom)}")
        lines.append(f"⏳ End: {fmt_date(exp)}")
    await update.message.reply_text("\n".join(lines), reply_markup=reply_menu())


async def do_free(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if await freebies_is_claimed(user_id):
        await update.message.reply_text("✅ You already claimed free credits.", reply_markup=reply_menu())
        return

    if not await is_user_subscribed(context, user_id):
        await update.message.reply_text(
            f"🎁 Free credits পেতে আগে join করুন: {REQUIRED_CHANNEL}\nJoin করে আবার /free দিন।",
            reply_markup=kb_channel(),
        )
        return

    await db_add_credits(user_id, FREE_CREDITS)
    await freebies_mark_claimed(user_id)
    await update.message.reply_text(f"🎁 Added {FREE_CREDITS} free credits!", reply_markup=reply_menu())


# =========================
# ADMIN PANEL (Inline)
# =========================
admin_steps: dict[int, dict] = {}


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Manage Credits", callback_data="admin:credits")],
        [InlineKeyboardButton("Manage Validity", callback_data="admin:validity")],
        [InlineKeyboardButton("List Users", callback_data="admin:list_users")],
        [InlineKeyboardButton("List Premium Users", callback_data="admin:list_premium")],
        [InlineKeyboardButton("Broadcast", callback_data="admin:broadcast")],
        [InlineKeyboardButton("Download Data", callback_data="admin:download")],
    ])


def credit_action_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Credits", callback_data=f"admin:credits:add:{user_id}")],
        [InlineKeyboardButton("➖ Remove Credits", callback_data=f"admin:credits:remove:{user_id}")],
        [InlineKeyboardButton("⬅ Back", callback_data="admin:menu")],
    ])


def validity_action_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Set Validity", callback_data=f"admin:validity:set:{user_id}")],
        [InlineKeyboardButton("❌ Remove Validity", callback_data=f"admin:validity:remove:{user_id}")],
        [InlineKeyboardButton("⬅ Back", callback_data="admin:menu")],
    ])


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only. First set ADMIN_IDS in Railway.", reply_markup=reply_menu())
        return
    await update.message.reply_text("⚙️ Admin Panel", reply_markup=admin_menu_kb())


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    uid = q.from_user.id
    if not is_admin(uid):
        return

    parts = (q.data or "").split(":")
    section = parts[1] if len(parts) > 1 else ""

    if section == "menu":
        await q.message.reply_text("⚙️ Admin Panel", reply_markup=admin_menu_kb())
        return

    if section == "credits" and len(parts) == 2:
        admin_steps[uid] = {"action": "credits_pick_user"}
        await q.message.reply_text("Send User ID for credits:")
        return

    if section == "credits" and len(parts) >= 4 and parts[2] in ("add", "remove"):
        action = parts[2]
        target = int(parts[3])
        admin_steps[uid] = {"action": f"credits_{action}_amount", "target": target}
        await q.message.reply_text(f"Send amount to {action.upper()} for {target}:")
        return

    if section == "validity" and len(parts) == 2:
        admin_steps[uid] = {"action": "validity_pick_user"}
        await q.message.reply_text("Send User ID for validity:")
        return

    if section == "validity" and len(parts) >= 4 and parts[2] in ("set", "remove"):
        target = int(parts[3])
        if parts[2] == "remove":
            await db_remove_validity(target)
            await q.message.reply_text(f"✅ Validity removed for {target}")
            return
        admin_steps[uid] = {"action": "validity_days", "target": target}
        await q.message.reply_text(f"Send validity days for {target}:")
        return

    if section == "list_users":
        users = await list_users(limit=50)
        text = "\n".join([f"{u['id']} @{u.get('username') or 'unknown'} | credits={u['credits']}" for u in users])
        await q.message.reply_text(text or "No users")
        return

    if section == "list_premium":
        users = await list_premium_users(limit=50)
        if not users:
            await q.message.reply_text("No premium users")
            return
        lines = []
        for u in users:
            lines.append(
                f"👤 {u['id']} @{u.get('username') or 'unknown'}\n"
                f"💳 Credits: {u['credits']}\n"
                f"✅ Start: {fmt_date(int(u['vfrom'])) if u['vfrom'] else 'N/A'}\n"
                f"⏳ End: {fmt_date(int(u['exp'])) if u['exp'] else 'N/A'}\n"
                f"----------------------"
            )
        await q.message.reply_text("\n".join(lines))
        return

    if section == "broadcast":
        admin_steps[uid] = {"action": "broadcast"}
        await q.message.reply_text("Send broadcast message:")
        return

    if section == "download":
        try:
            with open(DB_PATH, "rb") as f:
                await context.bot.send_document(chat_id=q.message.chat.id, document=f)
        except Exception:
            await q.message.reply_text("DB not found!")
        return


async def admin_step_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    uid = msg.from_user.id

    # only run when admin is in a step
    if uid not in admin_steps:
        return
    if not is_admin(uid):
        admin_steps.pop(uid, None)
        return

    step = admin_steps.pop(uid)
    action = step.get("action")

    try:
        if action == "credits_pick_user":
            target = parse_int(msg.text)
            await ensure_user(target)
            await msg.reply_text(
                f"User {target}\nChoose credits action:",
                reply_markup=credit_action_kb(target),
            )
            return

        if action == "credits_add_amount":
            amount = parse_int(msg.text)
            target = int(step.get("target"))
            await db_add_credits(target, amount)
            await msg.reply_text(f"✅ Added {amount} credits to {target}")
            return

        if action == "credits_remove_amount":
            amount = parse_int(msg.text)
            target = int(step.get("target"))
            await db_remove_credits(target, amount)
            await msg.reply_text(f"✅ Removed {amount} credits from {target}")
            return

        if action == "validity_pick_user":
            target = parse_int(msg.text)
            await ensure_user(target)
            await msg.reply_text(
                f"User {target}\nChoose validity action:",
                reply_markup=validity_action_kb(target),
            )
            return

        if action == "validity_days":
            days = parse_int(msg.text)
            target = int(step.get("target"))
            await db_set_validity(target, days)
            await msg.reply_text(f"✅ Validity set: {days} days for {target}")
            return

        if action == "broadcast":
            text = (msg.text or "").strip()
            users = await list_users(limit=100000)
            sent = 0
            failed = 0
            await msg.reply_text("📣 Broadcast started...")

            for u in users:
                uid2 = u.get("id")
                if not uid2:
                    continue
                try:
                    await context.bot.send_message(chat_id=uid2, text=text)
                    sent += 1
                    await asyncio.sleep(0.05)
                except Exception:
                    failed += 1
                    await asyncio.sleep(0.2)

            await msg.reply_text(f"📣 Done.\n✅ Sent: {sent}\n❌ Failed: {failed}")
            return

    except Exception as e:
        await msg.reply_text(f"❌ Error: {e}")


# =========================
# USER COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    uid = update.effective_user.id
    text = (
        "✅ Welcome!\n\n"
        f"🆔 Your ID: {uid}\n\n"
        f"🎬 Video cost: {CREDITS_PER_VIDEO} credit\n"
        "🎙 Voice is FREE\n\n"
        "🎁 Free credits: /free\n"
        "⚙️ Admin panel: /admin (admin only)\n"
    )
    await update.message.reply_text(text, reply_markup=reply_menu())


async def free_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    await do_free(update, context, update.effective_user.id)


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Your ID: {update.effective_user.id}", reply_markup=reply_menu())


# =========================
# MENU CLICK HANDLER
# =========================
async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    txt = (update.message.text or "").strip()

    # normalize for old keyboards
    if txt in (BTN_MODEL, BTN_MODEL_ALT):
        await update.message.reply_text("🧠 MODEL SUPPORT", reply_markup=kb_model_support())
        return

    if txt in (BTN_VOICE, BTN_VOICE_ALT):
        await update.message.reply_text("🎙 VOICE SUPPORT", reply_markup=kb_voice_support())
        return

    if txt in (BTN_ADMIN_CONTACT, BTN_ADMIN_ALT):
        await update.message.reply_text("🧑‍💼 ADMIN CONTACT", reply_markup=kb_admin_contacts())
        return

    if txt in (BTN_CHANNEL, BTN_CHANNEL_ALT):
        await update.message.reply_text("📣 CHANNEL", reply_markup=kb_channel())
        return

    if txt in (BTN_USAGE, BTN_USAGE_ALT):
        await send_usage(update, update.effective_user.id)
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
        await msg.reply_text(
            f"❌ Credits low!\n💳 Credits: {credits}\n🎬 Need: {CREDITS_PER_VIDEO}\n\n🎁 Join {REQUIRED_CHANNEL} then /free",
            reply_markup=reply_menu(),
        )
        return

    file_id = msg.video.file_id if msg.video else None
    if not file_id and msg.document and (msg.document.mime_type or "").startswith("video/"):
        file_id = msg.document.file_id

    if not file_id:
        await db_add_credits(user_id, CREDITS_PER_VIDEO)  # refund
        await msg.reply_text("ভিডিও ফাইল দিন।", reply_markup=reply_menu())
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO_NOTE)

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
        await msg.reply_text("Voice/Audio পাঠান।", reply_markup=reply_menu())
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VOICE)

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
# MAIN (handler order FIXED)
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var সেট করুন।")
    if REQUIRED_CHANNEL and not REQUIRED_CHANNEL.startswith("@"):
        raise RuntimeError("REQUIRED_CHANNEL অবশ্যই @ দিয়ে শুরু (যেমন @iuo82828)")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("free", free_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))

    # Admin callbacks
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^admin:"), group=0)

    # ✅ Admin steps FIRST (so menu doesn't interfere)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_step_handler), group=0)

    # ✅ Menu clicks SECOND
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_click), group=1)

    # Media
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.AUDIO, handle_voice))

    app.run_polling()


if __name__ == "__main__":
    main()
