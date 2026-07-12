import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opencode.session_delivery import SessionDeliveryObserver


class _Bot:
    def __init__(self) -> None:
        self.send_message = AsyncMock()


class _Client:
    def __init__(self) -> None:
        self.messages = []
        self.children = []
        self.list_messages = AsyncMock(side_effect=self._list_messages)
        self.list_session_children = AsyncMock(side_effect=self._list_session_children)

    async def _list_messages(self, session_id: str):
        return list(self.messages)

    async def _list_session_children(self, session_id: str):
        return list(self.children)


class SessionDeliveryObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_late_parent_assistant_message_once(self) -> None:
        # Given: a parent session has already returned once and a new assistant
        # message appears later after a background subagent notification.
        bot = _Bot()
        client = _Client()
        observer = SessionDeliveryObserver(
            bot=bot,
            client=client,
            max_message_length=4000,
        )
        sent_ids = {"msg_before"}
        observer.register_session(
            user_id=42,
            chat_id=1001,
            session_id="ses_parent",
            reply_to_message_id=77,
            sent_message_ids=sent_ids,
        )
        client.messages = [
            {
                "info": {"id": "msg_before", "role": "assistant"},
                "parts": [{"type": "text", "text": "old"}],
            },
            {
                "info": {"id": "msg_after", "role": "assistant"},
                "parts": [{"type": "text", "text": "late resume"}],
            },
        ]

        # When: the persistent observer checks the parent session twice.
        await observer.deliver_pending_for_session("ses_parent")
        await observer.deliver_pending_for_session("ses_parent")

        # Then: the late resumed response is pushed exactly once and recorded
        # in the same sent-ID set used by the prompt-scoped path.
        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], 1001)
        self.assertEqual(kwargs["reply_to_message_id"], 77)
        self.assertIn("late resume", kwargs["text"])
        self.assertIn("msg_after", sent_ids)

    async def test_seeded_existing_messages_are_not_sent(self) -> None:
        # Given: the watcher is seeded with message IDs captured before a prompt.
        bot = _Bot()
        client = _Client()
        observer = SessionDeliveryObserver(bot=bot, client=client, max_message_length=4000)
        sent_ids = {"msg_existing"}
        observer.register_session(
            user_id=42,
            chat_id=1001,
            session_id="ses_parent",
            reply_to_message_id=77,
            sent_message_ids=sent_ids,
        )
        client.messages = [
            {
                "info": {"id": "msg_existing", "role": "assistant"},
                "parts": [{"type": "text", "text": "old history"}],
            }
        ]

        # When: the watcher starts after the prompt is submitted.
        await observer.deliver_pending_for_session("ses_parent")

        # Then: existing history is not pushed to Telegram.
        bot.send_message.assert_not_awaited()

    async def test_tracks_child_sessions_without_treating_background_ids_as_sessions(self) -> None:
        # Given: OpenCode exposes child session rows and OmO background IDs are
        # separate bg_* identifiers that must not become watched sessions.
        bot = _Bot()
        client = _Client()
        client.children = [
            {"id": "ses_child", "title": "Explore", "status": "idle"},
            {"id": "bg_123", "title": "Background Result"},
        ]
        observer = SessionDeliveryObserver(bot=bot, client=client, max_message_length=4000)
        observer.register_session(
            user_id=42,
            chat_id=1001,
            session_id="ses_parent",
            reply_to_message_id=None,
            sent_message_ids=set(),
        )

        # When: child sessions are refreshed for the parent.
        children = await observer.refresh_children("ses_parent")
        status = observer.get_status("ses_parent")

        # Then: child `ses_*` sessions are tracked and bg_* result IDs are kept
        # out of the session watch set.
        self.assertEqual([child["id"] for child in children], ["ses_child", "bg_123"])
        self.assertIn("ses_child", status["child_session_ids"])
        self.assertNotIn("bg_123", status["child_session_ids"])


if __name__ == "__main__":
    unittest.main()
