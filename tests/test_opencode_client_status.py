import unittest
from unittest.mock import AsyncMock

from opencode.client import OpenCodeAPIError, OpenCodeClient, OpenCodeConnectionError


class OpenCodeClientStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_session_status_map_normalizes_supported_response_shapes(self) -> None:
        # Given: OpenCode can return direct, statuses-wrapped, or data-wrapped maps.
        shapes = [
            ({"ses_busy": "busy"}, {"ses_busy": "busy"}),
            ({"statuses": {"ses_retry": {"type": "retry"}}}, {"ses_retry": "retry"}),
            ({"data": {"ses_idle": "idle"}}, {"ses_idle": "idle"}),
        ]
        client = OpenCodeClient()

        for payload, expected in shapes:
            with self.subTest(payload=payload):
                client._request = AsyncMock(return_value=payload)

                # When: the session status endpoint is queried.
                result = await client.get_session_status_map()

                # Then: supported scalar and object values are normalized by session ID.
                self.assertTrue(result.available)
                self.assertEqual(result.statuses, expected)
                client._request.assert_awaited_once_with("GET", "/session/status")

        await client.close()

    async def test_get_session_status_map_distinguishes_empty_from_unavailable(self) -> None:
        # Given: a successful endpoint can legitimately report no active sessions.
        client = OpenCodeClient()
        client._request = AsyncMock(return_value={})

        # When: the empty status map is queried.
        result = await client.get_session_status_map()

        # Then: the endpoint is available even though the map is empty.
        self.assertTrue(result.available)
        self.assertEqual(result.statuses, {})
        await client.close()

    async def test_get_session_status_map_marks_api_and_transport_errors_unavailable(self) -> None:
        # Given: the optional endpoint can fail at either the API or transport boundary.
        errors = [
            OpenCodeAPIError(404, "not found"),
            OpenCodeAPIError(500, "server error"),
            OpenCodeConnectionError("connection failed"),
        ]
        client = OpenCodeClient()

        for error in errors:
            with self.subTest(error=error):
                client._request = AsyncMock(side_effect=error)

                # When: status lookup cannot reach a usable endpoint.
                result = await client.get_session_status_map()

                # Then: callers receive explicit unavailability, not an invented status.
                self.assertFalse(result.available)
                self.assertEqual(result.statuses, {})

        await client.close()

    async def test_get_session_status_map_ignores_malformed_payload_entries(self) -> None:
        # Given: a successful response contains malformed wrappers or status values.
        payloads = [
            [],
            {"statuses": []},
            {
                "ses_valid": {"type": "busy"},
                "ses_missing_type": {"state": "retry"},
                "ses_invalid_scalar": 42,
                "ses_invalid_type": {"type": "unknown"},
            },
        ]
        client = OpenCodeClient()

        for payload in payloads:
            with self.subTest(payload=payload):
                client._request = AsyncMock(return_value=payload)

                # When: malformed input is normalized.
                result = await client.get_session_status_map()

                # Then: availability is preserved and only valid statuses survive.
                expected = {"ses_valid": "busy"} if isinstance(payload, dict) and "ses_valid" in payload else {}
                self.assertTrue(result.available)
                self.assertEqual(result.statuses, expected)

        await client.close()


if __name__ == "__main__":
    unittest.main()
