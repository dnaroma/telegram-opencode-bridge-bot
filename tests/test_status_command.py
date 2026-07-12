import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers.commands import status_command
from utils.context_usage import ContextUsageAvailable, ContextUsageUnavailable


class _Message:
    def __init__(self) -> None:
        self.reply_text = AsyncMock()


class StatusCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_fetches_context_usage_when_session_is_active(self) -> None:
        # Given: OpenCode is available and the user has an active session.
        message = _Message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=message)
        session_info = {"session_id": "ses_123456789", "mode": "build", "message_count": 5, "model": "openai/gpt-5"}
        session_manager = SimpleNamespace(
            get_session_info=AsyncMock(return_value=session_info),
            get_effective_model=AsyncMock(return_value="openai/gpt-5"),
        )
        opencode_client = SimpleNamespace(
            is_available=AsyncMock(return_value=True),
            list_messages=AsyncMock(return_value=[{"info": {"role": "assistant"}}]),
            get_available_models=AsyncMock(return_value={"all": []}),
        )
        context = SimpleNamespace(
            bot_data={
                "session_manager": session_manager,
                "opencode_client": opencode_client,
                "config": SimpleNamespace(bot_version="1.2.3"),
            }
        )

        # When: the status command runs.
        with patch(
            "handlers.commands.get_context_usage",
            return_value=ContextUsageAvailable(current_tokens=1200, max_tokens=4000),
        ) as get_context_usage_mock:
            await status_command(update, context)

        # Then: status fetches session messages and provider metadata before rendering.
        opencode_client.list_messages.assert_awaited_once_with("ses_123456789")
        opencode_client.get_available_models.assert_awaited_once_with()
        get_context_usage_mock.assert_called_once_with(
            [{"info": {"role": "assistant"}}],
            {"all": []},
            "openai/gpt-5",
        )
        message.reply_text.assert_awaited_once()
        self.assertIn("Context: <code>1,200 / 4,000</code> (30.0%)", message.reply_text.await_args.args[0])

    async def test_status_degrades_gracefully_when_context_fetch_fails(self) -> None:
        # Given: the status basics work but the extra context metadata lookup fails.
        message = _Message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=message)
        session_info = {"session_id": "ses_123456789", "mode": "build", "message_count": 5, "model": "openai/gpt-5"}
        session_manager = SimpleNamespace(
            get_session_info=AsyncMock(return_value=session_info),
            get_effective_model=AsyncMock(return_value="openai/gpt-5"),
        )
        opencode_client = SimpleNamespace(
            is_available=AsyncMock(return_value=True),
            list_messages=AsyncMock(side_effect=RuntimeError("boom")),
            get_available_models=AsyncMock(return_value={"all": []}),
        )
        context = SimpleNamespace(
            bot_data={
                "session_manager": session_manager,
                "opencode_client": opencode_client,
                "config": SimpleNamespace(bot_version="1.2.3"),
            }
        )

        # When: the status command runs.
        with self.assertLogs("handlers.commands", level="ERROR"):
            await status_command(update, context)

        # Then: the command still replies and simply omits the context line.
        message.reply_text.assert_awaited_once()
        self.assertNotIn("Context:", message.reply_text.await_args.args[0])

    async def test_status_skips_context_fetch_when_server_is_unavailable(self) -> None:
        # Given: OpenCode is offline but session metadata still exists.
        message = _Message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=message)
        session_manager = SimpleNamespace(
            get_session_info=AsyncMock(return_value={"session_id": "ses_123456789"}),
            get_effective_model=AsyncMock(return_value="openai/gpt-5"),
        )
        opencode_client = SimpleNamespace(
            is_available=AsyncMock(return_value=False),
            list_messages=AsyncMock(),
            get_available_models=AsyncMock(),
        )
        context = SimpleNamespace(
            bot_data={
                "session_manager": session_manager,
                "opencode_client": opencode_client,
                "config": SimpleNamespace(bot_version="1.2.3"),
            }
        )

        # When: the status command runs.
        with patch(
            "handlers.commands.get_context_usage",
            return_value=ContextUsageUnavailable(),
        ) as get_context_usage_mock:
            await status_command(update, context)

        # Then: context-specific OpenCode fetches are skipped.
        opencode_client.list_messages.assert_not_awaited()
        opencode_client.get_available_models.assert_not_awaited()
        get_context_usage_mock.assert_not_called()
        message.reply_text.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
