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
# CONFIG
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

# =========================
# ADMIN (OWNER_ID always admin)
# =========================
ADMIN_IDS = set()

_admin_raw = os.getenv("ADMIN_IDS", "").strip()
if _admin_raw:
    ADMIN_IDS.update({int(x.strip()) for x in _admin_raw.split(",") if x.strip().isdigit()})

OWNER_ID = os.getenv("OWNER_ID", "").strip()
if OWNER_ID.isdigit():
    ADMIN_IDS.add(int(OWNER_ID))


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def now_ts() -> int:
    return int(time.time())


def fmt_date(ts: int | None) -> str:
    if ts is None:
        return "N/A"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%A, %d %b %Y")


def parse_int(text: str) -> int:
    nums = re.findall(r"\d+", text or "")
    if not nums:
        raise ValueError("No number found")
    return int(nums[0])


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


# =========================
# UI: MENU
# =========================
BTN_MODEL = "🧠 MODEL SUPPORT"
BTN_VOICE = "🎙 VOICE SUPPORT"
BTN_ADMIN_CONTACT = "🧑‍💼 ADMIN CONTACT"
BTN_CHANNEL = "📣 CHANNEL"
BTN_USAGE = "📊 USAGE"
BTN_ADMIN_PANEL = "⚙️ ADMIN PANEL"

# older text fallback
BTN_MODEL_ALT = "MODEL SUPPORT"
BTN_VOICE_ALT = "VOICE SUPPORT"
BTN_ADMIN_ALT = "ADMIN CONTACT"
BTN_CHANNEL_ALT = "CHANNEL"
BTN_USAGE_ALT = "USAGE"
BTN_ADMIN_PANEL_ALT = "ADMIN PANEL"


def reply_menu() -> ReplyKeyboardMarkup:
    keyboard = [
        [BTN_MODEL, BTN_VOICE],
        [BTN_ADMIN_CONTACT, BTN_CHANNEL],
        [BTN_USAGE, BTN_ADMIN_PANEL],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def kb_url_button(title: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(title, url=url)]])


def kb_model_support() -> InlineKeyboardMarkup:
    return kb_url_button("🧠 Open Model Support", to_url(MODEL_SUPPORT_LINK))


def kb_voice_support() -> InlineKeyboardMarkup:
    return kb_url_button("🎙 Open Voice Support", to_url(VOICE_SUPPORT_LINK))


def kb_channel() -> InlineKeyboardMarkup:
    return kb_url_button("📣 Open Channel", to_url(REQUIRED_CHANNEL))


def kb_admin_contacts() -> InlineKeyboardMarkup:
    links = parse_links(ADMIN_CONTACTS)
    rows = [[InlineKeyboardButton(f"👤 Admin {i}", url=link)] for i, link in enumerate(links, start=1)]
    return InlineKeyboardMarkup(rows)


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


