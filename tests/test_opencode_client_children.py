import unittest
from unittest.mock import AsyncMock

from opencode.client import OpenCodeAPIError, OpenCodeClient


class OpenCodeClientChildrenTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_session_children_accepts_common_response_shapes(self) -> None:
        # Given: OpenCode versions can return child sessions as a raw list or
        # under common wrapper keys.
        client = OpenCodeClient()
        shapes = [
            [{"id": "ses_child"}],
            {"children": [{"id": "ses_child"}]},
            {"sessions": [{"id": "ses_child"}]},
            {"data": [{"id": "ses_child"}]},
        ]

        for shape in shapes:
            with self.subTest(shape=shape):
                client._request = AsyncMock(return_value=shape)

                # When: child sessions are listed.
                children = await client.list_session_children("ses_parent")

                # Then: the response is normalized to a list of dicts.
                self.assertEqual(children, [{"id": "ses_child"}])

        await client.close()

    async def test_list_session_children_treats_unsupported_endpoint_as_unavailable(self) -> None:
        # Given: a local OpenCode build does not expose the child-session route.
        client = OpenCodeClient()
        client._request = AsyncMock(side_effect=OpenCodeAPIError(404, "not found"))

        # When: child sessions are listed.
        children = await client.list_session_children("ses_parent")

        # Then: callers get an empty list rather than a crashing command.
        self.assertEqual(children, [])
        await client.close()


if __name__ == "__main__":
    unittest.main()
