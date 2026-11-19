from typing import Any

import pytest
from pydantic import ValidationError

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


def test_an_anthropic_usage_that_is_not_a_count_is_refused() -> None:
    usage = {"input_tokens": "many", "output_tokens": 40}

    with pytest.raises(ValidationError):
        cuaderna.usage.from_anthropic(anthropic_response(usage), requested_model="claude-haiku-4-5")


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
