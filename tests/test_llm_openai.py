"""The OpenAI backend must round-trip the loop's Anthropic-shaped conversation.

No network: a fake stands in for `openai.OpenAI()` and records the request.
"""

import json
import types

from cua.discovery.llm_openai import OpenAIMessagesClient
from cua.discovery.prompts import EMIT_CAPABILITY_TOOL, TOOLS


class FakeCompletions:
    def __init__(self, message):
        self.message = message
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=self.message)])


def _client(message):
    completions = FakeCompletions(message)
    fake = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    return OpenAIMessagesClient(fake), completions


def _tool_call(name, arguments, call_id="call_1"):
    return types.SimpleNamespace(
        id=call_id, function=types.SimpleNamespace(name=name, arguments=arguments)
    )


def test_conversation_translates_to_chat_completions():
    reply = types.SimpleNamespace(
        content=None, tool_calls=[_tool_call("click", json.dumps({"ref": 3, "intent": "Search"}))]
    )
    client, completions = _client(reply)

    previous_call = types.SimpleNamespace(
        type="tool_use", id="call_0", name="type", input={"ref": 1, "text": "x", "intent": "i"}
    )
    response = client.messages.create(
        model="gpt-test",
        max_tokens=100,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}],
        tools=TOOLS,
        messages=[
            {"role": "user", "content": "goal"},
            {"role": "assistant", "content": [previous_call]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_0", "content": "no", "is_error": True}
                ],
            },
        ],
    )

    req = completions.requests[0]
    assert "thinking" not in req
    assert req["tool_choice"] == "required" and req["parallel_tool_calls"] is False
    assert req["messages"][0] == {"role": "system", "content": "SYS"}
    assert req["messages"][2]["tool_calls"][0]["function"]["name"] == "type"
    assert req["messages"][3] == {"role": "tool", "tool_call_id": "call_0", "content": "ERROR: no"}

    (block,) = response.content
    assert (block.type, block.name, block.input) == ("tool_use", "click", {"ref": 3, "intent": "Search"})


def test_only_flat_fully_required_tools_are_strict():
    client, completions = _client(types.SimpleNamespace(content="ok", tool_calls=None))
    client.messages.create(
        model="m", messages=[{"role": "user", "content": "hi"}], tools=[TOOLS[0], EMIT_CAPABILITY_TOOL]
    )
    click, emit = completions.requests[0]["tools"]
    assert click["function"].get("strict") is True
    assert "strict" not in emit["function"]


def test_malformed_arguments_are_not_executed():
    reply = types.SimpleNamespace(content=None, tool_calls=[_tool_call("click", "{not json")])
    client, _ = _client(reply)
    response = client.messages.create(model="m", messages=[{"role": "user", "content": "hi"}], tools=TOOLS)
    assert [b.type for b in response.content] == ["text"]
