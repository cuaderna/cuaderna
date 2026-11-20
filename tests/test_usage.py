from collections.abc import Callable
from typing import Any

import pytest

import cuaderna


def anthropic_response(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "usage": usage,
    }


def test_an_anthropic_call_without_caching_keeps_its_counts() -> None:
    usage = {"input_tokens": 120, "output_tokens": 40}

    facts = cuaderna.usage.from_anthropic(
        anthropic_response(usage), requested_model="claude-haiku-4-5"
    )

    assert facts == {
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": "claude-haiku-4-5",
        "gen_ai.response.model": "claude-haiku-4-5-20251001",
        "gen_ai.response.id": "msg_01",
        "gen_ai.response.finish_reasons": ["end_turn"],
        "provider.usage": usage,
        "gen_ai.usage.input_tokens": 120,
        "gen_ai.usage.output_tokens": 40,
        "gen_ai.usage.cache_read.input_tokens": 0,
        "gen_ai.usage.cache_write.input_tokens": 0,
    }


def test_anthropic_input_tokens_include_what_was_read_and_written_to_the_cache() -> None:
    usage = {
        "input_tokens": 50,
        "output_tokens": 300,
        "cache_read_input_tokens": 10_000,
        "cache_creation_input_tokens": 2_500,
        "cache_creation": {"ephemeral_5m_input_tokens": 500, "ephemeral_1h_input_tokens": 2_000},
    }

    facts = cuaderna.usage.from_anthropic(
        anthropic_response(usage), requested_model="claude-haiku-4-5"
    )

    assert facts["gen_ai.usage.input_tokens"] == 12_550
    assert facts["gen_ai.usage.cache_read.input_tokens"] == 10_000
    assert facts["gen_ai.usage.cache_write.input_tokens"] == 2_500
    assert facts["cuaderna.usage.cache_write_by_ttl"] == {"5m": 500, "1h": 2_000}
    assert facts["provider.usage"] == usage


def test_anthropic_cache_writes_without_a_breakdown_count_as_five_minutes() -> None:
    usage = {"input_tokens": 50, "output_tokens": 300, "cache_creation_input_tokens": 2_500}

    facts = cuaderna.usage.from_anthropic(
        anthropic_response(usage), requested_model="claude-haiku-4-5"
    )

    assert facts["cuaderna.usage.cache_write_by_ttl"] == {"5m": 2_500}


def test_anthropic_facts_that_select_a_price_are_kept() -> None:
    usage = {
        "input_tokens": 50,
        "output_tokens": 300,
        "service_tier": "batch",
        "speed": "standard",
        "inference_geo": "us",
        "server_tool_use": {"web_search_requests": 3, "web_fetch_requests": 1},
    }

    facts = cuaderna.usage.from_anthropic(
        anthropic_response(usage), requested_model="claude-haiku-4-5"
    )

    assert facts["cuaderna.usage.conditions"] == {
        "service_tier": "batch",
        "speed": "standard",
        "inference_geo": "us",
    }
    assert facts["cuaderna.usage.requests"] == {"web_search": 3}


def test_an_anthropic_response_without_usage_has_no_token_counts() -> None:
    response = {"id": "msg_01", "model": "claude-haiku-4-5-20251001"}

    facts = cuaderna.usage.from_anthropic(response, requested_model="claude-haiku-4-5")

    assert facts == {
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": "claude-haiku-4-5",
        "gen_ai.response.model": "claude-haiku-4-5-20251001",
        "gen_ai.response.id": "msg_01",
    }


def test_an_ollama_reply_keeps_everything_but_the_message() -> None:
    reply = {
        "model": "qwen3:8b",
        "message": {"role": "assistant", "content": "hello"},
        "done": True,
        "done_reason": "stop",
        "total_duration": 4_000_000_000,
        "prompt_eval_count": 900,
        "prompt_eval_cached_count": 600,
        "eval_count": 120,
    }

    facts = cuaderna.usage.from_ollama(reply, requested_model="qwen3")

    assert facts == {
        "gen_ai.provider.name": "ollama",
        "gen_ai.request.model": "qwen3",
        "gen_ai.response.model": "qwen3:8b",
        "gen_ai.response.finish_reasons": ["stop"],
        "gen_ai.usage.input_tokens": 900,
        "gen_ai.usage.cache_read.input_tokens": 600,
        "gen_ai.usage.output_tokens": 120,
        "provider.usage": {
            "model": "qwen3:8b",
            "done": True,
            "done_reason": "stop",
            "total_duration": 4_000_000_000,
            "prompt_eval_count": 900,
            "prompt_eval_cached_count": 600,
            "eval_count": 120,
        },
    }


