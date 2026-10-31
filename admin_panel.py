
import time
import re
from datetime import datetime, timezone
from telebot import types


# =========================
# GLOBAL ADMIN STEPS
# =========================
ADMIN_STEPS = {}


def is_waiting(uid: int) -> bool:
    return uid in ADMIN_STEPS


def fmt_date(ts):
    if ts is None:
        return "N/A"

    return datetime.fromtimestamp(
        int(ts),
        tz=timezone.utc
    ).strftime("%A, %d %b %Y")


def parse_int(text: str) -> int:
    nums = re.findall(r"\d+", text or "")

    if not nums:
        raise ValueError("No number found")

    return int(nums[0])


def parse_positive_int(text: str) -> int:
    raw = (text or "").strip()

    if not raw.isascii() or not raw.isdecimal():
        raise ValueError("Send a positive whole number.")

    value = int(raw)

    if value < 1:
        raise ValueError("Number must be greater than 0.")

    return value


# =========================
# ADMIN MENU
# =========================
def admin_menu_kb():
    kb = types.InlineKeyboardMarkup()

    kb.add(
        types.InlineKeyboardButton(
            "💳 Manage Credits",
            callback_data="adm:credits"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "📅 Manage Validity",
            callback_data="adm:validity"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "👥 Users",
            callback_data="adm:users:0"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "⭐ Premium Users",
            callback_data="adm:premium"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "📣 Broadcast",
            callback_data="adm:bcast"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "⬇️ Download DB",
            callback_data="adm:download"
        )
    )

    return kb


# =========================
# CREDIT ACTIONS
# =========================
def credit_actions_kb(user_id: int):
    kb = types.InlineKeyboardMarkup()

    kb.add(
        types.InlineKeyboardButton(
            "➕ Add Credits",
            callback_data=f"adm:credits:add:{user_id}"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "➖ Remove Credits",
            callback_data=f"adm:credits:remove:{user_id}"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "⬅ Back",
            callback_data="adm:menu"
        )
    )

    return kb


# =========================
# VALIDITY ACTIONS
# =========================
def validity_actions_kb(user_id: int):
    kb = types.InlineKeyboardMarkup()

    kb.add(
        types.InlineKeyboardButton(
            "✅ Set Validity",
            callback_data=f"adm:validity:set:{user_id}"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "❌ Remove Validity",
            callback_data=f"adm:validity:remove:{user_id}"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "⬅ Back",
            callback_data="adm:menu"
        )
    )

    return kb


# =========================
# USERS PAGE KEYBOARD
# =========================
def users_page_kb(users, offset, total):
    kb = types.InlineKeyboardMarkup()

    for user in users:
        user_id = user["id"]
        username = user.get("username") or "unknown"
        credits = user.get("credits") or 0

        label = (
            f"👤 {user_id} @{username} | "
            f"💳 {credits}"
        )

        kb.add(
            types.InlineKeyboardButton(
                label[:64],
                callback_data=f"adm:user:{user_id}:{offset}"
            )
        )

    nav = []

    if offset > 0:
        nav.append(
            types.InlineKeyboardButton(
                "⬅ Prev",
                callback_data=f"adm:users:{max(0, offset - 10)}"
            )
        )

    if offset + 10 < total:
        nav.append(
            types.InlineKeyboardButton(
                "Next ➡",
                callback_data=f"adm:users:{offset + 10}"
            )
        )

    if nav:
        kb.row(*nav)

    kb.add(
        types.InlineKeyboardButton(
            "🏠 Admin Menu",
            callback_data="adm:menu"
        )
    )

    return kb


