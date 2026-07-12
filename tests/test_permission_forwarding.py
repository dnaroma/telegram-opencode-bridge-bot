import asyncio
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from handlers.commands import callback_handler
from handlers.messages import _listen_and_stream_events


class _SSEContent:
    def __init__(self, events: list[dict]) -> None:
        self._lines = [f"data: {json.dumps(event)}\n".encode("utf-8") for event in events]

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        raise asyncio.CancelledError


class _SSEResponse:
    def __init__(self, events: list[dict]) -> None:
        self.content = _SSEContent(events)

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        return None


class _SSESession:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        return None

    def get(self, _url: str, headers: dict[str, str]):
        return _SSEResponse(self._events)


class PermissionForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_forwards_permission_updated_events_to_telegram(self) -> None:
        # Given: OpenCode emits the SDK permission event shape for a sensitive read.
        event = {
            "payload": {
                "type": "permission.updated",
                "properties": {
                    "id": "perm_123",
                    "sessionID": "ses_abc",
                    "type": "read",
                    "pattern": "/tmp/project/.env",
                    "title": "Read file",
                },
            }
        }
        sent_prompt = SimpleNamespace(message_id=321)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1001),
            message=SimpleNamespace(reply_text=AsyncMock(return_value=sent_prompt)),
        )
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(max_message_length=4000),
                "opencode_client": Mock(),
            },
            user_data={},
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )

        class _AiohttpModule:
            ClientTimeout = Mock(return_value=SimpleNamespace(total=None))
            ClientSession = Mock(return_value=_SSESession([event]))

        # When: the listener receives the event and then is cancelled by the test stream.
        with patch.dict(sys.modules, {"aiohttp": _AiohttpModule}):
            await _listen_and_stream_events(
                update=update,
                context=context,
                session_id="ses_abc",
                server_url="http://127.0.0.1:4096",
                is_streaming=False,
            )

        # Then: Telegram receives an approval prompt and the callback registry is populated.
        update.message.reply_text.assert_awaited_once()
        prompt_text = update.message.reply_text.await_args.args[0]
        self.assertIn("OpenCode Permission Requested", prompt_text)
        self.assertIn("Read file", prompt_text)
        self.assertIn("/tmp/project/.env", prompt_text)
        _AiohttpModule.ClientTimeout.assert_called_once_with(total=None)
        self.assertIs(_AiohttpModule.ClientSession.call_args.kwargs["timeout"].total, None)

        pending = context.bot_data.get("pending_permissions", {})
        self.assertEqual(len(pending), 1)
        self.assertEqual(
            {k: next(iter(pending.values()))[k] for k in ("session_id", "permission_id")},
            {"session_id": "ses_abc", "permission_id": "perm_123"},
        )

    async def test_listener_stores_permission_event_session_for_reply(self) -> None:
        # Given: a global permission event carries the session that owns the request.
        event = {
            "payload": {
                "type": "permission.updated",
                "properties": {
                    "id": "perm_123",
                    "sessionID": "ses_event_owner",
                    "type": "read",
                    "pattern": "/tmp/project/.env",
                    "title": "Read file",
                },
            }
        }
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1001),
            message=SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace(message_id=321))),
        )
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(max_message_length=4000),
                "opencode_client": Mock(),
            },
            user_data={},
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )

        class _AiohttpModule:
            ClientTimeout = Mock(return_value=SimpleNamespace(total=None))
            ClientSession = Mock(return_value=_SSESession([event]))

        # When: the listener receives the event for the current stream.
        with patch.dict(sys.modules, {"aiohttp": _AiohttpModule}):
            await _listen_and_stream_events(
                update=update,
                context=context,
                session_id="ses_event_owner",
                server_url="http://127.0.0.1:4096",
                is_streaming=False,
            )

        # Then: the callback registry routes the reply to the event's session.
        pending = context.bot_data.get("pending_permissions", {})
        self.assertEqual(len(pending), 1)
        self.assertEqual(
            {k: next(iter(pending.values()))[k] for k in ("session_id", "permission_id")},
            {"session_id": "ses_event_owner", "permission_id": "perm_123"},
        )

    async def test_listener_forwards_permission_from_discovered_child_session(self) -> None:
        # Given: the parent session exposes a background child session and the
        # child owns the permission request.
        event = {
            "payload": {
                "type": "permission.updated",
                "properties": {
                    "id": "perm_child",
                    "sessionID": "ses_child",
                    "type": "read",
                    "pattern": "/tmp/project/.env",
                    "title": "Read file",
                },
            }
        }
        sent_prompt = SimpleNamespace(message_id=321)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1001),
            message=SimpleNamespace(reply_text=AsyncMock(return_value=sent_prompt)),
        )
        oc_client = SimpleNamespace(
            list_session_children=AsyncMock(return_value=[{"id": "ses_child"}]),
        )
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(max_message_length=4000),
                "opencode_client": oc_client,
            },
            user_data={},
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )

        class _AiohttpModule:
            ClientTimeout = Mock(return_value=SimpleNamespace(total=None))
            ClientSession = Mock(return_value=_SSESession([event]))

        # When: the child permission event arrives on the global stream.
        with patch.dict(sys.modules, {"aiohttp": _AiohttpModule}):
            await _listen_and_stream_events(
                update=update,
                context=context,
                session_id="ses_parent",
                server_url="http://127.0.0.1:4096",
                is_streaming=False,
            )

        # Then: the child request is forwarded and routed back to the child.
        update.message.reply_text.assert_awaited_once()
        pending = context.bot_data["pending_permissions"]
        self.assertEqual(
            {k: next(iter(pending.values()))[k] for k in ("session_id", "permission_id")},
            {"session_id": "ses_child", "permission_id": "perm_child"},
        )

    async def test_listener_deduplicates_repeated_child_permission_events(self) -> None:
        # Given: OpenCode emits the same child permission update more than once.
        event = {
            "payload": {
                "type": "permission.updated",
                "properties": {
                    "id": "perm_duplicate",
                    "sessionID": "ses_child",
                    "type": "execute",
                    "pattern": "git status",
                    "title": "Run command",
                },
            }
        }
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1001),
            message=SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace(message_id=321))),
        )
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(max_message_length=4000),
                "opencode_client": SimpleNamespace(
                    list_session_children=AsyncMock(return_value=[{"id": "ses_child"}]),
                ),
            },
            user_data={},
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )

        class _AiohttpModule:
            ClientTimeout = Mock(return_value=SimpleNamespace(total=None))
            ClientSession = Mock(return_value=_SSESession([event, event]))

        # When: duplicate permission events are consumed.
        with patch.dict(sys.modules, {"aiohttp": _AiohttpModule}):
            await _listen_and_stream_events(
                update=update,
                context=context,
                session_id="ses_parent",
                server_url="http://127.0.0.1:4096",
                is_streaming=False,
            )

        # Then: Telegram gets one prompt and one callback registry entry.
        update.message.reply_text.assert_awaited_once()
        self.assertEqual(len(context.bot_data["pending_permissions"]), 1)

    async def test_permission_callback_allow_submits_once_response(self) -> None:
        # Given: an allow callback points at a pending OpenCode permission request.
        oc_client = SimpleNamespace(respond_to_permission=AsyncMock(return_value=True))
        query = SimpleNamespace(
            data="perm:once:key12345",
            message=SimpleNamespace(text="approval prompt"),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(opencode_work_dir="/tmp/project"),
                "session_manager": Mock(),
                "opencode_client": oc_client,
                "pending_permissions": {
                    "key12345": {"session_id": "ses_abc", "permission_id": "perm_123"},
                },
            }
        )
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), callback_query=query)

        # When: the user taps Allow.
        await callback_handler(update, context)

        # Then: OpenCode receives a one-time approval and the pending prompt is cleared.
        oc_client.respond_to_permission.assert_awaited_once_with(
            session_id="ses_abc",
            permission_id="perm_123",
            response="once",
            remember=False,
        )
        self.assertEqual(context.bot_data["pending_permissions"], {})
        self.assertIn("Allowed once", query.edit_message_text.await_args.kwargs["text"])

    async def test_permission_callback_deny_submits_reject_response(self) -> None:
        # Given: a deny callback points at a pending OpenCode permission request.
        oc_client = SimpleNamespace(respond_to_permission=AsyncMock(return_value=True))
        query = SimpleNamespace(
            data="perm:reject:key12345",
            message=SimpleNamespace(text="approval prompt"),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(opencode_work_dir="/tmp/project"),
                "session_manager": Mock(),
                "opencode_client": oc_client,
                "pending_permissions": {
                    "key12345": {"session_id": "ses_abc", "permission_id": "perm_123"},
                },
            }
        )
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), callback_query=query)

        # When: the user taps Deny.
        await callback_handler(update, context)

        # Then: OpenCode receives a rejection and the pending prompt is cleared.
        oc_client.respond_to_permission.assert_awaited_once_with(
            session_id="ses_abc",
            permission_id="perm_123",
            response="reject",
            remember=False,
        )
        self.assertEqual(context.bot_data["pending_permissions"], {})
        self.assertIn("Rejected", query.edit_message_text.await_args.kwargs["text"])

    async def test_permission_callback_always_requires_confirmation(self) -> None:
        # Given: an always-allow callback points at a pending permission.
        oc_client = SimpleNamespace(respond_to_permission=AsyncMock(return_value=True))
        query = SimpleNamespace(
            data="perm:always:key12345",
            message=SimpleNamespace(text="approval prompt"),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(opencode_work_dir="/tmp/project"),
                "session_manager": Mock(),
                "opencode_client": oc_client,
                "pending_permissions": {
                    "key12345": {"session_id": "ses_child", "permission_id": "perm_123"},
                },
            }
        )
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), callback_query=query)

        # When: the user taps Allow always.
        await callback_handler(update, context)

        # Then: the bot asks for the second confirmation and does not reply yet.
        oc_client.respond_to_permission.assert_not_awaited()
        query.edit_message_text.assert_awaited_once()
        self.assertIn("Confirm permanent permission", query.edit_message_text.await_args.kwargs["text"])
        self.assertIsNotNone(query.edit_message_text.await_args.kwargs["reply_markup"])

    async def test_permission_callback_always_confirm_submits_always_response(self) -> None:
        # Given: the user has reached the second confirmation step.
        oc_client = SimpleNamespace(respond_to_permission=AsyncMock(return_value=True))
        query = SimpleNamespace(
            data="perm:always_confirm:key12345",
            message=SimpleNamespace(text="approval prompt\n\nconfirm"),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(opencode_work_dir="/tmp/project"),
                "session_manager": Mock(),
                "opencode_client": oc_client,
                "pending_permissions": {
                    "key12345": {"session_id": "ses_child", "permission_id": "perm_123"},
                },
            }
        )
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), callback_query=query)

        # When: the user confirms Allow always.
        await callback_handler(update, context)

        # Then: OpenCode receives the persistent approval.
        oc_client.respond_to_permission.assert_awaited_once_with(
            session_id="ses_child",
            permission_id="perm_123",
            response="always",
            remember=False,
        )
        self.assertEqual(context.bot_data["pending_permissions"], {})
        self.assertIn("Always allowed", query.edit_message_text.await_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