async def db_get_credit(user_id: int):
    await ensure_user(user_id)
    await cleanup_if_expired(user_id)
    async with db_lock:
        cur = db.execute("SELECT balance, valid_from, expires_at FROM credits WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return 0, None, None
        return int(row[0]), row[1], row[2]


async def db_add_credits(user_id: int, amount: int):
    await ensure_user(user_id)
    async with db_lock:
        db.execute("UPDATE credits SET balance = balance + ? WHERE user_id=?", (amount, user_id))
        db.commit()


async def db_remove_credits(user_id: int, amount: int):
    await ensure_user(user_id)
    async with db_lock:
        cur = db.execute("SELECT balance FROM credits WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        bal = int(row[0]) if row else 0
        new_bal = max(0, bal - amount)
        db.execute("UPDATE credits SET balance=? WHERE user_id=?", (new_bal, user_id))
        db.commit()


async def db_set_validity(user_id: int, days: int):
    await ensure_user(user_id)
    start = now_ts()
    end = start + days * 86400
    async with db_lock:
        db.execute("UPDATE credits SET valid_from=?, expires_at=? WHERE user_id=?", (start, end, user_id))
        db.commit()


async def db_remove_validity(user_id: int):
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


async def freebies_mark_claimed(user_id: int):
    await ensure_user(user_id)
    async with db_lock:
        db.execute("UPDATE freebies SET claimed=1 WHERE user_id=?", (user_id,))
        db.commit()


async def stats_inc_video(user_id: int):
    await ensure_user(user_id)
    async with db_lock:
        db.execute("UPDATE stats SET videos_made = videos_made + 1 WHERE user_id=?", (user_id,))
        db.commit()


async def stats_inc_voice(user_id: int):
    await ensure_user(user_id)
    async with db_lock:
        db.execute("UPDATE stats SET voices_made = voices_made + 1 WHERE user_id=?", (user_id,))
        db.commit()


async def stats_get(user_id: int):
    await ensure_user(user_id)
    async with db_lock:
        cur = db.execute("SELECT videos_made, voices_made FROM stats WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return 0, 0
        return int(row[0]), int(row[1])


async def list_users(offset: int = 0, limit: int = 10):
    async with db_lock:
        cur = db.execute(
            "SELECT u.user_id, u.username, COALESCE(c.balance,0), c.valid_from, c.expires_at "
            "FROM users u LEFT JOIN credits c ON u.user_id=c.user_id "
            "ORDER BY u.last_seen DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = cur.fetchall()
    out = []
    for uid, username, bal, vfrom, exp in rows:
        out.append({"id": int(uid), "username": username, "credits": int(bal or 0), "vfrom": vfrom, "exp": exp})
    return out


async def count_users() -> int:
    async with db_lock:
        cur = db.execute("SELECT COUNT(*) FROM users")
        return int(cur.fetchone()[0])


async def list_premium(limit: int = 50):
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
    out = []
    for uid, username, bal, vfrom, exp in rows:
        out.append({"id": int(uid), "username": username, "credits": int(bal or 0), "vfrom": vfrom, "exp": exp})
    return out


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
# ADMIN PANEL (UI like your screenshot)
# =========================
admin_steps: dict[int, dict] = {}


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Users", callback_data="admin:users:0")],
        [InlineKeyboardButton("⭐ Premium Users", callback_data="admin:premium")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="admin:broadcast")],
        [InlineKeyboardButton("⬇️ Download DB", callback_data="admin:download")],
    ])


def users_page_kb(users: list[dict], offset: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    for u in users:
        label = f"👤 {u['id']} ({u['credits']})"
        if u.get("username"):
            label = f"👤 {u['id']} @{u['username']} ({u['credits']})"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"admin:user:{u['id']}:{offset}")])

    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"admin:users:{max(0, offset-10)}"))
    if offset + 10 < total:
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"admin:users:{offset+10}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("⬅ Back", callback_data="admin:menu")])
    return InlineKeyboardMarkup(rows)


def user_actions_kb(user_id: int, back_offset: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ +1", callback_data=f"admin:add:{user_id}:1:{back_offset}"),
            InlineKeyboardButton("➕ +5", callback_data=f"admin:add:{user_id}:5:{back_offset}"),
            InlineKeyboardButton("➕ +10", callback_data=f"admin:add:{user_id}:10:{back_offset}"),
        ],
        [
            InlineKeyboardButton("➖ -1", callback_data=f"admin:rem:{user_id}:1:{back_offset}"),
            InlineKeyboardButton("➖ -5", callback_data=f"admin:rem:{user_id}:5:{back_offset}"),
            InlineKeyboardButton("➖ -10", callback_data=f"admin:rem:{user_id}:10:{back_offset}"),
        ],
        [
            InlineKeyboardButton("✍ Custom Credit", callback_data=f"admin:custom_credit:{user_id}:{back_offset}"),
        ],
        [
            InlineKeyboardButton("✅ Valid 7d", callback_data=f"admin:valid:{user_id}:7:{back_offset}"),
            InlineKeyboardButton("✅ Valid 30d", callback_data=f"admin:valid:{user_id}:30:{back_offset}"),
            InlineKeyboardButton("✅ Valid 90d", callback_data=f"admin:valid:{user_id}:90:{back_offset}"),
        ],
        [
            InlineKeyboardButton("✍ Custom Validity", callback_data=f"admin:custom_valid:{user_id}:{back_offset}"),
            InlineKeyboardButton("❌ Remove Validity", callback_data=f"admin:valid_remove:{user_id}:{back_offset}"),
        ],
        [
            InlineKeyboardButton("⬅ Back to Users", callback_data=f"admin:users:{back_offset}"),
            InlineKeyboardButton("🏠 Admin Menu", callback_data="admin:menu"),
        ],
    ])


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.", reply_markup=reply_menu())
        return
    await update.message.reply_text("⚙️ Admin Panel", reply_markup=admin_menu_kb())


