import json

import pytest

from free_llm_proxy.anthropic_translate import (
    AnthropicError,
    anthropic_to_openai_request,
    estimate_input_tokens,
    map_stop_reason,
    openai_to_anthropic_response,
)

# --- request: Anthropic -> OpenAI ------------------------------------------ #


def test_system_string_becomes_system_message():
    out = anthropic_to_openai_request(
        {
            "model": "claude-x",
            "max_tokens": 10,
            "system": "be brief",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
    )
    assert out["messages"][0] == {"role": "system", "content": "be brief"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}
    assert out["max_tokens"] == 10
    # model and stream are the proxy's / endpoint's concern, not carried over
    assert "model" not in out
    assert "stream" not in out


def test_system_array_text_blocks_joined():
    out = anthropic_to_openai_request(
        {
            "max_tokens": 10,
            "system": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert out["messages"][0]["content"] == "a\nb"


def test_missing_max_tokens_raises():
    with pytest.raises(AnthropicError) as e:
        anthropic_to_openai_request({"messages": [{"role": "user", "content": "hi"}]})
    assert e.value.status_code == 400
    assert e.value.error_type == "invalid_request_error"


def test_empty_messages_raises():
    with pytest.raises(AnthropicError):
        anthropic_to_openai_request({"max_tokens": 1, "messages": []})


def test_image_block_raises():
    with pytest.raises(AnthropicError) as e:
        anthropic_to_openai_request(
            {
                "max_tokens": 1,
                "messages": [{"role": "user", "content": [{"type": "image", "source": {}}]}],
            }
        )
    assert e.value.status_code == 400


def test_tool_use_becomes_tool_calls():
    out = anthropic_to_openai_request(
        {
            "max_tokens": 1,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "let me check"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "get_weather",
                            "input": {"city": "SF"},
                        },
                    ],
                }
            ],
        }
    )
    msg = out["messages"][0]
    assert msg["role"] == "assistant"
    assert msg["content"] == "let me check"
    call = msg["tool_calls"][0]
    assert call["id"] == "toolu_1"
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "SF"}


def test_tool_result_becomes_tool_message_before_text():
    out = anthropic_to_openai_request(
        {
            "max_tokens": 1,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "sunny"},
                        {"type": "text", "text": "thanks"},
                    ],
                }
            ],
        }
    )
    assert out["messages"][0] == {"role": "tool", "tool_call_id": "toolu_1", "content": "sunny"}
    assert out["messages"][1] == {"role": "user", "content": "thanks"}


def test_multiple_tool_results_expand_in_order():
    out = anthropic_to_openai_request(
        {
            "max_tokens": 1,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "a", "content": "1"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "b",
                            "content": [{"type": "text", "text": "2"}],
                        },
                    ],
                }
            ],
        }
    )
    assert [m["tool_call_id"] for m in out["messages"]] == ["a", "b"]
    assert out["messages"][1]["content"] == "2"


def test_tools_and_tool_choice_tool_mapping():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    out = anthropic_to_openai_request(
        {
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "f", "description": "d", "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": "f"},
        }
    )
    assert out["tools"][0] == {
        "type": "function",
        "function": {"name": "f", "description": "d", "parameters": schema},
    }
    assert out["tool_choice"] == {"type": "function", "function": {"name": "f"}}


@pytest.mark.parametrize(
    "anthropic_tc, openai_tc",
    [({"type": "auto"}, "auto"), ({"type": "any"}, "required"), ({"type": "none"}, "none")],
)
def test_tool_choice_variants(anthropic_tc, openai_tc):
    out = anthropic_to_openai_request(
        {
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "f", "input_schema": {}}],
            "tool_choice": anthropic_tc,
        }
    )
    assert out["tool_choice"] == openai_tc


def test_disable_parallel_tool_use():
    out = anthropic_to_openai_request(
        {
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "f", "input_schema": {}}],
            "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
        }
    )
    assert out["parallel_tool_calls"] is False


def test_stop_sequences_temperature_and_topk_drop():
    out = anthropic_to_openai_request(
        {
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
            "stop_sequences": ["X"],
            "top_k": 5,
            "temperature": 0.5,
            "top_p": 0.9,
            "metadata": {"user_id": "u"},
        }
    )
    assert out["stop"] == ["X"]
    assert out["temperature"] == 0.5
    assert out["top_p"] == 0.9
    assert "top_k" not in out
    assert "metadata" not in out


# --- response: OpenAI -> Anthropic ----------------------------------------- #


def _resp(content=None, tool_calls=None, finish="stop", usage=None):
    msg = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-x",
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 7, "completion_tokens": 3},
    }


def test_response_text():
    out = openai_to_anthropic_response(_resp(content="hello"), "m/x:free")
    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["model"] == "m/x:free"
    assert out["content"] == [{"type": "text", "text": "hello"}]
    assert out["stop_reason"] == "end_turn"
    assert out["usage"] == {"input_tokens": 7, "output_tokens": 3}
    assert out["id"].startswith("msg_")


def test_response_tool_use():
    out = openai_to_anthropic_response(
        _resp(
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "f", "arguments": '{"x":1}'},
                }
            ],
            finish="tool_calls",
        ),
        "m",
    )
    assert out["stop_reason"] == "tool_use"
    assert out["content"][0] == {"type": "tool_use", "id": "call_1", "name": "f", "input": {"x": 1}}


def test_response_tool_use_bad_json_falls_back_to_empty():
    out = openai_to_anthropic_response(
        _resp(
            tool_calls=[
                {"id": "c", "type": "function", "function": {"name": "f", "arguments": "{bad"}}
            ],
            finish="tool_calls",
        ),
        "m",
    )
    assert out["content"][0]["input"] == {}


def test_response_text_then_tool_use_block_order():
    out = openai_to_anthropic_response(
        _resp(
            content="thinking",
            tool_calls=[
                {"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
            finish="tool_calls",
        ),
        "m",
    )
    assert [b["type"] for b in out["content"]] == ["text", "tool_use"]


@pytest.mark.parametrize(
    "finish_reason, stop_reason",
    [
        ("stop", "end_turn"),
        ("length", "max_tokens"),
        ("tool_calls", "tool_use"),
        ("content_filter", "end_turn"),
        (None, "end_turn"),
        ("weird", "end_turn"),
    ],
)
def test_map_stop_reason(finish_reason, stop_reason):
    assert map_stop_reason(finish_reason) == stop_reason


# --- count_tokens estimate ------------------------------------------------- #


def test_estimate_tokens_empty():
    assert estimate_input_tokens({"messages": []}) == 0


def test_estimate_tokens_positive():
    n = estimate_input_tokens(
        {"system": "abcd", "messages": [{"role": "user", "content": "abcd"}]}, divisor=4
    )
    assert n >= 1


def test_estimate_divisor_effect():
    body = {"messages": [{"role": "user", "content": "a" * 100}]}
    assert estimate_input_tokens(body, divisor=4) > estimate_input_tokens(body, divisor=10)
