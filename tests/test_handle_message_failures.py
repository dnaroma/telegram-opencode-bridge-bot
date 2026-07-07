import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from handlers.messages import handle_message


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
            ),
            "opencode_client": Mock(),
            "session_manager": session_manager,
        }


class HandleMessageFailureTests(unittest.IsolatedAsyncioTestCase):
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

        # Then: the handler contains the failure and replies instead of re-raising.
        message.reply_text.assert_awaited_once()
        reply_text = message.reply_text.await_args.args[0]
        self.assertIn("EngineCore encountered an issue", reply_text)


if __name__ == "__main__":
    unittest.main()