async def show_user_detail(chat_id: int, context: ContextTypes.DEFAULT_TYPE, user_id: int, back_offset: int):
    credits, vfrom, exp = await db_get_credit(user_id)
    videos, voices = await stats_get(user_id)

    text = (
        f"👤 User: {user_id}\n"
        f"💳 Credits: {credits}\n"
        f"✅ Start: {fmt_date(vfrom)}\n"
        f"⏳ End: {fmt_date(exp)}\n"
        f"🎬 Videos made: {videos}\n"
        f"🎧 Voices made: {voices}\n"
    )
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=user_actions_kb(user_id, back_offset))


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    uid = q.from_user.id
    if not is_admin(uid):
        return

    parts = (q.data or "").split(":")
    if len(parts) < 2:
        return

    action = parts[1]

    if action == "menu":
        await q.message.reply_text("⚙️ Admin Panel", reply_markup=admin_menu_kb())
        return

    if action == "users":
        offset = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
        total = await count_users()
        users = await list_users(offset=offset, limit=10)
        await q.message.reply_text(
            f"👥 Users (showing {offset+1}-{min(offset+10,total)} of {total})",
            reply_markup=users_page_kb(users, offset, total),
        )
        return

    if action == "user":
        # admin:user:<uid>:<back_offset>
        target = int(parts[2])
        back_offset = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
        await show_user_detail(q.message.chat.id, context, target, back_offset)
        return

    if action in ("add", "rem", "valid"):
        target = int(parts[2])
        amount = int(parts[3])
        back_offset = int(parts[4]) if len(parts) >= 5 and parts[4].isdigit() else 0

        if action == "add":
            await db_add_credits(target, amount)
        elif action == "rem":
            await db_remove_credits(target, amount)
        else:
            await db_set_validity(target, amount)  # amount is days

        await show_user_detail(q.message.chat.id, context, target, back_offset)
        return

    if action == "valid_remove":
        target = int(parts[2])
        back_offset = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
        await db_remove_validity(target)
        await show_user_detail(q.message.chat.id, context, target, back_offset)
        return

    if action == "custom_credit":
        target = int(parts[2])
        back_offset = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
        admin_steps[uid] = {"type": "custom_credit", "target": target, "back": back_offset}
        await q.message.reply_text("Send amount like: +50 or -20")
        return

    if action == "custom_valid":
        target = int(parts[2])
        back_offset = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
        admin_steps[uid] = {"type": "custom_valid", "target": target, "back": back_offset}
        await q.message.reply_text("Send validity days (example: 30)")
        return

    if action == "premium":
        users = await list_premium(limit=50)
        if not users:
            await q.message.reply_text("No premium users right now.")
            return
        lines = []
        for u in users:
            lines.append(
                f"👤 User: {u['id']}\n"
                f"💳 Credits: {u['credits']}\n"
                f"✅ Start: {fmt_date(u['vfrom'])}\n"
                f"⏳ End: {fmt_date(u['exp'])}\n"
                f"----------------------"
            )
        await q.message.reply_text("\n".join(lines))
        return

    if action == "broadcast":
        admin_steps[uid] = {"type": "broadcast"}
        await q.message.reply_text("Send broadcast message text now:")
        return

    if action == "download":
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

    if uid not in admin_steps:
        return
    if not is_admin(uid):
        admin_steps.pop(uid, None)
        return

    step = admin_steps.pop(uid)

    try:
        if step["type"] == "custom_credit":
            target = int(step["target"])
            back_offset = int(step["back"])
            raw = (msg.text or "").strip()

            sign = 1
            if raw.startswith("-"):
                sign = -1
            amount = parse_int(raw)

            if sign == 1:
                await db_add_credits(target, amount)
            else:
                await db_remove_credits(target, amount)

            await show_user_detail(msg.chat.id, context, target, back_offset)
            return

        if step["type"] == "custom_valid":
            target = int(step["target"])
            back_offset = int(step["back"])
            days = parse_int(msg.text)
            await db_set_validity(target, days)
            await show_user_detail(msg.chat.id, context, target, back_offset)
            return

        if step["type"] == "broadcast":
            text = (msg.text or "").strip()
            total = await count_users()
            sent = 0
            failed = 0
            await msg.reply_text(f"📣 Broadcasting to {total} users...")

            offset = 0
            while True:
                batch = await list_users(offset=offset, limit=50)
                if not batch:
                    break
                for u in batch:
                    try:
                        await context.bot.send_message(chat_id=u["id"], text=text)
                        sent += 1
                        await asyncio.sleep(0.05)
                    except Exception:
                        failed += 1
                        await asyncio.sleep(0.2)
                offset += 50

            await msg.reply_text(f"✅ Broadcast done.\nSent: {sent}\nFailed: {failed}")
            return

    except Exception as e:
        await msg.reply_text(f"❌ Error: {e}")