# =========================
# USER QUICK ACTIONS
# =========================
def user_actions_kb(user_id, back_offset):
    kb = types.InlineKeyboardMarkup()

    kb.row(
        types.InlineKeyboardButton(
            "➕ +1",
            callback_data=f"adm:add:{user_id}:1:{back_offset}"
        ),
        types.InlineKeyboardButton(
            "➕ +5",
            callback_data=f"adm:add:{user_id}:5:{back_offset}"
        ),
        types.InlineKeyboardButton(
            "➕ +10",
            callback_data=f"adm:add:{user_id}:10:{back_offset}"
        )
    )

    kb.row(
        types.InlineKeyboardButton(
            "➖ -1",
            callback_data=f"adm:rem:{user_id}:1:{back_offset}"
        ),
        types.InlineKeyboardButton(
            "➖ -5",
            callback_data=f"adm:rem:{user_id}:5:{back_offset}"
        ),
        types.InlineKeyboardButton(
            "➖ -10",
            callback_data=f"adm:rem:{user_id}:10:{back_offset}"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "✍ Custom Credit (+50 / -20)",
            callback_data=f"adm:ccredit:{user_id}:{back_offset}"
        )
    )

    kb.row(
        types.InlineKeyboardButton(
            "✅ Valid 7d",
            callback_data=f"adm:valid:{user_id}:7:{back_offset}"
        ),
        types.InlineKeyboardButton(
            "✅ Valid 30d",
            callback_data=f"adm:valid:{user_id}:30:{back_offset}"
        ),
        types.InlineKeyboardButton(
            "✅ Valid 90d",
            callback_data=f"adm:valid:{user_id}:90:{back_offset}"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "✍ Custom Validity (days)",
            callback_data=f"adm:cvalid:{user_id}:{back_offset}"
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            "❌ Remove Validity",
            callback_data=f"adm:vrem:{user_id}:{back_offset}"
        )
    )

    kb.row(
        types.InlineKeyboardButton(
            "⬅ Back to Users",
            callback_data=f"adm:users:{back_offset}"
        ),
        types.InlineKeyboardButton(
            "🏠 Admin Menu",
            callback_data="adm:menu"
        )
    )

    return kb


# =========================
# USER DETAILS
# =========================
def send_user_details(bot, db, chat_id, target, offset=0):
    credits, vfrom, exp = db.get_credit(target)
    usage = db.get_usage(target)

    text = (
        f"👤 User: <code>{target}</code>\n"
        f"🎬 Videos made: <b>{usage}</b>\n"
        f"💳 Credits: <b>{credits}</b>\n"
        f"✅ Start: <b>{fmt_date(vfrom)}</b>\n"
        f"⏳ End: <b>{fmt_date(exp)}</b>\n"
    )

    return bot.send_message(
        chat_id,
        text,
        reply_markup=user_actions_kb(target, offset),
        parse_mode="HTML"
    )


# =========================
# SEND ADMIN PANEL
# =========================
def send_admin_panel(bot, db, chat_id: int):
    total = db.count_users()

    return bot.send_message(
        chat_id,
        (
            "⚙️ <b>Admin Panel</b>\n"
            f"👥 Total Users: <b>{total}</b>"
        ),
        reply_markup=admin_menu_kb(),
        parse_mode="HTML"
    )


