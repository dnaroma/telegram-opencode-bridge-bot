from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from utils.formatting import format_opencode_response, split_message

logger = logging.getLogger(__name__)


@dataclass
class SessionDeliveryState:
    chat_id: int
    session_id: str
    reply_to_message_id: int | None
    sent_message_ids: set[str]
    user_id: int | None = None
    watched_session_ids: set[str] = field(default_factory=set)
    child_sessions: list[dict[str, Any]] = field(default_factory=list)
    child_listing_unavailable: bool = False
    last_event_at: float = 0.0
    last_forwarded_at: float = 0.0

    def __post_init__(self) -> None:
        self.watched_session_ids.add(self.session_id)


def _message_id(message: dict[str, Any]) -> str:
    info = message.get("info", {})
    if isinstance(info, dict):
        value = info.get("id", "")
        return str(value) if value else ""
    return ""


def _assistant_text(message: dict[str, Any]) -> str:
    parts = message.get("parts", [])
    if not isinstance(parts, list):
        return ""
    text_parts = [
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "".join(text_parts).strip()


def _assistant_error(message: dict[str, Any]) -> str:
    info = message.get("info", {})
    if not isinstance(info, dict):
        return ""
    error = info.get("error")
    if not isinstance(error, dict):
        return ""
    name = str(error.get("name", ""))
    if name == "MessageAbortedError":
        return ""
    text = str(error.get("message", ""))
    if name and text:
        return f"{name}: {text}"
    return name or text


def _is_assistant_message(message: dict[str, Any]) -> bool:
    info = message.get("info", {})
    return isinstance(info, dict) and info.get("role") == "assistant"


async def refresh_child_sessions(client: Any, state: SessionDeliveryState) -> None:
    try:
        children = await client.list_session_children(state.session_id)
    except AttributeError:
        state.child_listing_unavailable = True
        return
    state.child_sessions = children
    state.child_listing_unavailable = False
    for child in children:
        child_id = child.get("id") if isinstance(child, dict) else ""
        if isinstance(child_id, str) and child_id.startswith("ses_"):
            state.watched_session_ids.add(child_id)


async def deliver_new_messages(
    client: Any,
    bot: Any,
    state: SessionDeliveryState,
    max_message_length: int,
) -> int:
    await refresh_child_sessions(client, state)
    messages = await client.list_messages(state.session_id)
    delivered = 0
    for message in messages:
        if not isinstance(message, dict) or not _is_assistant_message(message):
            continue
        msg_id = _message_id(message)
        if not msg_id or msg_id in state.sent_message_ids:
            continue
        error_text = _assistant_error(message)
        content_text = _assistant_text(message)
        if not error_text and not content_text:
            continue
        state.sent_message_ids.add(msg_id)
        rendered = f"❌ <b>Error:</b> {html.escape(error_text)}" if error_text else format_opencode_response(content_text)
        for chunk in split_message(rendered, max_message_length):
            await bot.send_message(
                chat_id=state.chat_id,
                text=chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_to_message_id=state.reply_to_message_id,
            )
            await asyncio.sleep(0.5)
        delivered += 1
        state.last_forwarded_at = time.time()
    state.last_event_at = time.time()
    return delivered


class SessionDeliveryObserver:
    def __init__(self, bot: Any, client: Any, max_message_length: int) -> None:
        self.bot = bot
        self.client = client
        self.max_message_length = max_message_length
        self._states: dict[str, SessionDeliveryState] = {}

    def register_session(
        self,
        *,
        user_id: int,
        chat_id: int,
        session_id: str,
        reply_to_message_id: int | None,
        sent_message_ids: set[str],
    ) -> SessionDeliveryState:
        state = SessionDeliveryState(
            chat_id=chat_id,
            session_id=session_id,
            reply_to_message_id=reply_to_message_id,
            sent_message_ids=sent_message_ids,
            user_id=user_id,
        )
        self._states[session_id] = state
        return state

    async def deliver_pending_for_session(self, session_id: str) -> int:
        state = self._states[session_id]
        return await deliver_new_messages(self.client, self.bot, state, self.max_message_length)

    async def refresh_children(self, session_id: str) -> list[dict[str, Any]]:
        state = self._states[session_id]
        await refresh_child_sessions(self.client, state)
        return state.child_sessions

    def get_status(self, session_id: str) -> dict[str, Any]:
        state = self._states[session_id]
        return {
            "session_id": state.session_id,
            "child_session_ids": set(state.watched_session_ids) - {state.session_id},
            "last_event_at": state.last_event_at,
            "last_forwarded_at": state.last_forwarded_at,
            "child_listing_unavailable": state.child_listing_unavailable,
        }


async def _watch_session_delivery(
    client: Any,
    bot: Any,
    state: SessionDeliveryState,
    max_message_length: int,
    interval_seconds: float,
) -> None:
    while True:
        try:
            await deliver_new_messages(client, bot, state, max_message_length)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Session delivery watcher failed for %s: %s", state.session_id, exc)
        await asyncio.sleep(interval_seconds)


def register_session_delivery(
    context: Any,
    *,
    user_id: int,
    chat_id: int,
    session_id: str,
    reply_to_message_id: int | None,
    sent_message_ids: set[str],
) -> SessionDeliveryState:
    bot_data = context.bot_data
    states = bot_data.setdefault("session_delivery_states", {})
    tasks = bot_data.setdefault("session_delivery_tasks", {})
    users = bot_data.setdefault("session_delivery_users", {})
    key = f"{chat_id}:{session_id}"

    previous_key = users.get(user_id)
    if previous_key and previous_key != key:
        cancel_session_delivery(context, user_id)

    state = states.get(key)
    if not isinstance(state, SessionDeliveryState):
        state = SessionDeliveryState(
            chat_id=chat_id,
            session_id=session_id,
            reply_to_message_id=reply_to_message_id,
            sent_message_ids=sent_message_ids,
            user_id=user_id,
        )
        states[key] = state
    else:
        state.reply_to_message_id = reply_to_message_id
        state.sent_message_ids = sent_message_ids
        state.user_id = user_id

    users[user_id] = key
    task = tasks.get(key)
    if not task or task.done():
        max_length = bot_data["config"].max_message_length
        client = bot_data["opencode_client"]
        task = asyncio.create_task(
            _watch_session_delivery(client, context.bot, state, max_length, 3.0)
        )
        tasks[key] = task
    return state


def cancel_session_delivery(context: Any, user_id: int) -> None:
    bot_data = context.bot_data
    users = bot_data.setdefault("session_delivery_users", {})
    key = users.pop(user_id, None)
    if not key:
        return
    tasks = bot_data.setdefault("session_delivery_tasks", {})
    task = tasks.pop(key, None)
    if task and not task.done():
        task.cancel()
    bot_data.setdefault("session_delivery_states", {}).pop(key, None)


async def cancel_all_session_deliveries(bot_data: dict[str, Any]) -> None:
    tasks = bot_data.setdefault("session_delivery_tasks", {})
    pending = [task for task in tasks.values() if task and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    tasks.clear()
    bot_data.setdefault("session_delivery_states", {}).clear()
    bot_data.setdefault("session_delivery_users", {}).clear()
