import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from handlers.commands import subagents_command
from opencode.client import SessionStatusMapResult
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
            ),
            get_session_status_map=AsyncMock(
                return_value=SessionStatusMapResult(
                    available=True,
                    statuses={"ses_child": "idle"},
                )
            ),
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
        self.assertIn("Status: <code>idle</code>", text)

    async def test_subagents_reports_unavailable_child_api(self) -> None:
        # Given: the active runtime does not expose OpenCode child sessions.
        message = _Message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=message)
        session_manager = SimpleNamespace(get_active_session=AsyncMock(return_value="ses_parent"))
        opencode_client = SimpleNamespace(
            child_session_listing_available=False,
            list_session_children=AsyncMock(return_value=[]),
            get_session_status_map=AsyncMock(
                return_value=SessionStatusMapResult(available=False, statuses={})
            ),
        )
        context = _Context(session_manager, opencode_client)

        # When: the user asks for subagent status.
        await subagents_command(update, context)

        # Then: the output distinguishes API unavailability from no children.
        message.reply_text.assert_awaited_once()
        text = message.reply_text.await_args.args[0]
        self.assertIn("listing is unavailable", text)

    async def test_subagents_uses_mapped_status_and_escapes_child_values(self) -> None:
        # Given: the child embeds a stale status and contains HTML-significant values.
        message = _Message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=message)
        session_manager = SimpleNamespace(get_active_session=AsyncMock(return_value="ses_parent"))
        call_order = []

        async def list_children(_session_id):
            call_order.append("children")
            return [
                "malformed",
                {"title": "Missing ID"},
                {
                    "id": "ses_<child>",
                    "title": "Explore <code>",
                    "agent": "explore&review",
                    "status": "unknown",
                },
            ]

        async def get_statuses():
            call_order.append("statuses")
            return SessionStatusMapResult(
                available=True,
                statuses={"ses_<child>": "busy"},
            )

        opencode_client = SimpleNamespace(
            list_session_children=AsyncMock(side_effect=list_children),
            get_session_status_map=AsyncMock(side_effect=get_statuses),
        )
        context = _Context(session_manager, opencode_client)

        # When: the user asks for subagent status.
        await subagents_command(update, context)

        # Then: the authoritative map wins, values are escaped, and lookup occurs once after listing.
        text = message.reply_text.await_args.args[0]
        self.assertIn("ses_&lt;child", text)
        self.assertIn("Explore &lt;code&gt;", text)
        self.assertIn("explore&amp;review", text)
        self.assertIn("Status: <code>busy</code>", text)
        self.assertNotIn("unknown", text)
        self.assertNotIn("malformed", text)
        self.assertNotIn("Missing ID", text)
        self.assertEqual(call_order, ["children", "statuses"])
        opencode_client.get_session_status_map.assert_awaited_once_with()

    async def test_subagents_defaults_missing_available_status_to_idle(self) -> None:
        # Given: status lookup succeeds without an entry for the listed child.
        message = _Message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=message)
        session_manager = SimpleNamespace(get_active_session=AsyncMock(return_value="ses_parent"))
        opencode_client = SimpleNamespace(
            list_session_children=AsyncMock(
                return_value=[{"id": "ses_child", "title": "Explore", "status": "unknown"}]
            ),
            get_session_status_map=AsyncMock(
                return_value=SessionStatusMapResult(available=True, statuses={})
            ),
        )
        context = _Context(session_manager, opencode_client)

        # When: the user asks for subagent status.
        await subagents_command(update, context)

        # Then: an absent key in an available map is rendered as idle, never unknown.
        text = message.reply_text.await_args.args[0]
        self.assertIn("Status: <code>idle</code>", text)
        self.assertNotIn("unknown", text)

    async def test_subagents_renders_unavailable_when_status_lookup_is_unavailable(self) -> None:
        # Given: child listing works but the status endpoint is unavailable.
        message = _Message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=message)
        session_manager = SimpleNamespace(get_active_session=AsyncMock(return_value="ses_parent"))
        opencode_client = SimpleNamespace(
            list_session_children=AsyncMock(
                return_value=[{"id": "ses_child", "title": "Explore", "status": "unknown"}]
            ),
            get_session_status_map=AsyncMock(
                return_value=SessionStatusMapResult(available=False, statuses={})
            ),
        )
        context = _Context(session_manager, opencode_client)

        # When: the user asks for subagent status.
        await subagents_command(update, context)

        # Then: the endpoint state is visible without falling back to embedded status.
        text = message.reply_text.await_args.args[0]
        self.assertIn("Status: <code>unavailable</code>", text)
        self.assertNotIn("unknown", text)

    async def test_subagents_renders_unavailable_when_status_lookup_raises(self) -> None:
        # Given: child listing works but status lookup raises unexpectedly.
        message = _Message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=message)
        session_manager = SimpleNamespace(get_active_session=AsyncMock(return_value="ses_parent"))
        opencode_client = SimpleNamespace(
            list_session_children=AsyncMock(return_value=[{"id": "ses_child"}]),
            get_session_status_map=AsyncMock(side_effect=RuntimeError("status failed")),
        )
        context = _Context(session_manager, opencode_client)

        # When: the user asks for subagent status.
        await subagents_command(update, context)

        # Then: the child status is unavailable and the command still replies.
        text = message.reply_text.await_args.args[0]
        self.assertIn("Status: <code>unavailable</code>", text)
        self.assertNotIn("unknown", text)


if __name__ == "__main__":
    unittest.main()
