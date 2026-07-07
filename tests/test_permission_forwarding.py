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
        self.assertEqual(next(iter(pending.values())), {"session_id": "ses_abc", "permission_id": "perm_123"})

    async def test_permission_callback_allow_submits_once_response(self) -> None:
        # Given: an allow callback points at a pending OpenCode permission request.
        oc_client = SimpleNamespace(respond_to_permission=AsyncMock(return_value=True))
        query = SimpleNamespace(
            data="perm:allow:key12345",
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
        self.assertIn("Approved", query.edit_message_text.await_args.kwargs["text"])

    async def test_permission_callback_deny_submits_reject_response(self) -> None:
        # Given: a deny callback points at a pending OpenCode permission request.
        oc_client = SimpleNamespace(respond_to_permission=AsyncMock(return_value=True))
        query = SimpleNamespace(
            data="perm:deny:key12345",
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


if __name__ == "__main__":
    unittest.main()