# =========================
# USER COMMANDS + MENU
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    uid = update.effective_user.id
    text = (
        "✅ Welcome!\n\n"
        f"🆔 Your ID: {uid}\n"
        f"🎬 Video cost: {CREDITS_PER_VIDEO} credit\n"
        "🎙 Voice is FREE\n\n"
        "🎁 Free credits: /free (join channel first)\n"
        "⚙️ Admin panel: /admin\n"
    )
    await update.message.reply_text(text, reply_markup=reply_menu())


async def free_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    uid = update.effective_user.id

    if await freebies_is_claimed(uid):
        await update.message.reply_text("✅ You already claimed free credits.", reply_markup=reply_menu())
        return

    if not await is_user_subscribed(context, uid):
        await update.message.reply_text(
            f"🎁 Free credits পেতে আগে join করুন: {REQUIRED_CHANNEL}\nJoin করে আবার /free দিন।",
            reply_markup=kb_channel(),
        )
        return

    await db_add_credits(uid, FREE_CREDITS)
    await freebies_mark_claimed(uid)
    await update.message.reply_text(f"🎁 Added {FREE_CREDITS} free credits!", reply_markup=reply_menu())


async def send_usage(update: Update):
    uid = update.effective_user.id
    credits, vfrom, exp = await db_get_credit(uid)
    videos, voices = await stats_get(uid)
    lines = [
        "📊 USAGE",
        f"🎬 Videos made: {videos}",
        f"🎧 Voices made: {voices}",
        f"💳 Credits: {credits}",
        f"✅ Start: {fmt_date(vfrom)}",
        f"⏳ End: {fmt_date(exp)}",
    ]
    await update.message.reply_text("\n".join(lines), reply_markup=reply_menu())


async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update)
    txt = (update.message.text or "").strip()

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
        await send_usage(update)
        return

    if txt in (BTN_ADMIN_PANEL, BTN_ADMIN_PANEL_ALT):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Admin only.", reply_markup=reply_menu())
            return
        await update.message.reply_text("⚙️ Admin Panel", reply_markup=admin_menu_kb())
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
# MAIN (handler order important)
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var সেট করুন।")
    if REQUIRED_CHANNEL and not REQUIRED_CHANNEL.startswith("@"):
        raise RuntimeError("REQUIRED_CHANNEL অবশ্যই @ দিয়ে শুরু (যেমন @iuo82828)")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("free", free_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))

    # admin callbacks
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^admin:"), group=0)

    # admin steps must run BEFORE menu text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_step_handler), group=0)

    # menu handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_click), group=1)

    # media
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.AUDIO, handle_voice))

    app.run_polling()


if __name__ == "__main__":
    main()
