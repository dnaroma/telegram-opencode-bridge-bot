"""Single, request-scoped state machine for OpenCode questions."""
import asyncio
import secrets
import time

TTL = 120


def _lock(item):
    lock = item.get("lock")
    if lock is None:
        lock = item["lock"] = asyncio.Lock()
    return lock


def create(bot_data, user_id, chat_id, session_id, question_id, questions):
    store = bot_data.setdefault("question_callbacks", {})
    now = time.monotonic()
    for token, item in list(store.items()):
        if item["expires"] <= now:
            store.pop(token, None)
        elif (item["user_id"], item["chat_id"], item["question_id"]) == (user_id, chat_id, question_id):
            return token, item
    token = secrets.token_urlsafe(12)
    item = {
        "user_id": user_id, "chat_id": chat_id, "session_id": session_id,
        "question_id": question_id, "questions": list(questions), "index": 0,
        "answers": [], "selected": set(), "expires": now + TTL,
        "submitted": False, "in_flight": False, "version": 0,
        "lock": asyncio.Lock(), "custom": None,
    }
    store[token] = item
    return token, item


def get(bot_data, token, user_id, chat_id):
    item = bot_data.get("question_callbacks", {}).get(token)
    if not item or item["expires"] <= time.monotonic() or item["user_id"] != user_id or item["chat_id"] != chat_id:
        if item and item["expires"] <= time.monotonic():
            bot_data["question_callbacks"].pop(token, None)
        return None
    _lock(item)
    return item


def discard(bot_data, token):
    bot_data.get("question_callbacks", {}).pop(token, None)


def submission_failed(item):
    """Release the submission reservation without losing collected answers."""
    if item is not None:
        item["in_flight"] = False
        item["submitted"] = False


def option_label(item, index):
    options = item.get("options", []) if isinstance(item, dict) else []
    if not isinstance(index, int) or index < 0 or index >= len(options):
        return None
    option = options[index]
    return str(option.get("label", "")) if isinstance(option, dict) else str(option)


def parse_callback(data):
    """Parse only callbacks emitted by the question renderer."""
    parts = data.split(":")
    if len(parts) == 5 and parts[0] == "question" and parts[3] == "o":
        try:
            return parts[1], int(parts[2]), "option", int(parts[4])
        except ValueError:
            return None
    if len(parts) == 4 and parts[0] == "question" and parts[3] in {"done", "custom"}:
        try:
            return parts[1], int(parts[2]), parts[3], None
        except ValueError:
            return None
    if len(parts) == 4 and parts[0] == "question" and parts[3] == "retry":
        try:
            return parts[1], int(parts[2]), "retry", None
        except ValueError:
            return None
    return None


async def apply_callback(bot_data, token, user_id, chat_id, action, index=None, custom=None, version=None):
    """Atomically mutate a callback and return (record, submitted, error)."""
    item = get(bot_data, token, user_id, chat_id)
    if item is None:
        return None, False, "expired"
    async with _lock(item):
        if item["submitted"] or item.get("in_flight"):
            return item, False, "submitted"
        if version is not None and version != item.get("version", 0):
            return item, False, "stale"
        if item["index"] >= len(item["questions"]):
            # Completion is immutable; only the explicit, versioned retry may
            # resubmit the preserved answer matrix.
            if action != "retry" or not item.get("answers"):
                return item, False, "invalid"
            item["in_flight"] = True
            return item, True, None
        question = item["questions"][item["index"]]
        multi = bool(question.get("multiple", question.get("multiSelect", False)))
        if action == "option":
            label = option_label(question, index)
            if label is None:
                return item, False, "invalid"
            if multi:
                if label in item["selected"]:
                    item["selected"].remove(label)
                else:
                    item["selected"].add(label)
                return item, False, None
            item["answers"].append([label])
        elif action == "done":
            if not multi:
                return item, False, "invalid"
            # Preserve the option order supplied by OpenCode, not set order.
            item["answers"].append(list(item["selected"]))
            item["selected"].clear()
        elif action == "custom":
            if custom is False:
                return item, False, "custom_disabled"
            if custom is None:
                return item, False, "custom_input"
            item["answers"].append([str(custom)])
        else:
            return item, False, "invalid"
        item["index"] += 1
        item["version"] = item.get("version", 0) + 1
        item["rendered"] = False
        if item["index"] >= len(item["questions"]):
            # Reserve submission, but retain answers until the API confirms it.
            item["in_flight"] = True
            return item, True, None
        return item, False, None


async def render_current_question(update, context, token, item):
    """Render exactly the current request-scoped question and remember its message."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    import html
    questions = item.get("questions", [])
    if item.get("index", 0) >= len(questions):
        return None
    question = questions[item["index"]]
    text = str(question.get("question", ""))
    if not text:
        return None
    header = str(question.get("header", ""))
    multiple = bool(question.get("multiple", question.get("multiSelect", False)))
    custom = question.get("custom", True)
    msg = f"❓ <b>{html.escape(header)}</b>\n\n" if header else "❓ "
    msg += html.escape(text)
    if multiple:
        msg += "\n\n<i>You may select multiple options.</i>"
    msg += "\n\n⚠️ <i>Please answer quickly — the question times out after ~30 seconds.</i>"
    keyboard = []
    for index, option in enumerate(question.get("options", []) if isinstance(question.get("options", []), list) else []):
        label = str(option.get("label", f"Option {index + 1}") if isinstance(option, dict) else option)
        display = label if len(label) <= 50 else label[:47] + "..."
        keyboard.append([InlineKeyboardButton(display, callback_data=f"question:{token}:{item.get('version', 0)}:o:{index}")])
    if multiple:
        keyboard.append([InlineKeyboardButton("✅ Done", callback_data=f"question:{token}:{item.get('version', 0)}:done")])
    if custom:
        keyboard.append([InlineKeyboardButton("✏️ Type custom answer", callback_data=f"question:{token}:{item.get('version', 0)}:custom")])
    sent = await update.message.reply_text(
        msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
    )
    item["telegram_msg_id"] = sent.message_id
    item["rendered"] = True
    return sent


def retry_markup(token, item):
    """Build a scoped retry callback for a failed completed submission."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🔁 Retry submission",
            callback_data=f"question:{token}:{item.get('version', 0)}:retry",
        )
    ]])
