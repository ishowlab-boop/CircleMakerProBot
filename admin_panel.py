import time
from datetime import datetime, timezone
from telebot import types

# ---------- Helpers ----------
def fmt_date(ts):
    if ts is None:
        return "N/A"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%A, %d %b %Y")

def parse_int(text: str) -> int:
    import re
    nums = re.findall(r"\d+", text or "")
    if not nums:
        raise ValueError("No number found")
    return int(nums[0])

# ---------- Keyboards ----------
def admin_menu_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("👥 Users", callback_data="adm:users:0"))
    kb.add(types.InlineKeyboardButton("⭐ Premium Users", callback_data="adm:premium"))
    kb.add(types.InlineKeyboardButton("📣 Broadcast", callback_data="adm:bcast"))
    kb.add(types.InlineKeyboardButton("⬇️ Download DB", callback_data="adm:download"))
    return kb

def users_page_kb(users, offset, total):
    kb = types.InlineKeyboardMarkup()

    for u in users:
        uname = u.get("username") or "unknown"
        label = f"👤 {u['id']} @{uname} | 💳 {u.get('credits',0)}"
        kb.add(types.InlineKeyboardButton(label[:64], callback_data=f"adm:user:{u['id']}:{offset}"))

    nav = []
    if offset > 0:
        nav.append(types.InlineKeyboardButton("⬅ Prev", callback_data=f"adm:users:{max(0, offset-10)}"))
    if offset + 10 < total:
        nav.append(types.InlineKeyboardButton("Next ➡", callback_data=f"adm:users:{offset+10}"))
    if nav:
        kb.row(*nav)

    kb.add(types.InlineKeyboardButton("🏠 Admin Menu", callback_data="adm:menu"))
    return kb

def user_actions_kb(user_id, back_offset):
    kb = types.InlineKeyboardMarkup()

    kb.row(
        types.InlineKeyboardButton("➕ +1", callback_data=f"adm:add:{user_id}:1:{back_offset}"),
        types.InlineKeyboardButton("➕ +5", callback_data=f"adm:add:{user_id}:5:{back_offset}"),
        types.InlineKeyboardButton("➕ +10", callback_data=f"adm:add:{user_id}:10:{back_offset}"),
    )
    kb.row(
        types.InlineKeyboardButton("➖ -1", callback_data=f"adm:rem:{user_id}:1:{back_offset}"),
        types.InlineKeyboardButton("➖ -5", callback_data=f"adm:rem:{user_id}:5:{back_offset}"),
        types.InlineKeyboardButton("➖ -10", callback_data=f"adm:rem:{user_id}:10:{back_offset}"),
    )

    kb.add(types.InlineKeyboardButton("✍ Custom Credit (+50 / -20)", callback_data=f"adm:ccredit:{user_id}:{back_offset}"))

    kb.row(
        types.InlineKeyboardButton("✅ Valid 7d", callback_data=f"adm:valid:{user_id}:7:{back_offset}"),
        types.InlineKeyboardButton("✅ Valid 30d", callback_data=f"adm:valid:{user_id}:30:{back_offset}"),
        types.InlineKeyboardButton("✅ Valid 90d", callback_data=f"adm:valid:{user_id}:90:{back_offset}"),
    )
    kb.add(types.InlineKeyboardButton("✍ Custom Validity (days)", callback_data=f"adm:cvalid:{user_id}:{back_offset}"))
    kb.add(types.InlineKeyboardButton("❌ Remove Validity", callback_data=f"adm:vrem:{user_id}:{back_offset}"))

    kb.row(
        types.InlineKeyboardButton("⬅ Back to Users", callback_data=f"adm:users:{back_offset}"),
        types.InlineKeyboardButton("🏠 Admin Menu", callback_data="adm:menu"),
    )
    return kb

