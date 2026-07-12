import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from handlers.commands import callback_handler
from handlers.messages import handle_message
from handlers.question_state import create


class _Chat:
    def __init__(self) -> None:
        self.send_action = AsyncMock()


class _Message:
    def __init__(self, text: str) -> None:
        self.text = text
        self.message_id = 77
        self.chat = _Chat()
        self.reply_text = AsyncMock()


class _Context:
    def __init__(self, session_manager: Mock) -> None:
        self.user_data = {}
        self.bot_data = {
            "config": SimpleNamespace(
                opencode_model="test-model",
                opencode_server_url="http://127.0.0.1:8080",
                opencode_work_dir="/tmp/opencode-work",
            ),
            "opencode_client": Mock(),
            "session_manager": session_manager,
        }
        session_manager.get_user_work_dir = AsyncMock(return_value="/tmp/opencode-work")


class HandleMessageFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_final_answer_failure_retry_then_success_cleans_state(self) -> None:
        message = _Message("custom answer")
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=1001),
            message=message,
        )
        session_manager = Mock()
        context = _Context(session_manager)
        token, pending = create(
            context.bot_data, 42, 1001, "session", "question",
            [{"question": "Explain", "options": [], "custom": True}],
        )
        context.user_data["awaiting_question_answer"] = {
            "token": token, "version": pending["version"],
        }
        client = context.bot_data["opencode_client"]
        client.respond_to_question = AsyncMock(side_effect=[False, True])

        await handle_message(update, context)
        await asyncio.sleep(0)

        retry_markup = message.reply_text.await_args.kwargs["reply_markup"]
        retry_data = retry_markup.inline_keyboard[0][0].callback_data
        self.assertEqual(pending["version"], 1)
        self.assertEqual(
            context.user_data["awaiting_question_answer"]["version"], 1,
        )

        query_message = SimpleNamespace(text="question", chat_id=1001)
        query = SimpleNamespace(
            data=retry_data,
            message=query_message,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        callback_update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42),
        )
        context.bot = SimpleNamespace(edit_message_text=AsyncMock())

        await callback_handler(callback_update, context)

        self.assertEqual(client.respond_to_question.await_count, 2)
        self.assertNotIn(token, context.bot_data["question_callbacks"])
        self.assertNotIn("awaiting_question_answer", context.user_data)

    async def test_setup_failure_does_not_escape_when_server_start_crashes(self) -> None:
        # Given: OpenCode server startup raises the engine wrapper error before session lookup.
        message = _Message("please help")
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_message=message,
            effective_chat=SimpleNamespace(id=1001),
            message=message,
        )
        session_manager = Mock()
        context = _Context(session_manager)

        # When: the Telegram handler receives a normal message.
        failing_start = AsyncMock(
            side_effect=RuntimeError(
                "EngineCore encountered an issue. See stack trace (above) for the root cause."
            )
        )
        with patch("handlers.messages.ensure_server_running", new=failing_start):
            with self.assertLogs("handlers.messages", level="ERROR"):
                await handle_message(update, context)
                await asyncio.sleep(0)

        # Then: the handler contains the failure and replies instead of re-raising.
        message.reply_text.assert_awaited_once()
        reply_text = message.reply_text.await_args.args[0]
        self.assertIn("EngineCore encountered an issue", reply_text)

    async def test_setup_failure_does_not_escape_when_session_lookup_crashes(self) -> None:
        # Given: OpenCode setup succeeded, then session lookup raises the engine wrapper error.
        message = _Message("please help")
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_message=message,
            effective_chat=SimpleNamespace(id=1001),
            message=message,
        )
        session_manager = Mock()
        session_manager.get_active_session = AsyncMock(
            side_effect=RuntimeError(
                "EngineCore encountered an issue. See stack trace (above) for the root cause."
            )
        )
        context = _Context(session_manager)

        # When: the Telegram handler receives a normal message.
        with patch("handlers.messages.ensure_server_running", new=AsyncMock(return_value=True)):
            with self.assertLogs("handlers.messages", level="ERROR"):
                await handle_message(update, context)
                await asyncio.sleep(0)

        # Then: the handler contains the failure and replies instead of re-raising.
        message.reply_text.assert_awaited_once()
        reply_text = message.reply_text.await_args.args[0]
        self.assertIn("EngineCore encountered an issue", reply_text)


if __name__ == "__main__":
    unittest.main()
