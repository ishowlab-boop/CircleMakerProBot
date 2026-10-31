
import os
import tempfile
import subprocess
import shutil
from pathlib import Path
from datetime import datetime, timezone

import telebot
from telebot import types
import imageio_ffmpeg

import config
from db import DB
from admin_panel import (
    register_admin_panel,
    send_admin_panel,
    is_waiting
)


# =========================
# INIT
# =========================
db = DB(config.DB_PATH)

if not config.BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN missing! Railway Variables এ BOT_TOKEN দিন।"
    )

bot = telebot.TeleBot(
    config.BOT_TOKEN,
    parse_mode="HTML"
)


# =========================
# MENU BUTTONS
# =========================
BTN_MODEL = "🧠 MODEL SUPPORT"
BTN_VOICE = "🎙 VOICE SUPPORT"
BTN_CONTACT = "🧑‍💼 ADMIN CONTACT"
BTN_CHANNEL = "📣 CHANNEL"
BTN_USAGE = "📊 USAGE"
BTN_ADMIN_PANEL = "⚙️ ADMIN PANEL"


def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS


def safe_url(x: str) -> str:
    x = (x or "").strip()

    if not x:
        return ""

    if x.startswith(("http://", "https://")):
        return x

    if x.startswith("@"):
        return "https://t.me/" + x.lstrip("@")

    if "." in x and " " not in x:
        return "https://" + x

    return x


def url_btn(title: str, url: str):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            title,
            url=safe_url(url)
        )
    )
    return kb


# =========================
# VOICE SUPPORT MENU
# =========================
def voice_support_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)

    kb.row(
        types.InlineKeyboardButton(
            "📣 Voice Channel",
            url="https://t.me/ariyanvoice"
        ),
        types.InlineKeyboardButton(
            "🤖 Voice Bot",
            url="https://t.me/nexareal_bot"
        )
    )

    return kb


# =========================
# MAIN MENU
# =========================
def menu_kb(uid: int):
    kb = types.ReplyKeyboardMarkup(
        resize_keyboard=True
    )

    kb.row(BTN_MODEL, BTN_VOICE)
    kb.row(BTN_CONTACT, BTN_CHANNEL)
    kb.row(BTN_USAGE)

    if is_admin(uid):
        kb.row(BTN_ADMIN_PANEL)

    return kb


def fmt_date(ts):
    if ts is None:
        return "N/A"

    return datetime.fromtimestamp(
        int(ts),
        tz=timezone.utc
    ).strftime("%A, %d %b %Y")


# =========================
# JOIN CHECK
# =========================
def is_subscribed(user_id: int) -> bool:
    try:
        member = bot.get_chat_member(
            config.REQUIRED_CHANNEL,
            user_id
        )

        return member.status in (
            "creator",
            "administrator",
            "member"
        )

    except Exception:
        return False


# =========================
# FFMPEG
# =========================
TARGET_SIZE = 640
MAX_SECONDS = 60


def ffmpeg_path() -> str:
    path = os.getenv("FFMPEG_PATH")

    if path:
        return path

    system_ffmpeg = shutil.which("ffmpeg")

    if system_ffmpeg:
        return system_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def build_ffmpeg_cmd(
    inp: str,
    outp: str
) -> list[str]:

    vf = (
        f"scale={TARGET_SIZE}:{TARGET_SIZE}:"
        "force_original_aspect_ratio=increase,"
        f"crop={TARGET_SIZE}:{TARGET_SIZE},"
        "format=yuv420p"
    )

    return [
        ffmpeg_path(),
        "-y",
        "-i", inp,
        "-t", str(MAX_SECONDS),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        outp
    ]


# =========================
# START COMMAND
# =========================
@bot.message_handler(commands=["start"])
def start_cmd(message):
    db.upsert_user(message.from_user)

    uid = message.from_user.id
    credits, vfrom, exp = db.get_credit(uid)

    text = (
        "✅ <b>CircleMakerProBot</b>\n\n"
        f"🆔 <b>Your ID:</b> <code>{uid}</code>\n"
        f"🎬 <b>Video cost:</b> "
        f"<b>{config.CREDITS_PER_VIDEO}</b> credit\n"
        f"💳 <b>Your Credits:</b> <b>{credits}</b>\n"
        f"✅ <b>Start:</b> <b>{fmt_date(vfrom)}</b>\n"
        f"⏳ <b>End:</b> <b>{fmt_date(exp)}</b>\n\n"
        f"🎁 Free credits পেতে: আগে join করুন "
        f"{config.REQUIRED_CHANNEL} তারপর /free দিন ✅\n"
    )

    bot.send_message(
        message.chat.id,
        text,
        reply_markup=menu_kb(uid)
    )


# =========================
# FREE CREDITS
# =========================
@bot.message_handler(commands=["free"])
def free_cmd(message):
    db.upsert_user(message.from_user)

    uid = message.from_user.id

    if db.free_claimed(uid):
        return bot.reply_to(
            message,
            "✅ আপনি আগেই free credits নিয়েছেন।",
            reply_markup=menu_kb(uid)
        )

    if not is_subscribed(uid):
        return bot.send_message(
            message.chat.id,
            f"🎁 Free credits পেতে আগে join করুন: "
            f"{config.REQUIRED_CHANNEL}\n"
            "Join করে আবার /free দিন।",
            reply_markup=url_btn(
                "📣 Join Channel",
                config.REQUIRED_CHANNEL
            )
        )

    db.add_credits(
        uid,
        config.FREE_CREDITS
    )

    db.mark_free_claimed(uid)

    bot.send_message(
        message.chat.id,
        f"🎁 Added {config.FREE_CREDITS} free credits ✅",
        reply_markup=menu_kb(uid)
    )


