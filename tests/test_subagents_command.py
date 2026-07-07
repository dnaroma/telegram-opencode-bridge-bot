import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from handlers.commands import subagents_command
from opencode.session_delivery import SessionDeliveryState


class _Message:
    def __init__(self) -> None:
        self.reply_text = AsyncMock()
        self.chat = SimpleNamespace(send_action=AsyncMock())


class _Context:
    def __init__(self, session_manager, opencode_client) -> None:
        self.bot_data = {
            "session_manager": session_manager,
            "opencode_client": opencode_client,
        }


class SubagentsCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_subagents_reports_no_active_session(self) -> None:
        # Given: the Telegram user has not started an OpenCode session.
        message = _Message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=message)
        session_manager = SimpleNamespace(get_active_session=AsyncMock(return_value=None))
        context = _Context(session_manager, SimpleNamespace())

        # When: the user asks for subagent status.
        await subagents_command(update, context)

        # Then: the bot returns a clear empty state.
        message.reply_text.assert_awaited_once()
        self.assertIn("No active", message.reply_text.await_args.args[0])

    async def test_subagents_lists_child_sessions_and_observer_status(self) -> None:
        # Given: OpenCode exposes child sessions and the local observer knows one
        # of them is being watched.
        message = _Message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=message)
        session_manager = SimpleNamespace(get_active_session=AsyncMock(return_value="ses_parent"))
        opencode_client = SimpleNamespace(
            list_session_children=AsyncMock(
                return_value=[
                    {
                        "id": "ses_child",
                        "title": "Explore code",
                        "agent": "explore",
                        "status": {"type": "idle"},
                    }
                ]
            )
        )
        delivery_state = SessionDeliveryState(
            chat_id=1001,
            session_id="ses_parent",
            reply_to_message_id=None,
            sent_message_ids=set(),
        )
        delivery_state.watched_session_ids.add("ses_child")
        context = _Context(session_manager, opencode_client)
        context.bot_data["session_delivery_states"] = {"1001:ses_parent": delivery_state}

        # When: the user asks for subagent status.
        await subagents_command(update, context)

        # Then: the child session and local observer state are rendered.
        message.reply_text.assert_awaited_once()
        text = message.reply_text.await_args.args[0]
        self.assertIn("ses_child", text)
        self.assertIn("Explore code", text)
        self.assertIn("explore", text)
        self.assertIn("aware of <code>1</code>", text)

    async def test_subagents_reports_unavailable_child_api(self) -> None:
        # Given: the active runtime does not expose OpenCode child sessions.
        message = _Message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=message)
        session_manager = SimpleNamespace(get_active_session=AsyncMock(return_value="ses_parent"))
        opencode_client = SimpleNamespace(
            child_session_listing_available=False,
            list_session_children=AsyncMock(return_value=[]),
        )
        context = _Context(session_manager, opencode_client)

        # When: the user asks for subagent status.
        await subagents_command(update, context)

        # Then: the output distinguishes API unavailability from no children.
        message.reply_text.assert_awaited_once()
        text = message.reply_text.await_args.args[0]
        self.assertIn("listing is unavailable", text)


if __name__ == "__main__":
    unittest.main()
