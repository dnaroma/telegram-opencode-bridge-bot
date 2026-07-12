import unittest
from unittest.mock import AsyncMock

from opencode.client import OpenCodeClient


class OpenCodeClientApprovalReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_respond_to_permission_uses_current_reply_endpoint(self) -> None:
        # Given: a pending permission request from the global event stream.
        client = OpenCodeClient()
        client._request = AsyncMock(return_value={"success": True})

        # When: the bridge approves the request once.
        result = await client.respond_to_permission(
            session_id="ses_abc",
            permission_id="perm_123",
            response="once",
        )

        # Then: the current OpenCode reply route and payload are used.
        self.assertTrue(result)
        client._request.assert_awaited_once_with(
            "POST",
            "/permission/perm_123/reply",
            json_data={"reply": "once"},
        )
        await client.close()

    async def test_respond_to_question_uses_current_session_scoped_reply_endpoint(self) -> None:
        # Given: a pending question request from the global event stream.
        client = OpenCodeClient()
        client._request = AsyncMock(return_value={"data": True})

        # When: the bridge submits the selected answer.
        result = await client.respond_to_question(
            session_id="ses_abc",
            question_id="que_123",
            answers=[["Yes"]],
        )

        # Then: the current OpenCode question reply route is used.
        self.assertTrue(result)
        client._request.assert_awaited_once_with(
            "POST",
            "/api/session/ses_abc/question/que_123/reply",
            json_data={"answers": [["Yes"]]},
        )
        await client.close()


if __name__ == "__main__":
    unittest.main()
