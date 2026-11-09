"""HTTP model adapters for Ollama and LiteLLM-compatible proxies."""

import json
import os
import urllib.error
import urllib.request

MAX_RESPONSE_BYTES = 1_000_000


def _bounded_response(response, provider: str) -> bytes:
    """Read enough to detect an oversized reply without buffering it in full."""
    payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError(f"{provider} response exceeded size limit")
    return payload


class OllamaClient:
    """Adapt Ollama chat responses to the model callable used by the agent."""

    def __init__(
        self,
        model_id,
        endpoint="http://127.0.0.1:11434/api/chat",
        timeout=90,
        num_ctx=16384,
        max_tokens=2048,
    ):
        self.model_id = model_id
        self.endpoint = endpoint
        self.timeout = timeout
        # Ollama's default window is small and silently drops the oldest messages,
        # including the system prompt, once the history outgrows it.
        self.num_ctx = num_ctx
        self.max_tokens = max_tokens
        self.usage_summary = {}

    def __call__(self, messages):
        """Return the model's message content as a JSON string.

        Raises:
            OSError/ValueError: on connection failure, timeout, or a malformed reply.
        """
        body = json.dumps(
            {
                "model": self.model_id,
                "messages": messages,
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": 0,
                    "num_predict": self.max_tokens,
                    "num_ctx": self.num_ctx,
                },
            }
        ).encode()
        request = urllib.request.Request(
            self.endpoint, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = _bounded_response(response, "Ollama")
        data = json.loads(raw)
        self.usage_summary = {
            "prompt_tokens": data.get("prompt_eval_count"),
            "output_tokens": data.get("eval_count"),
            "num_ctx": self.num_ctx,
        }
        content = data["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("Ollama response has no text content")  # noqa: TRY004
        return content


class ProxyClient:
    """Call a LiteLLM-compatible proxy with credentials supplied by the host environment."""

    def __init__(self, model_id, base_url, api_key, timeout=120, max_tokens=2048):
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.usage_summary = {}

    def _exchange(self, path, body=None):
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = _bounded_response(response, "LiteLLM")
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise ValueError(f"LiteLLM returned HTTP {exc.code}: {detail}") from None
        return json.loads(raw)

    def __call__(self, messages):
        """Return the model's message content.

        Raises:
            OSError/ValueError: on connection failure, timeout, HTTP error or a
                malformed reply.
        """
        data = self._exchange(
            "/chat/completions",
            {
                "model": self.model_id,
                "messages": messages,
                "temperature": 0,
                "max_tokens": self.max_tokens,
            },
        )
        self.usage_summary = data.get("usage") or {}
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ValueError("LiteLLM response has no message") from None
        if not isinstance(content, str):
            raise ValueError("LiteLLM response has no text content")  # noqa: TRY004
        return content

    def available_models(self):
        """List accessible model identifiers in sorted order."""
        return sorted(item["id"] for item in self._exchange("/models")["data"])


def create_client(config, name=None):
    """Select the configured provider and apply the existing model-name precedence."""
    if config.get("provider") == "ollama":
        return OllamaClient(
            name or config["name"],
            config["endpoint"],
            config["timeout"],
            config["num_ctx"],
            config.get("max_tokens", 2048),
        )
    values = {
        "LITELLM_BASE_URL": os.environ.get("LITELLM_BASE_URL"),
        "LITELLM_API_KEY": os.environ.get("LITELLM_API_KEY"),
    }
    if missing := [key for key, value in values.items() if not value]:
        raise RuntimeError(f"missing {', '.join(missing)}; see .env.example")
    return ProxyClient(
        name or os.environ.get("LITELLM_MODEL") or config.get("name", ""),
        values["LITELLM_BASE_URL"],
        values["LITELLM_API_KEY"],
        config["timeout"],
        config.get("max_tokens", 2048),
    )