def test_an_ollama_reply_cut_short_has_no_token_counts() -> None:
    reply = {"model": "qwen3:8b", "message": {"role": "assistant", "content": ""}, "done": False}

    facts = cuaderna.usage.from_ollama(reply, requested_model="qwen3")

    assert facts == {
        "gen_ai.provider.name": "ollama",
        "gen_ai.request.model": "qwen3",
        "gen_ai.response.model": "qwen3:8b",
        "provider.usage": {"model": "qwen3:8b", "done": False},
    }


def openai_response(usage: dict[str, Any], **extra: object) -> dict[str, Any]:
    return {
        "id": "resp_01",
        "object": "response",
        "model": "gpt-5.4-mini-2026-03-17",
        "status": "completed",
        "output": [],
        "usage": usage,
        **extra,
    }


def test_openai_cached_and_written_tokens_are_parts_of_the_input() -> None:
    usage = {
        "input_tokens": 12_000,
        "input_tokens_details": {
            "cached_tokens": 9_000,
            "cache_write_tokens": 2_500,
            "cache_write_tokens_details": {"ttl_30m_tokens": 500, "ttl_12h_tokens": 2_000},
        },
        "output_tokens": 800,
        "output_tokens_details": {"reasoning_tokens": 600},
        "total_tokens": 12_800,
    }

    facts = cuaderna.usage.from_openai_responses(
        openai_response(usage), requested_model="gpt-5.4-mini"
    )

    assert facts == {
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": "gpt-5.4-mini",
        "gen_ai.response.model": "gpt-5.4-mini-2026-03-17",
        "gen_ai.response.id": "resp_01",
        "gen_ai.response.finish_reasons": ["completed"],
        "provider.usage": usage,
        "gen_ai.usage.input_tokens": 12_000,
        "gen_ai.usage.output_tokens": 800,
        "gen_ai.usage.cache_read.input_tokens": 9_000,
        "gen_ai.usage.cache_write.input_tokens": 2_500,
        "gen_ai.usage.reasoning.output_tokens": 600,
        "cuaderna.usage.cache_write_by_ttl": {"30m": 500, "12h": 2_000},
    }


def test_openai_cache_writes_without_a_breakdown_count_as_thirty_minutes() -> None:
    usage = {
        "input_tokens": 3_000,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 2_500},
        "output_tokens": 10,
        "output_tokens_details": {"reasoning_tokens": 0},
    }

    facts = cuaderna.usage.from_openai_responses(
        openai_response(usage), requested_model="gpt-5.4-mini"
    )

    assert facts["cuaderna.usage.cache_write_by_ttl"] == {"30m": 2_500}


def test_the_openai_tier_recorded_is_the_one_that_served_the_request() -> None:
    usage = {"input_tokens": 100, "output_tokens": 10}
    response = openai_response(
        usage,
        service_tier="default",
        tool_usage={"web_search": {"num_requests": 2}},
    )

    facts = cuaderna.usage.from_openai_responses(response, requested_model="gpt-5.4-mini")

    assert facts["cuaderna.usage.conditions"] == {"service_tier": "default"}
    assert facts["cuaderna.usage.requests"] == {"web_search": 2}


def test_an_openai_response_cut_short_says_why() -> None:
    usage = {"input_tokens": 100, "output_tokens": 4_096}
    response = openai_response(
        usage, status="incomplete", incomplete_details={"reason": "max_output_tokens"}
    )

    facts = cuaderna.usage.from_openai_responses(response, requested_model="gpt-5.4-mini")

    assert facts["gen_ai.response.finish_reasons"] == ["max_output_tokens"]


def test_an_openai_response_without_usage_has_no_token_counts() -> None:
    response = {"id": "resp_01", "model": "gpt-5.4-mini-2026-03-17", "status": "in_progress"}

    facts = cuaderna.usage.from_openai_responses(response, requested_model="gpt-5.4-mini")

    assert facts == {
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": "gpt-5.4-mini",
        "gen_ai.response.model": "gpt-5.4-mini-2026-03-17",
        "gen_ai.response.id": "resp_01",
        "gen_ai.response.finish_reasons": ["in_progress"],
    }


def chat_completion(usage: dict[str, Any], **extra: object) -> dict[str, Any]:
    return {
        "id": "chatcmpl-01",
        "object": "chat.completion",
        "model": "gpt-5.4-mini-2026-03-17",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
        **extra,
    }