# =========================
# REGISTER ADMIN PANEL
# =========================
def register_admin_panel(bot, db, config):
    steps = ADMIN_STEPS

    def is_admin(uid: int) -> bool:
        return uid in config.ADMIN_IDS

    # =========================
    # ADMIN COMMAND
    # =========================
    @bot.message_handler(commands=["admin"])
    def cmd_admin(message):
        db.upsert_user(message.from_user)

        if not is_admin(message.from_user.id):
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
    # ADMIN CALLBACK HANDLER
    # =========================
    @bot.callback_query_handler(
        func=lambda c: c.data and c.data.startswith("adm:")
    )
    def cb(call):
        uid = call.from_user.id

        if not is_admin(uid):
            return bot.answer_callback_query(call.id)

        bot.answer_callback_query(call.id)

        parts = call.data.split(":")
        act = parts[1]
        chat_id = call.message.chat.id

        # ADMIN MENU
        if act == "menu":
            return send_admin_panel(bot, db, chat_id)

        # =========================
        # MANAGE CREDITS
        # =========================
        if act == "credits" and len(parts) == 2:
            steps[uid] = {
                "type": "credits_pick_user"
            }

            return bot.send_message(
                chat_id,
                "👤 Send User ID for credits:"
            )

        if (
            act == "credits"
            and len(parts) == 4
            and parts[2] in ("add", "remove")
        ):
            target = parse_positive_int(parts[3])
            action = parts[2]

            steps[uid] = {
                "type": f"credits_{action}_amount",
                "target": target
            }

            return bot.send_message(
                chat_id,
                f"💳 Send amount to {action.upper()} "
                f"for User ID {target}:"
            )

        # =========================
        # MANAGE VALIDITY
        # =========================
        if act == "validity" and len(parts) == 2:
            steps[uid] = {
                "type": "validity_pick_user"
            }

            return bot.send_message(
                chat_id,
                "👤 Send User ID for validity:"
            )

        if (
            act == "validity"
            and len(parts) == 4
            and parts[2] in ("set", "remove")
        ):
            target = parse_positive_int(parts[3])

            if parts[2] == "remove":
                db.remove_validity(target)

                return bot.send_message(
                    chat_id,
                    f"✅ Validity removed for {target}",
                    reply_markup=validity_actions_kb(target)
                )

            steps[uid] = {
                "type": "validity_days",
                "target": target
            }

            return bot.send_message(
                chat_id,
                f"📅 Send validity days for {target}:"
            )

        # =========================
        # USERS LIST
        # =========================
        if act == "users":
            offset = (
                int(parts[2])
                if len(parts) >= 3 and parts[2].isdigit()
                else 0
            )

            total = db.count_users()
            users = db.list_users(
                offset=offset,
                limit=10
            )

            start = offset + 1 if total > 0 else 0
            end = min(offset + 10, total)

            return bot.send_message(
                chat_id,
                f"👥 Users (showing {start}-{end} of {total})",
                reply_markup=users_page_kb(
                    users,
                    offset,
                    total
                )
            )

        # =========================
        # USER DETAILS
        # =========================
        if act == "user":
            target = int(parts[2])

            back_offset = (
                int(parts[3])
                if len(parts) >= 4 and parts[3].isdigit()
                else 0
            )

            return send_user_details(
                bot,
                db,
                chat_id,
                target,
                back_offset
            )

        # =========================
        # QUICK CREDIT / VALIDITY
        # =========================
        if act in ("add", "rem", "valid"):
            target = int(parts[2])
            amount = int(parts[3])

            back_offset = (
                int(parts[4])
                if len(parts) >= 5 and parts[4].isdigit()
                else 0
            )

            if act == "add":
                db.add_credits(target, amount)

            elif act == "rem":
                db.remove_credits(target, amount)

            else:
                db.set_validity(target, amount)

            return send_user_details(
                bot,
                db,
                chat_id,
                target,
                back_offset
            )

        # =========================
        # REMOVE VALIDITY
        # =========================
        if act == "vrem":
            target = int(parts[2])

            back_offset = (
                int(parts[3])
                if len(parts) >= 4 and parts[3].isdigit()
                else 0
            )

            db.remove_validity(target)

            return send_user_details(
                bot,
                db,
                chat_id,
                target,
                back_offset
            )

        # =========================
        # CUSTOM CREDITS
        # =========================
        if act == "ccredit":
            target = int(parts[2])
            back_offset = int(parts[3])

            steps[uid] = {
                "type": "ccredit",
                "target": target,
                "back": back_offset
            }

            return bot.send_message(
                chat_id,
                "Send amount like: +50 or -20"
            )

        # =========================
        # CUSTOM VALIDITY
        # =========================
        if act == "cvalid":
            target = int(parts[2])
            back_offset = int(parts[3])

            steps[uid] = {
                "type": "cvalid",
                "target": target,
                "back": back_offset
            }

            return bot.send_message(
                chat_id,
                "Send validity days (example: 30)"
            )

        # =========================
        # PREMIUM USERS
        # =========================
        if act == "premium":
            users = db.list_premium(limit=50)

            if not users:
                return bot.send_message(
                    chat_id,
                    "No premium users."
                )

            lines = []

            for user in users:
                lines.append(
                    f"👤 User: {user['id']}\n"
                    f"💳 Credits: {user['credits']}\n"
                    f"✅ Start: {fmt_date(user['vfrom'])}\n"
                    f"⏳ End: {fmt_date(user['exp'])}\n"
                    "----------------------"
                )

            output = "\n".join(lines)

            for i in range(0, len(output), 3500):
                bot.send_message(
                    chat_id,
                    output[i:i + 3500]
                )

            return

        # =========================
        # BROADCAST
        # =========================
        if act == "bcast":
            steps[uid] = {
                "type": "bcast"
            }

            return bot.send_message(
                chat_id,
                "📣 Send broadcast message:"
            )

        # =========================
        # DOWNLOAD DATABASE
        # =========================
        if act == "download":
            try:
                with open(config.DB_PATH, "rb") as f:
                    return bot.send_document(
                        chat_id,
                        f
                    )

            except Exception:
                return bot.send_message(
                    chat_id,
                    "DB not found!"
                )

    # =========================
    # ADMIN TEXT STEP HANDLER
    # =========================
    @bot.message_handler(
        func=lambda m: (
            m.from_user is not None
            and m.from_user.id in steps
        ),
        content_types=["text"]
    )
    def step_handler(message):
        uid = message.from_user.id

        if not is_admin(uid):
            steps.pop(uid, None)
            return

        step = steps.pop(uid, None)

        if not step:
            return

        chat_id = message.chat.id
        action = step.get("type")

        try:
            # =====================
            # PICK USER FOR CREDITS
            # =====================
            if action == "credits_pick_user":
                target = parse_positive_int(
                    message.text
                )

                credits, _, _ = db.get_credit(target)

                return bot.send_message(
                    chat_id,
                    (
                        f"👤 User: {target}\n"
                        f"💳 Current Credits: {credits}\n\n"
                        "Choose credits action:"
                    ),
                    reply_markup=credit_actions_kb(target)
                )

            # =====================
            # ADD / REMOVE CREDITS
            # =====================
            elif action in (
                "credits_add_amount",
                "credits_remove_amount"
            ):
                amount = parse_positive_int(
                    message.text
                )

                target = int(step["target"])

                if action == "credits_add_amount":
                    db.add_credits(
                        target,
                        amount
                    )

                    result = (
                        f"✅ Added {amount} credits "
                        f"to {target}"
                    )

                else:
                    db.remove_credits(
                        target,
                        amount
                    )

                    result = (
                        f"✅ Removed {amount} credits "
                        f"from {target}"
                    )

                credits, _, _ = db.get_credit(target)

                return bot.send_message(
                    chat_id,
                    (
                        f"{result}\n"
                        f"💳 Current Credits: {credits}"
                    ),
                    reply_markup=credit_actions_kb(target)
                )

            # =====================
            # PICK USER FOR VALIDITY
            # =====================
            elif action == "validity_pick_user":
                target = parse_positive_int(
                    message.text
                )

                _, vfrom, exp = db.get_credit(target)

                return bot.send_message(
                    chat_id,
                    (
                        f"👤 User: {target}\n"
                        f"✅ Start: {fmt_date(vfrom)}\n"
                        f"⏳ End: {fmt_date(exp)}\n\n"
                        "Choose validity action:"
                    ),
                    reply_markup=validity_actions_kb(target)
                )

            # =====================
            # SET VALIDITY
            # =====================
            elif action == "validity_days":
                days = parse_positive_int(
                    message.text
                )

                target = int(step["target"])

                db.set_validity(
                    target,
                    days
                )

                _, vfrom, exp = db.get_credit(target)

                return bot.send_message(
                    chat_id,
                    (
                        f"✅ Validity set: {days} days "
                        f"for {target}\n"
                        f"✅ Start: {fmt_date(vfrom)}\n"
                        f"⏳ End: {fmt_date(exp)}"
                    ),
                    reply_markup=validity_actions_kb(target)
                )

            # =====================
            # CUSTOM CREDIT
            # =====================
            elif action == "ccredit":
                raw = (message.text or "").strip()

                if not re.fullmatch(r"[+-]?[0-9]+", raw):
                    raise ValueError(
                        "Send +50 or -20"
                    )

                amount = abs(int(raw))

                if amount < 1:
                    raise ValueError(
                        "Amount must be greater than 0."
                    )

                target = int(step["target"])
                back_offset = int(step["back"])

                if raw.startswith("-"):
                    db.remove_credits(
                        target,
                        amount
                    )

                else:
                    db.add_credits(
                        target,
                        amount
                    )

                return send_user_details(
                    bot,
                    db,
                    chat_id,
                    target,
                    back_offset
                )

            # =====================
            # CUSTOM VALIDITY
            # =====================
            elif action == "cvalid":
                days = parse_positive_int(
                    message.text
                )

                target = int(step["target"])
                back_offset = int(step["back"])

                db.set_validity(
                    target,
                    days
                )

                return send_user_details(
                    bot,
                    db,
                    chat_id,
                    target,
                    back_offset
                )

            # =====================
            # BROADCAST SEND
            # =====================
            elif action == "bcast":
                text = message.text or ""
                user_ids = db.list_user_ids()

                bot.send_message(
                    chat_id,
                    (
                        f"📣 Broadcasting to "
                        f"{len(user_ids)} users..."
                    )
                )

                sent = 0
                failed = 0

                for user_id in user_ids:
                    try:
                        bot.send_message(
                            user_id,
                            text
                        )
                        sent += 1
                        time.sleep(0.05)

                    except Exception:
                        failed += 1
                        time.sleep(0.2)

                return bot.send_message(
                    chat_id,
                    (
                        "✅ Broadcast Done.\n"
                        f"✅ Sent: {sent}\n"
                        f"❌ Failed: {failed}"
                    )
                )

        except ValueError as error:
            steps[uid] = step

            return bot.send_message(
                chat_id,
                f"❌ Invalid input: {error}\nTry again."
            )

        except Exception as error:
            return bot.send_message(
                chat_id,
                f"❌ Error: {error}"
            )
