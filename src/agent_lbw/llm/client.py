from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from openai import OpenAI

from agent_lbw.llm.glimmer import ATEM_RETRY_NUDGE, is_atem_parse_error

log = logging.getLogger("lbw")

_WINDOW_CACHE: dict[tuple[str, str], int | None] = {}
_TRANSIENT_ATTEMPTS = 3
_TRANSIENT_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
_CONTEXT_KEYS = (
    "context_length",
    "max_model_len",
    "max_tokens",
    "context_window",
    "max_context_length",
)
_NUM_CTX_RE = re.compile(r"num_ctx\s+(\d+)", re.IGNORECASE)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AssistantTurn:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


class LLMClient(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        """Return the next assistant turn."""


class OpenAICompatClient:
    """Talks to Ollama, vLLM, LM Studio, or any other OpenAI-compatible server."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        context_window: int | None = None,
    ) -> None:
        self.model = model
        self.base_url = normalize_model_base_url(base_url)
        self.context_window = context_window if context_window and context_window >= 512 else None
        # The SDK rejects a blank key. Ollama, vLLM, and LM Studio do not check one.
        self.client = OpenAI(base_url=self.base_url, api_key=api_key.strip() or "ollama")

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        blocked = cursor_chat_block(self.base_url)
        if blocked:
            raise ModelRequestError(blocked, status_code=404)
        try:
            return self._complete_with_retry(messages, tools)
        except Exception as exc:
            if not (tools and is_atem_parse_error(exc)):
                raise
            log.warning("server rejected Glimmer tool call without ATEM wrapper; retrying")
            nudged = [*messages, {"role": "user", "content": ATEM_RETRY_NUDGE}]
            try:
                return self._complete_with_retry(nudged, tools)
            except Exception as retry_exc:
                if not is_atem_parse_error(retry_exc):
                    raise
                log.warning("Glimmer ATEM retry still rejected; completing without native tools")
                return self._complete_with_retry(nudged, [])

    def _complete_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        for attempt in range(1, _TRANSIENT_ATTEMPTS + 1):
            try:
                return self._complete(messages, tools)
            except Exception as exc:
                if attempt >= _TRANSIENT_ATTEMPTS or not is_transient_llm_error(exc):
                    raise
                log.warning(
                    "model request failed (%s); retrying %s/%s",
                    exc,
                    attempt,
                    _TRANSIENT_ATTEMPTS - 1,
                )
                time.sleep(0.5 * attempt)
        raise RuntimeError("model request failed")

    def _complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        try:
            response = self._create(messages, tools, with_num_ctx=self.context_window is not None)
        except Exception as exc:
            if self.context_window is None or not _rejected_num_ctx(exc):
                raise
            log.warning("server rejected num_ctx; sending the request without it")
            self.context_window = None
            response = self._create(messages, tools, with_num_ctx=False)
        choice = response.choices[0].message
        calls: list[ToolCall] = []
        for call in choice.tool_calls or []:
            calls.append(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=_parse_arguments(call.function.arguments),
                )
            )
        prompt_tokens, completion_tokens, total_tokens = completion_usage(response)
        return AssistantTurn(
            text=choice.content,
            tool_calls=calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=cached_prompt_tokens(response),
        )

    def _create(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        with_num_ctx: bool,
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if with_num_ctx and self.context_window:
            kwargs["extra_body"] = {"options": {"num_ctx": self.context_window}}
        return self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools or None,
            tool_choice="auto" if tools else None,
            **kwargs,
        )


class ModelRequestError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AnthropicClient:
    """Talks to Claude through Anthropic's Messages API."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        context_window: int | None = None,
    ) -> None:
        self.model = model
        self.base_url = normalize_model_base_url(base_url).rstrip("/")
        self.api_key = api_key.strip()
        self.context_window = context_window if context_window and context_window >= 512 else None

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        try:
            return self._complete_with_retry(messages, tools)
        except Exception as exc:
            if not (tools and is_atem_parse_error(exc)):
                raise
            log.warning("Claude rejected the tool call; retrying")
            nudged = [*messages, {"role": "user", "content": ATEM_RETRY_NUDGE}]
            try:
                return self._complete_with_retry(nudged, tools)
            except Exception as retry_exc:
                if not is_atem_parse_error(retry_exc):
                    raise
                log.warning("Claude tool retry still rejected; completing without native tools")
                return self._complete_with_retry(nudged, [])

    def _complete_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        for attempt in range(1, _TRANSIENT_ATTEMPTS + 1):
            try:
                return self._complete(messages, tools)
            except Exception as exc:
                if attempt >= _TRANSIENT_ATTEMPTS or not is_transient_llm_error(exc):
                    raise
                log.warning(
                    "Claude request failed (%s); retrying %s/%s",
                    exc,
                    attempt,
                    _TRANSIENT_ATTEMPTS - 1,
                )
                time.sleep(0.5 * attempt)
        raise RuntimeError("model request failed")

    def _complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        system, turns = _anthropic_messages(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 8192,
            "messages": turns,
        }
        if system:
            body["system"] = system
        converted = _anthropic_tools(tools)
        if converted:
            body["tools"] = converted
            body["tool_choice"] = {"type": "auto"}
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-api-key": self.api_key,
        }
        response = httpx.post(
            f"{self.base_url}/messages",
            headers=headers,
            json=body,
            timeout=600.0,
        )
        if response.status_code >= 400:
            raise ModelRequestError(
                _anthropic_error(response),
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelRequestError("Claude returned a response that was not JSON") from exc
        return _anthropic_turn(payload)


def cursor_chat_block(base_url: str) -> str:
    """Cursor lists models. It does not serve chat completions."""
    try:
        host = urlparse(normalize_model_base_url(base_url)).netloc.lower()
    except ValueError:
        return ""
    if host != "api.cursor.com":
        return ""
    return (
        "Cursor lists models but does not serve chat completions. "
        "Run this proposal through a gateway that does, such as LiteLLM."
    )


def client_for(
    *,
    model: str,
    base_url: str,
    api_key: str,
    context_window: int | None = None,
) -> LLMClient:
    """The client for this host. Claude uses Anthropic's Messages API."""
    host = ""
    try:
        host = urlparse(normalize_model_base_url(base_url)).netloc.lower()
    except ValueError:
        host = ""
    if host == "api.anthropic.com":
        return AnthropicClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            context_window=context_window,
        )
    return OpenAICompatClient(
        model=model,
        base_url=base_url,
        api_key=api_key,
        context_window=context_window,
    )


