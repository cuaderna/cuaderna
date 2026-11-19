from collections.abc import Mapping

from pydantic import BaseModel, JsonValue, TypeAdapter

_JSON_OBJECT = TypeAdapter[dict[str, JsonValue]](dict[str, JsonValue])


class _AnthropicCacheCreation(BaseModel):
    ephemeral_5m_input_tokens: int = 0
    ephemeral_1h_input_tokens: int = 0


class _AnthropicServerToolUse(BaseModel):
    web_search_requests: int = 0


class _AnthropicUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation: _AnthropicCacheCreation | None = None
    server_tool_use: _AnthropicServerToolUse | None = None
    service_tier: str | None = None
    speed: str | None = None
    inference_geo: str | None = None


class _AnthropicResponse(BaseModel):
    id: str | None = None
    model: str | None = None
    stop_reason: str | None = None
    usage: _AnthropicUsage | None = None


def from_anthropic(response: Mapping[str, object], *, requested_model: str) -> dict[str, JsonValue]:
    parsed = _AnthropicResponse.model_validate(response)
    facts: dict[str, JsonValue] = {
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": requested_model,
    }
    if parsed.model is not None:
        facts["gen_ai.response.model"] = parsed.model
    if parsed.id is not None:
        facts["gen_ai.response.id"] = parsed.id
    if parsed.stop_reason is not None:
        facts["gen_ai.response.finish_reasons"] = [parsed.stop_reason]
    usage = parsed.usage
    if usage is None:
        return facts
    facts["provider.usage"] = _JSON_OBJECT.validate_python(response["usage"])
    read_from_cache = usage.cache_read_input_tokens or 0
    written_to_cache = usage.cache_creation_input_tokens or 0
    facts["gen_ai.usage.input_tokens"] = usage.input_tokens + read_from_cache + written_to_cache
    facts["gen_ai.usage.output_tokens"] = usage.output_tokens
    facts["gen_ai.usage.cache_read.input_tokens"] = read_from_cache
    facts["gen_ai.usage.cache_write.input_tokens"] = written_to_cache
    if written_to_cache > 0:
        facts["cuaderna.usage.cache_write_by_ttl"] = _anthropic_cache_writes(
            usage.cache_creation, written_to_cache
        )
    if usage.server_tool_use is not None and usage.server_tool_use.web_search_requests > 0:
        facts["cuaderna.usage.requests"] = {"web_search": usage.server_tool_use.web_search_requests}
    conditions: dict[str, JsonValue] = {}
    if usage.service_tier is not None:
        conditions["service_tier"] = usage.service_tier
    if usage.speed is not None:
        conditions["speed"] = usage.speed
    if usage.inference_geo is not None:
        conditions["inference_geo"] = usage.inference_geo
    if conditions:
        facts["cuaderna.usage.conditions"] = conditions
    return facts


def _anthropic_cache_writes(
    by_lifetime: _AnthropicCacheCreation | None, written_to_cache: int
) -> dict[str, JsonValue]:
    if by_lifetime is None:
        return {"5m": written_to_cache}
    writes: dict[str, JsonValue] = {}
    if by_lifetime.ephemeral_5m_input_tokens > 0:
        writes["5m"] = by_lifetime.ephemeral_5m_input_tokens
    if by_lifetime.ephemeral_1h_input_tokens > 0:
        writes["1h"] = by_lifetime.ephemeral_1h_input_tokens
    return writes


def from_ollama(response: Mapping[str, object], *, requested_model: str) -> dict[str, JsonValue]:
    reply = _JSON_OBJECT.validate_python(
        {name: value for name, value in response.items() if name != "message"}
    )
    facts: dict[str, JsonValue] = {
        "gen_ai.provider.name": "ollama",
        "gen_ai.request.model": requested_model,
        "gen_ai.response.model": reply["model"],
        "provider.usage": reply,
    }
    if "done_reason" in reply:
        facts["gen_ai.response.finish_reasons"] = [reply["done_reason"]]
    if "prompt_eval_count" in reply:
        facts["gen_ai.usage.input_tokens"] = reply["prompt_eval_count"]
    if "prompt_eval_cached_count" in reply:
        facts["gen_ai.usage.cache_read.input_tokens"] = reply["prompt_eval_cached_count"]
    if "eval_count" in reply:
        facts["gen_ai.usage.output_tokens"] = reply["eval_count"]
    return facts
