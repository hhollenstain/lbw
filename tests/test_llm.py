from __future__ import annotations

import pytest

from agent_lbw.llm.client import AnthropicClient, ModelRequestError, OpenAICompatClient, client_for


def test_a_blank_key_still_opens_a_local_client() -> None:
    llm = OpenAICompatClient(
        model="qwen-pi-fixed:latest",
        base_url="http://192.168.86.122:11434/v1",
        api_key="",
    )
    assert llm.client.api_key == "ollama"

    kept = OpenAICompatClient(
        model="qwen-pi-fixed:latest",
        base_url="http://192.168.86.122:11434/v1",
        api_key="sk-real",
    )
    assert kept.client.api_key == "sk-real"


def test_a_claude_host_uses_the_messages_api(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    class Response:
        status_code = 200

        def json(self) -> dict:
            return {
                "content": [
                    {"type": "text", "text": "Looking."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "a.py"},
                    },
                ],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            }

    def fake_post(url, headers, json, timeout):
        seen["url"] = url
        seen["headers"] = headers
        seen["json"] = json
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr("agent_lbw.llm.client.httpx.post", fake_post)
    llm = client_for(
        model="claude-sonnet-4-5",
        base_url="https://api.anthropic.com/v1",
        api_key="sk-ant-test",
    )
    assert isinstance(llm, AnthropicClient)
    turn = llm.complete(
        [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Read a.py"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "toolu_0",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_0",
                "name": "read_file",
                "content": "print(1)",
            },
        ],
        [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-ant-test"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    body = seen["json"]
    assert body["model"] == "claude-sonnet-4-5"
    assert body["system"] == "Be brief."
    assert body["messages"][1]["content"][0]["type"] == "tool_use"
    assert body["messages"][1]["content"][0]["input"] == {"path": "a.py"}
    assert body["messages"][2]["content"][0]["type"] == "tool_result"
    assert body["messages"][2]["content"][0]["content"] == "print(1)"
    assert body["tools"][0]["input_schema"] == {"type": "object"}
    assert turn.text == "Looking."
    assert turn.tool_calls[0].name == "read_file"
    assert turn.tool_calls[0].arguments == {"path": "a.py"}
    assert turn.prompt_tokens == 3
    assert turn.completion_tokens == 4
    assert turn.total_tokens == 7


def test_a_cursor_host_does_not_call_chat_completions() -> None:
    llm = client_for(
        model="grok-4.5",
        base_url="https://api.cursor.com/v1",
        api_key="cursor_test",
    )
    with pytest.raises(ModelRequestError, match="does not serve chat completions"):
        llm.complete([{"role": "user", "content": "hi"}], [])