# ---------- Register ----------
def register_admin_panel(bot, db, config):
    steps = {}  # admin_id -> step dict

    def is_admin(uid: int) -> bool:
        return uid in config.ADMIN_IDS

    # /admin command (only admin)
    @bot.message_handler(commands=["admin"])
    def cmd_admin(message):
        db.upsert_user(message.from_user)
        if not is_admin(message.from_user.id):
            return bot.reply_to(message, "⛔ Admin only.")

        total = db.count_users()
        bot.send_message(
            message.chat.id,
            f"⚙️ Admin Panel\n👥 Total Users: {total}",
            reply_markup=admin_menu_kb()
        )

    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm:"))
    def cb(call):
        uid = call.from_user.id
        if not is_admin(uid):
            return bot.answer_callback_query(call.id)

        bot.answer_callback_query(call.id)
        parts = call.data.split(":")
        act = parts[1]

        if act == "menu":
            total = db.count_users()
            return bot.send_message(
                call.message.chat.id,
                f"⚙️ Admin Panel\n👥 Total Users: {total}",
                reply_markup=admin_menu_kb()
            )

        if act == "users":
            offset = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
            total = db.count_users()
            users = db.list_users(offset=offset, limit=10)
            return bot.send_message(
                call.message.chat.id,
                f"👥 Users (showing {offset+1}-{min(offset+10,total)} of {total})",
                reply_markup=users_page_kb(users, offset, total),
            )

        if act == "user":
            target = int(parts[2])
            back_offset = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
            credits, vfrom, exp = db.get_credit(target)
            usage = db.get_usage(target)
            text = (
                f"👤 User: {target}\n"
                f"🎬 Videos made: {usage}\n"
                f"💳 Credits: {credits}\n"
                f"✅ Start: {fmt_date(vfrom)}\n"
                f"⏳ End: {fmt_date(exp)}\n"
            )
            return bot.send_message(call.message.chat.id, text, reply_markup=user_actions_kb(target, back_offset))

        if act in ("add", "rem", "valid"):
            target = int(parts[2])
            amt = int(parts[3])
            back_offset = int(parts[4]) if len(parts) >= 5 and parts[4].isdigit() else 0

            if act == "add":
                db.add_credits(target, amt)
            elif act == "rem":
                db.remove_credits(target, amt)
            else:
                db.set_validity(target, amt)

            credits, vfrom, exp = db.get_credit(target)
            usage = db.get_usage(target)
            text = (
                f"👤 User: {target}\n"
                f"🎬 Videos made: {usage}\n"
                f"💳 Credits: {credits}\n"
                f"✅ Start: {fmt_date(vfrom)}\n"
                f"⏳ End: {fmt_date(exp)}\n"
            )
            return bot.send_message(call.message.chat.id, text, reply_markup=user_actions_kb(target, back_offset))

        if act == "vrem":
            target = int(parts[2])
            back_offset = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
            db.remove_validity(target)

            credits, vfrom, exp = db.get_credit(target)
            usage = db.get_usage(target)
            text = (
                f"👤 User: {target}\n"
                f"🎬 Videos made: {usage}\n"
                f"💳 Credits: {credits}\n"
                f"✅ Start: {fmt_date(vfrom)}\n"
                f"⏳ End: {fmt_date(exp)}\n"
            )
            return bot.send_message(call.message.chat.id, text, reply_markup=user_actions_kb(target, back_offset))

        if act == "ccredit":
            target = int(parts[2])
            back_offset = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
            steps[uid] = {"type": "ccredit", "target": target, "back": back_offset}
            return bot.send_message(call.message.chat.id, "Send amount like: +50 or -20")

        if act == "cvalid":
            target = int(parts[2])
            back_offset = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
            steps[uid] = {"type": "cvalid", "target": target, "back": back_offset}
            return bot.send_message(call.message.chat.id, "Send validity days (example: 30)")

        if act == "premium":
            users = db.list_premium(limit=50)
            if not users:
                return bot.send_message(call.message.chat.id, "No premium users.")
            lines = []
            for u in users:
                lines.append(
                    f"👤 User: {u['id']}\n"
                    f"💳 Credits: {u['credits']}\n"
                    f"✅ Start: {fmt_date(u['vfrom'])}\n"
                    f"⏳ End: {fmt_date(u['exp'])}\n"
                    f"----------------------"
                )
            return bot.send_message(call.message.chat.id, "\n".join(lines))

        if act == "bcast":
            steps[uid] = {"type": "bcast"}
            return bot.send_message(call.message.chat.id, "Send broadcast message:")

        if act == "download":
            try:
                with open(config.DB_PATH, "rb") as f:
                    return bot.send_document(call.message.chat.id, f)
            except Exception:
                return bot.send_message(call.message.chat.id, "DB not found!")

    @bot.message_handler(func=lambda m: m.from_user and m.from_user.id in steps)
    def step_handler(message):
        uid = message.from_user.id
        if not is_admin(uid):
            steps.pop(uid, None)
            return

        step = steps.pop(uid, None)
        if not step:
            return

        try:
            if step["type"] == "ccredit":
                raw = (message.text or "").strip()
                sign = -1 if raw.startswith("-") else 1
                amt = parse_int(raw)
                target = int(step["target"])
                back_offset = int(step["back"])

                if sign == 1:
                    db.add_credits(target, amt)
                else:
                    db.remove_credits(target, amt)

                credits, vfrom, exp = db.get_credit(target)
                usage = db.get_usage(target)
                text = (
                    f"👤 User: {target}\n"
                    f"🎬 Videos made: {usage}\n"
                    f"💳 Credits: {credits}\n"
                    f"✅ Start: {fmt_date(vfrom)}\n"
                    f"⏳ End: {fmt_date(exp)}\n"
                )
                bot.send_message(message.chat.id, text, reply_markup=user_actions_kb(target, back_offset))

            elif step["type"] == "cvalid":
                days = parse_int(message.text)
                target = int(step["target"])
                back_offset = int(step["back"])
                db.set_validity(target, days)

                credits, vfrom, exp = db.get_credit(target)
                usage = db.get_usage(target)
                text = (
                    f"👤 User: {target}\n"
                    f"🎬 Videos made: {usage}\n"
                    f"💳 Credits: {credits}\n"
                    f"✅ Start: {fmt_date(vfrom)}\n"
                    f"⏳ End: {fmt_date(exp)}\n"
                )
                bot.send_message(message.chat.id, text, reply_markup=user_actions_kb(target, back_offset))

            elif step["type"] == "bcast":
                text = message.text or ""
                user_ids = db.list_user_ids()
                bot.send_message(message.chat.id, f"📣 Broadcasting to {len(user_ids)} users...")

                sent = 0
                failed = 0
                for uid2 in user_ids:
                    try:
                        bot.send_message(uid2, text)
                        sent += 1
                        time.sleep(0.05)
                    except Exception:
                        failed += 1
                        time.sleep(0.2)

                bot.send_message(message.chat.id, f"✅ Done.\nSent: {sent}\nFailed: {failed}")

        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Error: {e}")
