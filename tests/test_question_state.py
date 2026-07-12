import asyncio
import unittest

from handlers.question_state import apply_callback, create, parse_callback, submission_failed


class QuestionRetryTests(unittest.TestCase):
    def test_failed_final_submission_can_retry_preserved_matrix(self):
        async def scenario():
            data = {}
            token, item = create(data, 7, 8, "session", "question", [
                {"question": "Pick", "options": [{"label": "yes"}]},
            ])
            current, submitted, error = await apply_callback(
                data, token, 7, 8, "option", index=0, version=0
            )
            self.assertTrue(submitted)
            self.assertEqual(current["answers"], [["yes"]])
            submission_failed(current)

            # The old option callback cannot mutate or resubmit completion.
            stale, sent, error = await apply_callback(
                data, token, 7, 8, "option", index=0, version=0
            )
            self.assertFalse(sent)
            self.assertEqual(error, "stale")
            self.assertEqual(stale["answers"], [["yes"]])

            retry, sent, error = await apply_callback(
                data, token, 7, 8, "retry", version=1
            )
            self.assertTrue(sent)
            self.assertIsNone(error)
            self.assertEqual(retry["answers"], [["yes"]])

        asyncio.run(scenario())

    def test_retry_callback_is_versioned(self):
        self.assertEqual(parse_callback("question:abc:4:retry"), ("abc", 4, "retry", None))


if __name__ == "__main__":
    unittest.main()
