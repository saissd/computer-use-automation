"""OpenAI as a discovery backend, behind the client interface the loop already uses.

The discovery loop and the generalisation pass are written against the
Anthropic Messages shape: `client.messages.create(system=, tools=, messages=)`
returning `.content` blocks of type `text` / `tool_use`, with tool results sent
back as `tool_result` blocks. Rather than fork the loop per provider, this
module presents that exact interface and translates at the boundary to OpenAI
Chat Completions. The loop, the stuck detection, the policy gate and the
descriptor capture are therefore identical whichever model discovers.

Provider-specific request options (`thinking`, `output_config`,
`cache_control`) have no Chat Completions equivalent and are dropped.

Two deliberate differences from a literal translation:

- `tool_choice="required"` and `parallel_tool_calls=False`. The loop expects
  exactly one action per turn, and `done` / `escalate` are tools, so the model
  always has a legitimate call available. Requiring one removes a wasted turn
  rather than changing what the model may decide.
- A tool call whose arguments are not valid JSON is surfaced as *text*, not as
  a `tool_use`. The loop then takes its existing "you did not call a tool"
  path and asks again, instead of acting on half-parsed arguments.
"""

import json
from types import SimpleNamespace
from typing import Any

DEFAULT_OPENAI_MODEL = "gpt-4.1"


class OpenAIMessagesClient:
    """Duck-types `anthropic.Anthropic()` for the subset discovery uses."""

    def __init__(self, client: Any | None = None):
        if client is None:
            import openai

            client = openai.OpenAI()
        self._client = client
        self.messages = self

    def create(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int = 4_096,
        system: Any = None,
        tools: list[dict] | None = None,
        **_provider_specific: Any,
    ) -> SimpleNamespace:
        oa_messages: list[dict] = []
        system_text = _system_text(system)
        if system_text:
            oa_messages.append({"role": "system", "content": system_text})
        for message in messages:
            oa_messages.extend(_translate_message(message))

        request: dict[str, Any] = {
            "model": model,
            "messages": oa_messages,
            "max_completion_tokens": max_tokens,
        }
        if tools:
            request["tools"] = [_translate_tool(t) for t in tools]
            request["tool_choice"] = "required"
            request["parallel_tool_calls"] = False

        response = self._client.chat.completions.create(**request)
        return _translate_response(response)


# --- request translation ---------------------------------------------------


def _system_text(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(_get(block, "text", "") for block in system)


def _translate_tool(tool: dict) -> dict:
    schema = tool["input_schema"]
    function: dict[str, Any] = {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "parameters": schema,
    }
    # OpenAI strict mode demands every property be required and every nested
    # object close its properties. Only flat, fully-required schemas qualify;
    # the rest are sent non-strict and the loop's own checks still apply.
    props = schema.get("properties", {})
    flat = all(p.get("type") not in ("object", "array") for p in props.values())
    if tool.get("strict") and flat and set(schema.get("required", [])) == set(props):
        function["strict"] = True
    return {"type": "function", "function": function}


def _translate_message(message: dict) -> list[dict]:
    role, content = message["role"], message["content"]
    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if role == "assistant":
        texts, tool_calls = [], []
        for block in content:
            kind = _get(block, "type")
            if kind == "text":
                texts.append(_get(block, "text", ""))
            elif kind == "tool_use":
                tool_calls.append(
                    {
                        "id": _get(block, "id"),
                        "type": "function",
                        "function": {
                            "name": _get(block, "name"),
                            "arguments": json.dumps(_get(block, "input", {})),
                        },
                    }
                )
        out: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
        if tool_calls:
            out["tool_calls"] = tool_calls
        return [out]

    # user turn: tool results become `tool` messages, text stays a user message
    translated: list[dict] = []
    texts = []
    for block in content:
        kind = _get(block, "type")
        if kind == "tool_result":
            body = _get(block, "content", "")
            if not isinstance(body, str):
                body = "\n".join(_get(b, "text", "") for b in body)
            if _get(block, "is_error"):
                body = f"ERROR: {body}"
            translated.append(
                {"role": "tool", "tool_call_id": _get(block, "tool_use_id"), "content": body}
            )
        elif kind == "text":
            texts.append(_get(block, "text", ""))
    if texts:
        translated.append({"role": "user", "content": "\n".join(texts)})
    return translated


# --- response translation --------------------------------------------------


def _translate_response(response: Any) -> SimpleNamespace:
    choice = response.choices[0]
    message = choice.message
    blocks: list[SimpleNamespace] = []
    if message.content:
        blocks.append(SimpleNamespace(type="text", text=message.content))

    # The loop answers exactly one call per turn, and OpenAI rejects a history
    # containing a call with no matching result, so only the first is kept.
    for call in (message.tool_calls or [])[:1]:
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            blocks.append(
                SimpleNamespace(
                    type="text",
                    text=f"(malformed arguments for {call.function.name}; not executed)",
                )
            )
            continue
        blocks.append(
            SimpleNamespace(type="tool_use", id=call.id, name=call.function.name, input=args)
        )

    stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return SimpleNamespace(content=blocks, stop_reason=stop)


def _get(block: Any, key: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)
