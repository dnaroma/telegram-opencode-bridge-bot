import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import Config
from handlers.messages import _keep_typing
from opencode.client import OpenCodeClient


class TimeoutBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def test_response_timeout_zero_disables_timeout(self) -> None:
        # Given: the runtime environment requests disabled response timeout.
        with patch.dict(os.environ, {"RESPONSE_TIMEOUT": "0"}):
            # When: config is parsed.
            parsed = Config()

        # Then: zero is preserved as the disabled-timeout sentinel.
        self.assertEqual(parsed.response_timeout, 0)

    def test_response_timeout_uses_default_when_env_is_negative(self) -> None:
        # Given: the timeout is configured with a non-positive value.
        with patch.dict(os.environ, {"RESPONSE_TIMEOUT": "-1"}):
            # When: config is parsed.
            parsed = Config()

        # Then: the value is clamped to the bounded default.
        self.assertEqual(parsed.response_timeout, 300)

    def test_response_timeout_preserves_positive_env_value(self) -> None:
        # Given: the timeout is explicitly set to a positive value.
        with patch.dict(os.environ, {"RESPONSE_TIMEOUT": "7"}):
            # When: config is parsed.
            parsed = Config()

        # Then: the custom timeout remains intact.
        self.assertEqual(parsed.response_timeout, 7)

    def test_opencode_client_disables_aiohttp_total_timeout_when_timeout_is_zero(self) -> None:
        # Given: a direct client construction requests disabled timeout.
        client = OpenCodeClient(timeout=0)

        # When/Then: aiohttp receives None for no total timeout.
        self.assertIsNone(client.timeout.total)

    def test_opencode_client_uses_default_when_timeout_is_negative(self) -> None:
        # Given: a direct client construction passes a negative timeout.
        client = OpenCodeClient(timeout=-1)

        # When/Then: aiohttp receives a finite total timeout.
        self.assertEqual(client.timeout.total, 300)

    def test_opencode_client_preserves_positive_timeout(self) -> None:
        # Given: a direct client construction passes a positive timeout.
        client = OpenCodeClient(timeout=7)

        # When/Then: aiohttp receives that exact total timeout.
        self.assertEqual(client.timeout.total, 7)

    async def test_keep_typing_runs_until_cancelled_when_timeout_is_zero(self) -> None:
        # Given: Telegram typing is started with disabled timeout.
        update = SimpleNamespace(
            message=SimpleNamespace(
                chat=SimpleNamespace(send_action=AsyncMock()),
            )
        )
        sleep_calls = 0

        async def cancel_after_many_sleeps(_seconds: int) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 80:
                raise asyncio.CancelledError

        # When: the task is cancelled after many heartbeat cycles.
        with patch("handlers.messages.asyncio.sleep", new=AsyncMock(side_effect=cancel_after_many_sleeps)):
            await _keep_typing(update, 0)

        # Then: it did not stop at the 300-second default bound.
        self.assertGreater(update.message.chat.send_action.await_count, 75)


if __name__ == "__main__":
    unittest.main()
