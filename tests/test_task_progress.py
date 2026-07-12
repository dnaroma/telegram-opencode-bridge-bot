import sys
import unittest
from types import SimpleNamespace
from typing import Any, TypedDict
from unittest.mock import AsyncMock, Mock, patch

from handlers.messages import _listen_and_stream_events
from handlers.task_progress import (
    parse_task_progress,
    render_task_progress,
    upsert_task_progress_card,
)
from tests.test_permission_forwarding import _SSESession


class _TaskPart(TypedDict):
    type: str
    tool: str
    callID: str
    state: dict[str, Any]


def _task_part(
    call_id: str = "call_1",
    status: str = "running",
    *,
    metadata: dict | None = None,
    output: str = "",
    error: str = "",
) -> _TaskPart:
    return {
        "type": "tool",
        "tool": "task",
        "callID": call_id,
        "state": {
            "status": status,
            "input": {
                "subagent_type": "explore",
                "description": "Inspect <unsafe> & report",
            },
            "metadata": metadata or {"sessionId": "ses_child"},
            "output": output,
            "error": error,
        },
    }


def _task_event(part: dict[str, Any]) -> dict[str, Any]:
    return {
        "payload": {
            "type": "message.part.updated",
            "properties": {"sessionID": "ses_parent", "part": part},
        }
    }


class TaskProgressParsingTests(unittest.TestCase):
    def test_parse_task_progress_returns_typed_running_event(self) -> None:
        # Given: a valid OpenCode task tool part.
        part = _task_part()

        # When: the part is parsed at the helper boundary.
        event = parse_task_progress(part, "ses_parent")

        # Then: all identity and display fields are available as typed attributes.
        self.assertIsNotNone(event)
        self.assertEqual(event.parent_session_id, "ses_parent")
        self.assertEqual(event.call_id, "call_1")
        self.assertEqual(event.child_session_id, "ses_child")
        self.assertEqual(event.agent_type, "explore")
        self.assertEqual(event.description, "Inspect <unsafe> & report")
        self.assertEqual(event.lifecycle, "Running")

    def test_parse_task_progress_maps_terminal_lifecycles(self) -> None:
        # Given: terminal task states, including each cancellation signal.
        cases = (
            (_task_part(status="completed"), "Completed"),
            (_task_part(status="error", error="network failed"), "Failed"),
            (_task_part(status="error", metadata={"sessionID": "ses_child", "interrupted": True}), "Cancelled"),
            (_task_part(status="error", metadata={"sessionId": "ses_child", "aborted": True}), "Cancelled"),
            (_task_part(status="error", output="Task was cancelled by operator"), "Cancelled"),
        )

        for part, expected in cases:
            with self.subTest(expected=expected, part=part):
                # When: each terminal part is parsed.
                event = parse_task_progress(part, "ses_parent")

                # Then: its lifecycle reflects the observable task outcome.
                self.assertIsNotNone(event)
                self.assertEqual(event.lifecycle, expected)

    def test_parse_task_progress_rejects_malformed_events(self) -> None:
        # Given: malformed or non-task tool parts.
        valid = _task_part()
        cases = (
            {},
            {**valid, "tool": "bash"},
            {**valid, "callID": ""},
            {**valid, "state": []},
            {**valid, "state": {"status": "running", "input": [], "metadata": {"sessionId": "child"}}},
            {**valid, "state": {"status": "running", "input": {"description": "work"}, "metadata": {"sessionId": "child"}}},
            {**valid, "state": {"status": "running", "input": {"subagent_type": "explore"}, "metadata": {"sessionId": "child"}}},
            {**valid, "state": {"status": "running", "input": {"subagent_type": "explore", "description": "work"}, "metadata": {}}},
        )

        for part in cases:
            with self.subTest(part=part):
                # When/Then: invalid input is suppressed at the boundary.
                self.assertIsNone(parse_task_progress(part, "ses_parent"))

    def test_render_task_progress_escapes_html_and_shows_state_emoji(self) -> None:
        # Given: a valid task whose model-controlled values contain HTML.
        event = parse_task_progress(_task_part(), "ses_parent")
        self.assertIsNotNone(event)

        # When: the Telegram card is rendered.
        text = render_task_progress(event)

        # Then: content is escaped and the running state remains visible.
        self.assertIn("🔄 <b>Running</b>", text)
        self.assertIn("<code>explore</code>", text)
        self.assertIn("Inspect &lt;unsafe&gt; &amp; report", text)
        self.assertNotIn("<unsafe>", text)

    def test_render_task_progress_shows_each_terminal_state(self) -> None:
        # Given: completed, failed, and cancelled task events.
        cases = (
            (_task_part(status="completed"), "✅ <b>Completed</b>"),
            (_task_part(status="error", error="network failed"), "❌ <b>Failed</b>"),
            (_task_part(status="error", error="operator aborted task"), "🚫 <b>Cancelled</b>"),
        )

        for part, expected in cases:
            with self.subTest(expected=expected):
                event = parse_task_progress(part, "ses_parent")
                self.assertIsNotNone(event)

                # When: the terminal card is rendered.
                text = render_task_progress(event)

                # Then: its distinct state emoji and label are visible.
                self.assertIn(expected, text)


class TaskProgressCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_upserts_non_streaming_task_lifecycles(self) -> None:
        # Given: finite SSE covers duplicates and three independent terminal outcomes.
        parts = (
            _task_part("call_completed", "pending"),
            _task_part("call_completed", "running"),
            _task_part("call_completed", "running"),
            _task_part("call_failed", "running"),
            _task_part("call_cancelled", "pending"),
            _task_part("call_completed", "completed"),
            _task_part("call_failed", "error", error="network failed"),
            _task_part(
                "call_cancelled",
                "error",
                metadata={"sessionId": "ses_child", "interrupted": True},
            ),
        )
        messages = tuple(SimpleNamespace(edit_text=AsyncMock()) for _ in range(3))
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1001),
            message=SimpleNamespace(reply_text=AsyncMock(side_effect=messages)),
        )
        context = SimpleNamespace(
            bot_data={"config": SimpleNamespace(max_message_length=4000)},
            user_data={},
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )

        class _AiohttpModule:
            ClientTimeout = Mock(return_value=SimpleNamespace(total=None))
            ClientSession = Mock(return_value=_SSESession([_task_event(part) for part in parts]))

        # When: the actual shared listener consumes the non-streaming task events.
        with patch.dict(sys.modules, {"aiohttp": _AiohttpModule}):
            await _listen_and_stream_events(
                update=update,
                context=context,
                session_id="ses_parent",
                server_url="http://127.0.0.1:4096",
                is_streaming=False,
                reply_to_message_id=44,
            )

        # Then: each call owns one card and receives only its changed terminal edit.
        self.assertEqual(update.message.reply_text.await_count, 3)
        for sent in update.message.reply_text.await_args_list:
            self.assertIn("🔄 <b>Running</b>", sent.args[0])
            self.assertEqual(
                sent.kwargs,
                {"parse_mode": "HTML", "reply_to_message_id": 44},
            )
        for message, terminal in zip(
            messages,
            ("✅ <b>Completed</b>", "❌ <b>Failed</b>", "🚫 <b>Cancelled</b>"),
            strict=True,
        ):
            message.edit_text.assert_awaited_once()
            self.assertIn(terminal, message.edit_text.await_args.args[0])
        self.assertEqual(
            set(context.bot_data["task_progress_cards"]),
            {
                "ses_parent:call_completed",
                "ses_parent:call_failed",
                "ses_parent:call_cancelled",
            },
        )

    async def test_listener_skips_task_cards_for_streaming_and_malformed_parts(self) -> None:
        # Given: a malformed task and a valid running-to-completed streaming task.
        malformed = _task_part("call_malformed")
        malformed["state"]["metadata"] = {}
        parts = (malformed, _task_part("call_streaming"), _task_part("call_streaming", "completed"))
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1001),
            message=SimpleNamespace(
                reply_text=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock()))
            ),
        )
        context = SimpleNamespace(
            bot_data={"config": SimpleNamespace(max_message_length=4000)},
            user_data={},
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )

        class _AiohttpModule:
            ClientTimeout = Mock(return_value=SimpleNamespace(total=None))
            ClientSession = Mock(return_value=_SSESession([_task_event(part) for part in parts]))

        # When: the actual shared listener consumes the events in streaming mode.
        with patch.dict(sys.modules, {"aiohttp": _AiohttpModule}):
            await _listen_and_stream_events(
                update=update,
                context=context,
                session_id="ses_parent",
                server_url="http://127.0.0.1:4096",
                is_streaming=True,
                reply_to_message_id=44,
            )

        # Then: existing streaming output succeeds without creating task cards.
        self.assertGreater(update.message.reply_text.await_count, 0)
        self.assertNotIn("task_progress_cards", context.bot_data)

    async def test_upsert_creates_each_call_once_and_edits_only_changed_text(self) -> None:
        # Given: two independent calls and retained Telegram messages.
        first_message = SimpleNamespace(edit_text=AsyncMock())
        second_message = SimpleNamespace(edit_text=AsyncMock())
        reply_target = SimpleNamespace(
            reply_text=AsyncMock(side_effect=(first_message, second_message))
        )
        context = SimpleNamespace(bot_data={})
        first_running = parse_task_progress(_task_part("call_1"), "ses_parent")
        second_running = parse_task_progress(_task_part("call_2"), "ses_parent")
        first_completed = parse_task_progress(
            _task_part("call_1", status="completed"), "ses_parent"
        )
        self.assertIsNotNone(first_running)
        self.assertIsNotNone(second_running)
        self.assertIsNotNone(first_completed)

        # When: running repeats, a second call starts, and the first completes.
        await upsert_task_progress_card(reply_target, context, first_running, 44)
        await upsert_task_progress_card(reply_target, context, first_running, 44)
        await upsert_task_progress_card(reply_target, context, second_running, 44)
        await upsert_task_progress_card(reply_target, context, first_completed, 44)

        # Then: two cards are sent, the duplicate is ignored, and only call 1 is edited.
        self.assertEqual(reply_target.reply_text.await_count, 2)
        first_send = reply_target.reply_text.await_args_list[0]
        self.assertIn("🔄 <b>Running</b>", first_send.args[0])
        self.assertEqual(first_send.kwargs, {"parse_mode": "HTML", "reply_to_message_id": 44})
        first_message.edit_text.assert_awaited_once()
        self.assertIn("✅ <b>Completed</b>", first_message.edit_text.await_args.args[0])
        self.assertEqual(first_message.edit_text.await_args.kwargs, {"parse_mode": "HTML"})
        second_message.edit_text.assert_not_awaited()
        self.assertEqual(
            set(context.bot_data["task_progress_cards"]),
            {"ses_parent:call_1", "ses_parent:call_2"},
        )

    async def test_upsert_suppresses_malformed_event_without_creating_card(self) -> None:
        # Given: malformed task input and an empty context.
        reply_target = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(bot_data={})
        event = parse_task_progress({"tool": "task", "callID": "call_1"}, "ses_parent")

        # When: the caller suppresses the unparsable event.
        if event is not None:
            await upsert_task_progress_card(reply_target, context, event, 44)

        # Then: Telegram and card storage remain untouched.
        reply_target.reply_text.assert_not_awaited()
        self.assertNotIn("task_progress_cards", context.bot_data)

    async def test_upsert_retains_changed_text_when_telegram_edit_fails(self) -> None:
        # Given: a retained Telegram card whose edit raises a transport error.
        message = SimpleNamespace(edit_text=AsyncMock(side_effect=RuntimeError("edit failed")))
        reply_target = SimpleNamespace(reply_text=AsyncMock(return_value=message))
        context = SimpleNamespace(bot_data={})
        running = parse_task_progress(_task_part(status="running"), "ses_parent")
        failed = parse_task_progress(_task_part(status="error", error="boom"), "ses_parent")
        self.assertIsNotNone(running)
        self.assertIsNotNone(failed)
        await upsert_task_progress_card(reply_target, context, running, None)

        # When: the failed lifecycle attempts to update the card.
        await upsert_task_progress_card(reply_target, context, failed, None)

        # Then: the error does not escape and the rendered failed text is retained.
        message.edit_text.assert_awaited_once()
        card = context.bot_data["task_progress_cards"]["ses_parent:call_1"]
        self.assertIn("❌ <b>Failed</b>", card.last_text)