def _anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system: list[str] = []
    turns: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role == "system":
            text = _message_text(message.get("content"))
            if text:
                system.append(text)
            continue
        if role == "tool":
            _append_blocks(
                turns,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(message.get("tool_call_id") or ""),
                        "content": _message_text(message.get("content")),
                    }
                ],
            )
            continue
        if role == "assistant":
            blocks = _assistant_blocks(message)
            if blocks:
                _append_blocks(turns, "assistant", blocks)
            continue
        text = _message_text(message.get("content"))
        if text:
            _append_blocks(turns, "user", [{"type": "text", "text": text}])
    if turns and turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": [{"type": "text", "text": "Continue."}]})
    if not turns:
        turns.append({"role": "user", "content": [{"type": "text", "text": "Continue."}]})
    return "\n\n".join(system), turns


def _assistant_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    text = _message_text(message.get("content"))
    if text:
        blocks.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        blocks.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or ""),
                "name": str(function.get("name") or ""),
                "input": _tool_input(function.get("arguments")),
            }
        )
    return blocks


def _tool_input(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        schema = function.get("parameters")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        converted.append(
            {
                "name": name,
                "description": str(function.get("description") or ""),
                "input_schema": schema,
            }
        )
    return converted


def _append_blocks(turns: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    if turns and turns[-1]["role"] == role:
        turns[-1]["content"].extend(blocks)
        return
    turns.append({"role": role, "content": list(blocks)})


def _message_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(part for part in parts if part)
    return str(content)


def _anthropic_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if payload.get("message"):
            return str(payload["message"])
    return f"Claude returned HTTP {response.status_code}"


def _anthropic_turn(payload: object) -> AssistantTurn:
    if not isinstance(payload, dict):
        raise ModelRequestError("Claude returned a response that was not a message")
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            texts.append(str(block["text"]))
        elif block.get("type") == "tool_use":
            raw = block.get("input")
            calls.append(
                ToolCall(
                    id=str(block.get("id") or ""),
                    name=str(block.get("name") or ""),
                    arguments=raw if isinstance(raw, dict) else {},
                )
            )
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    fresh = _positive_int(usage.get("input_tokens"))
    cache_read = _positive_int(usage.get("cache_read_input_tokens"))
    cache_write = _positive_int(usage.get("cache_creation_input_tokens"))
    completion = _positive_int(usage.get("output_tokens"))
    # Claude counts cached input apart from input_tokens. Input here is every
    # input token, cached or not, as the OpenTelemetry GenAI conventions ask.
    parts = [value for value in (fresh, cache_read, cache_write) if value is not None]
    prompt = sum(parts) if parts else None
    total = prompt + completion if prompt is not None and completion is not None else None
    return AssistantTurn(
        text="\n".join(texts) or None,
        tool_calls=calls,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


class ScriptedClient:
    """Deterministic stand-in used by unit tests."""

    def __init__(self, turns: Sequence[AssistantTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[list[dict[str, Any]]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        self.calls.append(messages)
        if not self._turns:
            return AssistantTurn(text="no more scripted turns")
        return self._turns.pop(0)


def _rejected_num_ctx(exc: BaseException) -> bool:
    """True when the server refused the Ollama num_ctx option, not the prompt."""
    status = getattr(exc, "status_code", None)
    if status != 400:
        return False
    text = str(exc).lower()
    return "num_ctx" in text or "options" in text or "unexpected" in text or "extra" in text


def is_transient_llm_error(exc: BaseException) -> bool:
    """True when the model server failed in a way a second request may survive."""
    text = str(exc)
    if is_atem_parse_error(exc) and "XML syntax error" not in text:
        return False
    status = getattr(exc, "status_code", None)
    if status in _TRANSIENT_STATUS:
        return True
    lowered = text.lower()
    return "xml syntax error" in lowered or "unexpected eof" in lowered


def check_model_endpoint(base_url: str, api_key: str, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        url = normalize_model_base_url(base_url).rstrip("/") + "/models"
    except ValueError as exc:
        return False, str(exc)
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = httpx.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        return False, f"unreachable: {exc}"
    if response.status_code >= 400:
        return False, f"HTTP {response.status_code} from {url}"
    try:
        payload = response.json()
    except ValueError:
        return True, f"reachable ({response.status_code})"
    names = model_ids_from_payload(payload)
    if names:
        shown = names[:8]
        extra = "" if len(names) <= 8 else f" (+{len(names) - 8} more)"
        return True, "models: " + ", ".join(shown) + extra
    return True, f"reachable ({response.status_code})"


def list_remote_models(base_url: str, api_key: str, timeout: float = 3.0) -> list[str]:
    """Return model ids from an OpenAI-compatible `/models` endpoint."""
    url = normalize_model_base_url(base_url).rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = httpx.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError:
        return []
    if response.status_code >= 400:
        return []
    try:
        payload = response.json()
    except ValueError:
        return []
    return model_ids_from_payload(payload)


def normalize_model_base_url(url: str) -> str:
    """Accept host:port or a full OpenAI-compatible `/v1` URL."""
    text = url.strip()
    if not text:
        raise ValueError("LLM server URL is required")
    if "://" not in text:
        text = "http://" + text
    parsed = text.rstrip("/")
    if parsed.endswith("/v1"):
        return parsed
    return parsed + "/v1"


def completion_usage(response: object) -> tuple[int | None, int | None, int | None]:
    """Read prompt/completion/total tokens from an OpenAI-compatible response."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None, None, None
    return (
        _usage_int(usage, "prompt_tokens"),
        _usage_int(usage, "completion_tokens"),
        _usage_int(usage, "total_tokens"),
    )


def cached_prompt_tokens(response: object) -> int | None:
    """Prompt tokens an OpenAI-compatible server served from its cache."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None
    details = (
        usage.get("prompt_tokens_details")
        if isinstance(usage, dict)
        else getattr(usage, "prompt_tokens_details", None)
    )
    if details is None:
        return None
    return _usage_int(details, "cached_tokens")


def tag_client(
    client: object, *, model_id: str = "", service: str = "", provider: str = ""
) -> None:
    """Label a client with the catalog model and service it runs, for usage records."""
    for name, value in (
        ("usage_model_id", model_id),
        ("usage_service", service),
        ("usage_provider", provider),
    ):
        if value:
            try:
                setattr(client, name, value)
            except AttributeError:
                return


def apply_credential(client: object, credential: dict) -> None:
    """Tag the client with the catalog model and service its credential belongs to."""
    tag_client(
        client,
        model_id=str(credential.get("model_id") or ""),
        service=str(credential.get("service") or ""),
        provider=str(credential.get("provider") or ""),
    )


def effective_context_window(
    *,
    configured: int | None,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 2.0,
) -> int | None:
    """Use the configured window when set, otherwise ask the server.

    A configured value wins because local hardware often cannot hold the
    context length the model advertises.
    """
    if configured is not None and configured >= 512:
        return configured
    return lookup_context_window(base_url, api_key, model, timeout=timeout)


def lookup_context_window(
    base_url: str,
    api_key: str,
    model: str,
    *,
    timeout: float = 2.0,
) -> int | None:
    """Best-effort context length for a model on an OpenAI-compatible host."""
    name = (model or "").strip()
    if not name:
        return None
    try:
        url = normalize_model_base_url(base_url)
    except ValueError:
        return None
    cache_key = (url, name)
    if cache_key in _WINDOW_CACHE:
        return _WINDOW_CACHE[cache_key]
    window = _fetch_context_window(url, api_key, name, timeout=timeout)
    _WINDOW_CACHE[cache_key] = window
    return window


def context_window_from_ollama_show(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    info = payload.get("model_info") or payload.get("modelinfo") or {}
    if isinstance(info, dict):
        for key, value in info.items():
            text = str(key).lower()
            if text.endswith(".context_length") or text in _CONTEXT_KEYS:
                parsed = _positive_int(value)
                if parsed is not None:
                    return parsed
    params = payload.get("parameters")
    if isinstance(params, str):
        match = _NUM_CTX_RE.search(params)
        if match:
            return _positive_int(match.group(1))
    return None


def context_window_from_models_payload(payload: object, model: str) -> int | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    wanted = (model or "").strip().lower()
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("id") or item.get("name") or "").strip()
        if wanted and name.lower() != wanted:
            continue
        for key in _CONTEXT_KEYS:
            parsed = _positive_int(item.get(key))
            if parsed is not None:
                return parsed
        extra = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        for key in _CONTEXT_KEYS:
            parsed = _positive_int(extra.get(key))
            if parsed is not None:
                return parsed
    return None


def _fetch_context_window(
    base_url: str,
    api_key: str,
    model: str,
    *,
    timeout: float,
) -> int | None:
    headers = {"Authorization": f"Bearer {api_key}"}
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    try:
        response = httpx.post(
            f"{origin}/api/show",
            json={"name": model},
            headers=headers,
            timeout=timeout,
        )
        if response.status_code < 400:
            window = context_window_from_ollama_show(response.json())
            if window is not None:
                return window
    except (httpx.HTTPError, ValueError):
        pass
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers=headers,
            timeout=timeout,
        )
        if response.status_code < 400:
            return context_window_from_models_payload(response.json(), model)
    except (httpx.HTTPError, ValueError):
        pass
    return None


def _usage_int(usage: object, key: str) -> int | None:
    if isinstance(usage, dict):
        return _positive_int(usage.get(key))
    return _positive_int(getattr(usage, key, None))


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def model_ids_from_payload(payload: object) -> list[str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("id") or item.get("name")
        if not name:
            continue
        text = str(name)
        if text in seen:
            continue
        seen.add(text)
        names.append(text)
    return names


def _parse_arguments(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if not isinstance(raw, str):
        return {"_raw": str(raw)}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_raw": raw}


def iter_text_blocks(text: str | None) -> Iterator[str]:
    if text:
        yield text
