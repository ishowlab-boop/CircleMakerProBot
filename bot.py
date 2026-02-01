import os
import asyncio
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")  # export BOT_TOKEN="xxxx"

TARGET_SIZE = 640          # video note diameter (width=height)
MAX_SECONDS = 60           # Telegram video note is up to ~1 minute

async def run_cmd(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode("utf-8", errors="ignore"))

def build_ffmpeg_cmd(inp: str, outp: str) -> list[str]:
    # Center-crop to square, scale to 640x640, force a widely compatible pixel format
    vf = f"scale={TARGET_SIZE}:{TARGET_SIZE}:force_original_aspect_ratio=increase," \
         f"crop={TARGET_SIZE}:{TARGET_SIZE},format=yuv420p"
    return [
        "ffmpeg", "-y",
        "-i", inp,
        "-t", str(MAX_SECONDS),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        outp
    ]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ভিডিও পাঠান—আমি সেটাকে গোল Video Note করে ফেরত দেব (সর্বোচ্চ 60 সেকেন্ড)।"
    )

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    # ভিডিও কোথায় এসেছে (video / document)
    file_id = None
    if msg.video:
        file_id = msg.video.file_id
    elif msg.document and (msg.document.mime_type or "").startswith("video/"):
        file_id = msg.document.file_id

    if not file_id:
        await msg.reply_text("ভিডিও ফাইল পাঠান (video বা video document)।")
        return

    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.UPLOAD_VIDEO_NOTE)

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        inp = str(td_path / "in.mp4")
        outp = str(td_path / "out.mp4")

        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(custom_path=inp)

        cmd = build_ffmpeg_cmd(inp, outp)
        try:
            await run_cmd(cmd)
        except Exception as e:
            await msg.reply_text(f"কনভার্ট করতে সমস্যা হয়েছে: {e}")
            return

        # sendVideoNote: length = diameter/size
        with open(outp, "rb") as f:
            await msg.reply_video_note(video_note=f, length=TARGET_SIZE)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var সেট করুন।")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, handle_video))

    app.run_polling()

if __name__ == "__main__":
    main()
