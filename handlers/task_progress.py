from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any, Literal, Protocol, assert_never


TaskLifecycle = Literal["Running", "Completed", "Failed", "Cancelled"]
TaskStatus = Literal["pending", "running", "completed", "error"]


@dataclass(frozen=True, slots=True)
class TaskProgressEvent:
    parent_session_id: str
    call_id: str
    child_session_id: str
    agent_type: str
    description: str
    lifecycle: TaskLifecycle


class EditableMessage(Protocol):
    async def edit_text(self, text: str, **kwargs: Any) -> Any: ...


class ReplyTarget(Protocol):
    async def reply_text(self, text: str, **kwargs: Any) -> EditableMessage: ...


class BotContext(Protocol):
    bot_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TaskProgressCard:
    message: EditableMessage
    last_text: str


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _lifecycle(
    status: TaskStatus,
    metadata: dict[str, Any],
    output: Any,
    error: Any,
) -> TaskLifecycle:
    match status:
        case "pending" | "running":
            return "Running"
        case "completed":
            return "Completed"
        case "error":
            details = " ".join(value for value in (output, error) if isinstance(value, str)).lower()
            cancelled = (
                bool(metadata.get("interrupted"))
                or bool(metadata.get("aborted"))
                or "abort" in details
                or "cancel" in details
            )
            return "Cancelled" if cancelled else "Failed"
        case unreachable:
            assert_never(unreachable)


def parse_task_progress(
    part: Any,
    parent_session_id: str,
) -> TaskProgressEvent | None:
    if not isinstance(part, dict) or part.get("type") != "tool" or part.get("tool") != "task":
        return None

    call_id = _nonempty_string(part.get("callID"))
    parent_id = _nonempty_string(parent_session_id)
    state = part.get("state")
    if call_id is None or parent_id is None or not isinstance(state, dict):
        return None

    input_data = state.get("input")
    metadata = state.get("metadata")
    status = state.get("status")
    if not isinstance(input_data, dict) or not isinstance(metadata, dict):
        return None
    if status not in ("pending", "running", "completed", "error"):
        return None

    agent_type = _nonempty_string(input_data.get("subagent_type"))
    description = _nonempty_string(input_data.get("description"))
    child_session_id = _nonempty_string(metadata.get("sessionId"))
    if child_session_id is None:
        child_session_id = _nonempty_string(metadata.get("sessionID"))
    if agent_type is None or description is None or child_session_id is None:
        return None

    return TaskProgressEvent(
        parent_session_id=parent_id,
        call_id=call_id,
        child_session_id=child_session_id,
        agent_type=agent_type,
        description=description,
        lifecycle=_lifecycle(status, metadata, state.get("output"), state.get("error")),
    )


def render_task_progress(event: TaskProgressEvent) -> str:
    match event.lifecycle:
        case "Running":
            heading = "🔄 <b>Running</b>"
        case "Completed":
            heading = "✅ <b>Completed</b>"
        case "Failed":
            heading = "❌ <b>Failed</b>"
        case "Cancelled":
            heading = "🚫 <b>Cancelled</b>"
        case unreachable:
            assert_never(unreachable)

    return (
        f"{heading}\n"
        f"<b>Agent:</b> <code>{html.escape(event.agent_type)}</code>\n"
        f"<b>Task:</b> {html.escape(event.description)}"
    )


async def upsert_task_progress_card(
    reply_target: ReplyTarget,
    context: BotContext,
    event: TaskProgressEvent,
    reply_to_message_id: int | None,
) -> None:
    cards = context.bot_data.setdefault("task_progress_cards", {})
    key = f"{event.parent_session_id}:{event.call_id}"
    text = render_task_progress(event)
    retained = cards.get(key)

    if not isinstance(retained, TaskProgressCard):
        message = await reply_target.reply_text(
            text,
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id,
        )
        cards[key] = TaskProgressCard(message=message, last_text=text)
        return

    if retained.last_text == text:
        return

    cards[key] = TaskProgressCard(message=retained.message, last_text=text)
    try:
        await retained.message.edit_text(text, parse_mode="HTML")
    except Exception:  # noqa: BROAD_EXCEPT_OK - Adapter edit errors have no shared dependency-free type.
        return
