import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
ADMIN_IDS = [OWNER_ID] if OWNER_ID else []

DB_PATH = os.getenv("DB_PATH", "file.db")

# credits (only for VIDEO)
FREE_CREDITS = int(os.getenv("FREE_CREDITS", "2"))
CREDITS_PER_VIDEO = int(os.getenv("CREDITS_PER_VIDEO", "1"))

# links / channel
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@iuo82828")
VOICE_SUPPORT_LINK = os.getenv("VOICE_SUPPORT_LINK", "https://t.me/ariyanvoice")
MODEL_SUPPORT_LINK = os.getenv("MODEL_SUPPORT_LINK", "https://modelboxbd.com")
ADMIN_CONTACTS = os.getenv("ADMIN_CONTACTS", "https://t.me/AriyanFix")