# =========================
# USAGE COMMAND
# =========================
@bot.message_handler(commands=["usage"])
def usage_cmd(message):
    db.upsert_user(message.from_user)

    uid = message.from_user.id

    credits, vfrom, exp = db.get_credit(uid)
    made = db.get_usage(uid)

    text = (
        "📊 <b>USAGE</b>\n\n"
        f"🎬 Videos made: <b>{made}</b>\n"
        f"💳 Credits: <b>{credits}</b>\n"
        f"✅ Start: <b>{fmt_date(vfrom)}</b>\n"
        f"⏳ End: <b>{fmt_date(exp)}</b>\n"
    )

    bot.send_message(
        message.chat.id,
        text,
        reply_markup=menu_kb(uid)
    )


# =========================
# MENU HANDLER
# =========================
MENU_BUTTONS = {
    BTN_MODEL,
    BTN_VOICE,
    BTN_CONTACT,
    BTN_CHANNEL,
    BTN_USAGE,
    BTN_ADMIN_PANEL
}


@bot.message_handler(
    func=lambda m: (m.text or "").strip() in MENU_BUTTONS,
    content_types=["text"]
)
def menu_handler(message):
    db.upsert_user(message.from_user)

    uid = message.from_user.id
    text = (message.text or "").strip()

    # MODEL SUPPORT
    if text == BTN_MODEL:
        return bot.send_message(
            message.chat.id,
            "🧠 <b>MODEL SUPPORT</b>",
            reply_markup=url_btn(
                "Open Model Support",
                config.MODEL_SUPPORT_LINK
            )
        )

    # VOICE SUPPORT
    if text == BTN_VOICE:
        return bot.send_message(
            message.chat.id,
            "🎙 <b>VOICE SUPPORT</b>\n\n"
            "👇 Choose your option:",
            reply_markup=voice_support_kb()
        )

    # ADMIN CONTACT
    if text == BTN_CONTACT:
        return bot.send_message(
            message.chat.id,
            "🧑‍💼 <b>ADMIN CONTACT</b>",
            reply_markup=url_btn(
                "Contact Admin",
                config.ADMIN_CONTACTS
            )
        )

    # CHANNEL
    if text == BTN_CHANNEL:
        return bot.send_message(
            message.chat.id,
            "📣 <b>CHANNEL</b>",
            reply_markup=url_btn(
                "Open Channel",
                config.REQUIRED_CHANNEL
            )
        )

    # USAGE
    if text == BTN_USAGE:
        return usage_cmd(message)

    # ADMIN PANEL
    if text == BTN_ADMIN_PANEL:
        if not is_admin(uid):
            return bot.reply_to(
                message,
                "⛔ Admin only."
            )

        return send_admin_panel(
            bot,
            db,
            message.chat.id
        )


# =========================
# VIDEO HANDLER
# =========================
@bot.message_handler(
    content_types=["video", "document"]
)
def handle_video(message):
    db.upsert_user(message.from_user)

    uid = message.from_user.id
    file_id = None

    if (
        message.content_type == "video"
        and message.video
    ):
        file_id = message.video.file_id

    elif (
        message.content_type == "document"
        and message.document
    ):
        mime = message.document.mime_type or ""

        if mime.startswith("video/"):
            file_id = message.document.file_id

    if not file_id:
        return

    ok = db.deduct_for_video(
        uid,
        config.CREDITS_PER_VIDEO
    )

    if not ok:
        credits, _, _ = db.get_credit(uid)

        bot.send_message(
            message.chat.id,
            f"❌ Credits কম!\n"
            f"💳 Your Credits: <b>{credits}</b>\n"
            f"🎬 Need: <b>{config.CREDITS_PER_VIDEO}</b>\n\n"
            f"Join {config.REQUIRED_CHANNEL} তারপর /free",
            reply_markup=menu_kb(uid)
        )
        return

    bot.send_chat_action(
        message.chat.id,
        "upload_video_note"
    )

    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)

            inp = str(td / "in.mp4")
            outp = str(td / "out.mp4")

            telegram_file = bot.get_file(file_id)
            data = bot.download_file(
                telegram_file.file_path
            )

            with open(inp, "wb") as writer:
                writer.write(data)

            cmd = build_ffmpeg_cmd(inp, outp)

            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            with open(outp, "rb") as reader:
                bot.send_video_note(
                    message.chat.id,
                    reader,
                    length=TARGET_SIZE
                )

        db.inc_videos(uid)

    except Exception as e:
        db.add_credits(
            uid,
            config.CREDITS_PER_VIDEO
        )

        bot.send_message(
            message.chat.id,
            f"❌ Convert error: {e}",
            reply_markup=menu_kb(uid)
        )


# =========================
# REGISTER ADMIN PANEL
# =========================
register_admin_panel(
    bot,
    db,
    config
)


# =========================
# FALLBACK TEXT
# =========================
@bot.message_handler(
    func=lambda m: (
        m.content_type == "text"
        and m.from_user is not None
        and not is_waiting(m.from_user.id)
    ),
    content_types=["text"]
)
def fallback(message):
    db.upsert_user(message.from_user)

    uid = message.from_user.id

    bot.send_message(
        message.chat.id,
        "ভিডিও পাঠান ✅ আমি সেটাকে গোল Video Note করে দিবো।",
        reply_markup=menu_kb(uid)
    )


# =========================
# RUN BOT
# =========================
print("Bot started...")

bot.infinity_polling(
    timeout=60,
    long_polling_timeout=60
)