def test_a_chat_completion_counts_cache_and_reasoning_as_parts() -> None:
    usage = {
        "prompt_tokens": 12_000,
        "completion_tokens": 800,
        "total_tokens": 12_800,
        "prompt_tokens_details": {"cached_tokens": 9_000, "cache_write_tokens": 2_500},
        "completion_tokens_details": {"reasoning_tokens": 600},
    }

    facts = cuaderna.usage.from_openai_chat(
        chat_completion(usage, service_tier="flex"), requested_model="gpt-5.4-mini"
    )

    assert facts == {
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": "gpt-5.4-mini",
        "gen_ai.response.model": "gpt-5.4-mini-2026-03-17",
        "gen_ai.response.id": "chatcmpl-01",
        "gen_ai.response.finish_reasons": ["stop"],
        "provider.usage": usage,
        "gen_ai.usage.input_tokens": 12_000,
        "gen_ai.usage.output_tokens": 800,
        "gen_ai.usage.cache_read.input_tokens": 9_000,
        "gen_ai.usage.cache_write.input_tokens": 2_500,
        "gen_ai.usage.reasoning.output_tokens": 600,
        "cuaderna.usage.cache_write_by_ttl": {"30m": 2_500},
        "cuaderna.usage.conditions": {"service_tier": "flex"},
    }


def test_a_chat_completion_from_another_provider_carries_its_name() -> None:
    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 40,
        "prompt_tokens_details": {"cached_tokens": None},
        "cost": 0.00012,
    }

    facts = cuaderna.usage.from_openai_chat(
        chat_completion(usage), requested_model="qwen/qwen3", provider="openrouter"
    )

    assert facts["gen_ai.provider.name"] == "openrouter"
    assert facts["gen_ai.usage.input_tokens"] == 120
    assert facts["gen_ai.usage.cache_read.input_tokens"] == 0
    assert "gen_ai.usage.reasoning.output_tokens" not in facts
    assert facts["provider.usage"] == usage


def test_a_chat_completion_without_usage_has_no_token_counts() -> None:
    response = {"id": "chatcmpl-01", "model": "gpt-5.4-mini-2026-03-17"}

    facts = cuaderna.usage.from_openai_chat(response, requested_model="gpt-5.4-mini")

    assert facts == {
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": "gpt-5.4-mini",
        "gen_ai.response.model": "gpt-5.4-mini-2026-03-17",
        "gen_ai.response.id": "chatcmpl-01",
    }


FromUsage = Callable[..., dict[str, Any]]
BuildResponse = Callable[[Any], dict[str, Any]]
PROVIDERS = [
    (cuaderna.usage.from_anthropic, anthropic_response, ("input_tokens", "output_tokens")),
    (cuaderna.usage.from_openai_responses, openai_response, ("input_tokens", "output_tokens")),
    (cuaderna.usage.from_openai_chat, chat_completion, ("prompt_tokens", "completion_tokens")),
]
UNREADABLE = [None, "many", 1.5, True, -1, 10**12 + 1, 10**5000]


@pytest.mark.parametrize("count", UNREADABLE, ids=lambda count: type(count).__name__)
@pytest.mark.parametrize(("from_usage", "build", "fields"), PROVIDERS)
def test_a_count_that_cannot_be_read_leaves_the_counts_out_without_raising(
    from_usage: FromUsage, build: BuildResponse, fields: tuple[str, str], count: object
) -> None:
    facts = from_usage(build({fields[0]: count, fields[1]: 40}), requested_model="m")

    assert "gen_ai.usage.input_tokens" not in facts
    assert "gen_ai.usage.output_tokens" not in facts
    assert facts["gen_ai.request.model"] == "m"


def test_anthropic_cache_writes_given_only_by_lifetime_are_summed() -> None:
    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation": {"ephemeral_5m_input_tokens": 2_000_000, "ephemeral_1h_input_tokens": 4},
    }

    facts = cuaderna.usage.from_anthropic(anthropic_response(usage), requested_model="m")

    assert facts["gen_ai.usage.cache_write.input_tokens"] == 2_000_004
    assert facts["gen_ai.usage.input_tokens"] == 2_000_014
    assert facts["cuaderna.usage.cache_write_by_ttl"] == {"5m": 2_000_000, "1h": 4}


def test_openai_cache_writes_given_only_by_lifetime_are_summed() -> None:
    details = {"cache_write_tokens_details": {"ttl_30m_tokens": 100}}
    usage = {"input_tokens": 150, "output_tokens": 5, "input_tokens_details": details}

    facts = cuaderna.usage.from_openai_responses(openai_response(usage), requested_model="m")

    assert facts["gen_ai.usage.cache_write.input_tokens"] == 100
    assert facts["cuaderna.usage.cache_write_by_ttl"] == {"30m": 100}


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param({"prompt_eval_count": "many", "eval_count": 10**5000}, id="counts"),
        pytest.param({"model": 5, "eval_count": 3.5}, id="model-and-float"),
        pytest.param({"options": {"seed": object()}}, id="not-json"),
        pytest.param({}, id="empty"),
    ],
)
def test_an_ollama_reply_with_odd_fields_is_recorded_without_raising(reply: dict[str, Any]) -> None:
    facts = cuaderna.usage.from_ollama(reply, requested_model="qwen3")

    assert facts["gen_ai.request.model"] == "qwen3"
    assert "gen_ai.usage.output_tokens" not in facts
