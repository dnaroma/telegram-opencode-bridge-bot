import unittest

from utils.context_usage import ContextUsageAvailable, ContextUsageUnavailable, get_context_usage
from utils.formatting import format_status


class ContextUsageTests(unittest.TestCase):
    def test_returns_available_usage_from_latest_assistant_info_tokens(self) -> None:
        # Given: the latest assistant message includes provider/model IDs and info tokens.
        messages = [
            {"info": {"role": "user"}},
            {
                "info": {
                    "role": "assistant",
                    "providerID": "anthropic",
                    "modelID": "claude-sonnet-4",
                    "tokens": {"input": 43210},
                }
            },
        ]
        providers = {
            "all": [
                {
                    "id": "anthropic",
                    "models": {
                        "claude-sonnet-4": {
                            "limit": {"context": 272000}
                        }
                    },
                }
            ]
        }

        # When: context usage is extracted.
        result = get_context_usage(messages, providers)

        # Then: the latest assistant input tokens and provider context limit are used.
        self.assertEqual(
            result,
            ContextUsageAvailable(current_tokens=43210, max_tokens=272000),
        )

    def test_falls_back_to_step_finish_tokens_and_provider_model_string(self) -> None:
        # Given: the assistant message lacks provider/model IDs and info tokens.
        messages = [
            {
                "info": {
                    "role": "assistant",
                    "model": "openai/gpt-5",
                },
                "parts": [
                    {
                        "type": "step-finish",
                        "tokens": {"input": 1200},
                    }
                ],
            }
        ]
        providers = {
            "all": [
                {
                    "id": "openai",
                    "models": {"gpt-5": {"limit": {"context": 4000}}},
                }
            ]
        }

        # When: context usage is extracted.
        result = get_context_usage(messages, providers)

        # Then: the fallback message fields still produce a valid usage result.
        self.assertEqual(
            result,
            ContextUsageAvailable(current_tokens=1200, max_tokens=4000),
        )

    def test_uses_fallback_model_when_message_lacks_model_ids(self) -> None:
        # Given: usage metadata exists but the assistant message lacks model identity.
        messages = [
            {
                "info": {
                    "role": "assistant",
                    "tokens": {"input": 2048},
                }
            }
        ]
        providers = {
            "all": [
                {
                    "id": "newapi-local",
                    "models": {"gpt-5.5": {"limit": {"context": 128000}}},
                }
            ]
        }

        # When: the selected session model is provided as a fallback.
        result = get_context_usage(messages, providers, "newapi-local/gpt-5.5")

        # Then: the fallback model supplies the provider context limit.
        self.assertEqual(
            result,
            ContextUsageAvailable(current_tokens=2048, max_tokens=128000),
        )

    def test_returns_unavailable_when_token_or_limit_data_is_missing(self) -> None:
        # Given: assistant metadata is present but malformed for usage extraction.
        messages = [{"info": {"role": "assistant", "tokens": {"input": "oops"}}}]
        providers = {"all": []}

        # When: context usage is extracted.
        result = get_context_usage(messages, providers)

        # Then: callers get an unavailable typed result instead of an exception.
        self.assertIsInstance(result, ContextUsageUnavailable)

    def test_format_status_renders_context_line_when_available(self) -> None:
        # Given: status formatting receives a valid current session usage snapshot.
        context_usage = ContextUsageAvailable(current_tokens=43210, max_tokens=272000)

        # When: the status text is rendered.
        text = format_status(
            True,
            {
                "session_id": "ses_123456789",
                "mode": "build",
                "message_count": 8,
                "model": "anthropic/claude-sonnet-4",
            },
            "anthropic/claude-sonnet-4",
            "1.2.3",
            context_usage,
        )

        # Then: the current context window is shown under the current session block.
        self.assertIn("Context: <code>43,210 / 272,000</code> (15.9%)", text)

    def test_format_status_omits_context_line_when_unavailable(self) -> None:
        # Given: usage data could not be resolved.
        context_usage = ContextUsageUnavailable()

        # When: the status text is rendered.
        text = format_status(
            True,
            {
                "session_id": "ses_123456789",
                "mode": "build",
                "message_count": 8,
                "model": "anthropic/claude-sonnet-4",
            },
            "anthropic/claude-sonnet-4",
            "1.2.3",
            context_usage,
        )

        # Then: the status stays stable without a placeholder line.
        self.assertNotIn("Context:", text)


if __name__ == "__main__":
    unittest.main()
