from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, JsonValue, TypeAdapter

_JSON_OBJECT = TypeAdapter[dict[str, JsonValue]](dict[str, JsonValue])
_AMOUNT = TypeAdapter[Decimal](Annotated[Decimal, Field(ge=0)])


def reported_cost(usd: Decimal | str, reason: str) -> dict[str, JsonValue]:
    amount = _AMOUNT.validate_python(usd)
    return {"cuaderna.cost": {"usd": format(amount, "f"), "basis": "reported", "reason": reason}}


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


class _OpenAICacheWriteDetails(BaseModel):
    ttl_30m_tokens: int = 0
    ttl_12h_tokens: int = 0


class _OpenAIInputDetails(BaseModel):
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_write_tokens_details: _OpenAICacheWriteDetails | None = None


class _OpenAIOutputDetails(BaseModel):
    reasoning_tokens: int | None = None


class _OpenAIResponsesUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    input_tokens_details: _OpenAIInputDetails | None = None
    output_tokens_details: _OpenAIOutputDetails | None = None


class _OpenAIIncompleteDetails(BaseModel):
    reason: str | None = None


class _OpenAIWebSearchUsage(BaseModel):
    num_requests: int = 0


class _OpenAIToolUsage(BaseModel):
    web_search: _OpenAIWebSearchUsage | None = None


class _OpenAIResponsesResponse(BaseModel):
    id: str | None = None
    model: str | None = None
    status: str | None = None
    incomplete_details: _OpenAIIncompleteDetails | None = None
    service_tier: str | None = None
    tool_usage: _OpenAIToolUsage | None = None
    usage: _OpenAIResponsesUsage | None = None


class _OpenAIChatUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    prompt_tokens_details: _OpenAIInputDetails | None = None
    completion_tokens_details: _OpenAIOutputDetails | None = None


class _OpenAIChatChoice(BaseModel):
    finish_reason: str | None = None


class _OpenAIChatResponse(BaseModel):
    id: str | None = None
    model: str | None = None
    choices: list[_OpenAIChatChoice] = []
    service_tier: str | None = None
    usage: _OpenAIChatUsage | None = None


def from_openai_responses(
    response: Mapping[str, object], *, requested_model: str
) -> dict[str, JsonValue]:
    parsed = _OpenAIResponsesResponse.model_validate(response)
    facts: dict[str, JsonValue] = {
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": requested_model,
    }
    if parsed.model is not None:
        facts["gen_ai.response.model"] = parsed.model
    if parsed.id is not None:
        facts["gen_ai.response.id"] = parsed.id
    finish_reason = parsed.status
    if parsed.incomplete_details is not None and parsed.incomplete_details.reason is not None:
        finish_reason = parsed.incomplete_details.reason
    if finish_reason is not None:
        facts["gen_ai.response.finish_reasons"] = [finish_reason]
    if parsed.service_tier is not None:
        facts["cuaderna.usage.conditions"] = {"service_tier": parsed.service_tier}
    web_searches = 0
    if parsed.tool_usage is not None and parsed.tool_usage.web_search is not None:
        web_searches = parsed.tool_usage.web_search.num_requests
    if web_searches > 0:
        facts["cuaderna.usage.requests"] = {"web_search": web_searches}
    usage = parsed.usage
    if usage is None:
        return facts
    facts["provider.usage"] = _JSON_OBJECT.validate_python(response["usage"])
    facts.update(
        _openai_token_facts(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            input_details=usage.input_tokens_details,
            output_details=usage.output_tokens_details,
        )
    )
    return facts


def from_openai_chat(
    response: Mapping[str, object], *, requested_model: str, provider: str = "openai"
) -> dict[str, JsonValue]:
    parsed = _OpenAIChatResponse.model_validate(response)
    facts: dict[str, JsonValue] = {
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": requested_model,
    }
    if parsed.model is not None:
        facts["gen_ai.response.model"] = parsed.model
    if parsed.id is not None:
        facts["gen_ai.response.id"] = parsed.id
    finish_reasons: list[JsonValue] = [
        choice.finish_reason for choice in parsed.choices if choice.finish_reason is not None
    ]
    if finish_reasons:
        facts["gen_ai.response.finish_reasons"] = finish_reasons
    if parsed.service_tier is not None:
        facts["cuaderna.usage.conditions"] = {"service_tier": parsed.service_tier}
    usage = parsed.usage
    if usage is None:
        return facts
    facts["provider.usage"] = _JSON_OBJECT.validate_python(response["usage"])
    facts.update(
        _openai_token_facts(
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            input_details=usage.prompt_tokens_details,
            output_details=usage.completion_tokens_details,
        )
    )
    return facts


def _openai_token_facts(
    *,
    input_tokens: int,
    output_tokens: int,
    input_details: _OpenAIInputDetails | None,
    output_details: _OpenAIOutputDetails | None,
) -> dict[str, JsonValue]:
    read_from_cache = 0
    written_to_cache = 0
    writes_by_lifetime: _OpenAICacheWriteDetails | None = None
    if input_details is not None:
        read_from_cache = input_details.cached_tokens or 0
        written_to_cache = input_details.cache_write_tokens or 0
        writes_by_lifetime = input_details.cache_write_tokens_details
    facts: dict[str, JsonValue] = {
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "gen_ai.usage.cache_read.input_tokens": read_from_cache,
        "gen_ai.usage.cache_write.input_tokens": written_to_cache,
    }
    if written_to_cache > 0:
        facts["cuaderna.usage.cache_write_by_ttl"] = _openai_cache_writes(
            writes_by_lifetime, written_to_cache
        )
    if output_details is not None and output_details.reasoning_tokens is not None:
        facts["gen_ai.usage.reasoning.output_tokens"] = output_details.reasoning_tokens
    return facts


def _openai_cache_writes(
    by_lifetime: _OpenAICacheWriteDetails | None, written_to_cache: int
) -> dict[str, JsonValue]:
    if by_lifetime is None:
        return {"30m": written_to_cache}
    writes: dict[str, JsonValue] = {}
    if by_lifetime.ttl_30m_tokens > 0:
        writes["30m"] = by_lifetime.ttl_30m_tokens
    if by_lifetime.ttl_12h_tokens > 0:
        writes["12h"] = by_lifetime.ttl_12h_tokens
    return writes
