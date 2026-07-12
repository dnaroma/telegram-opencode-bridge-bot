from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextUsageAvailable:
    current_tokens: int
    max_tokens: int

    @property
    def percentage(self) -> float:
        return (self.current_tokens / self.max_tokens) * 100


@dataclass(frozen=True, slots=True)
class ContextUsageUnavailable:
    pass


ContextUsage = ContextUsageAvailable | ContextUsageUnavailable


def get_context_usage(
    messages: list[dict],
    providers_payload: dict,
    fallback_model: str | None = None,
) -> ContextUsage:
    assistant_message = _get_latest_assistant_message(messages)
    if assistant_message is None:
        return ContextUsageUnavailable()

    current_tokens = _get_current_tokens(assistant_message)
    if current_tokens is None:
        return ContextUsageUnavailable()

    provider_and_model = _get_provider_and_model(assistant_message)
    if provider_and_model is None:
        provider_and_model = _parse_provider_model_string(fallback_model)
    if provider_and_model is None:
        return ContextUsageUnavailable()
    provider_id, model_id = provider_and_model

    context_limit = _get_context_limit(providers_payload, provider_id, model_id)
    if context_limit is None:
        return ContextUsageUnavailable()

    return ContextUsageAvailable(current_tokens=current_tokens, max_tokens=context_limit)


def _get_latest_assistant_message(messages: list[dict]) -> dict | None:
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        info = message.get("info", {})
        if isinstance(info, dict) and info.get("role") == "assistant":
            return message
        if message.get("role") == "assistant":
            return message
    return None


def _get_current_tokens(message: dict) -> int | None:
    info = message.get("info", {})
    if isinstance(info, dict):
        tokens = info.get("tokens", {})
        if isinstance(tokens, dict):
            input_tokens = tokens.get("input")
            if isinstance(input_tokens, int):
                return input_tokens

    parts = message.get("parts", [])
    if not isinstance(parts, list):
        return None
    for part in reversed(parts):
        if not isinstance(part, dict) or part.get("type") != "step-finish":
            continue
        tokens = part.get("tokens", {})
        if not isinstance(tokens, dict):
            continue
        input_tokens = tokens.get("input")
        if isinstance(input_tokens, int):
            return input_tokens
    return None


def _get_provider_and_model(message: dict) -> tuple[str, str] | None:
    info = message.get("info", {})
    if isinstance(info, dict):
        provider_id = info.get("providerID")
        model_id = info.get("modelID")
        if isinstance(provider_id, str) and provider_id and isinstance(model_id, str) and model_id:
            return provider_id, model_id

    for key in ("model",):
        value = message.get(key)
        parsed = _parse_provider_model_string(value)
        if parsed is not None:
            return parsed
        if isinstance(info, dict):
            parsed = _parse_provider_model_string(info.get(key))
            if parsed is not None:
                return parsed
    return None


def _parse_provider_model_string(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    parts = value.split("/")
    if len(parts) < 2:
        return None
    provider_id = parts[0].strip()
    model_id = parts[1].strip()
    if not provider_id or not model_id:
        return None
    return provider_id, model_id


def _get_context_limit(providers_payload: dict, provider_id: str, model_id: str) -> int | None:
    all_providers = providers_payload.get("all", [])
    if not isinstance(all_providers, list):
        return None

    for provider in all_providers:
        if not isinstance(provider, dict) or provider.get("id") != provider_id:
            continue
        models = provider.get("models", {})
        if not isinstance(models, dict):
            return None
        model = models.get(model_id)
        if not isinstance(model, dict):
            return None
        limit = model.get("limit", {})
        if not isinstance(limit, dict):
            return None
        context_limit = limit.get("context")
        if isinstance(context_limit, int):
            return context_limit
        return None
    return None
