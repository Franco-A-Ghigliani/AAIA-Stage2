"""Local Ollama chat adapter."""

import json
import urllib.request

MAX_RESPONSE_BYTES = 1_000_000


class OllamaModel:
    """Callable adapter for Ollama's local chat endpoint."""

    def __init__(
        self,
        model_name,
        endpoint="http://127.0.0.1:11434/api/chat",
        timeout=90,
        num_ctx=16384,
    ):
        self.model_name = model_name
        self.endpoint = endpoint
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.last_usage = {}

    def __call__(self, messages):
        """Return the model's message content as a JSON string.

        Raises:
            OSError/ValueError: On connection failure, timeout,
                or a malformed reply.
        """
        request = urllib.request.Request(
            self.endpoint,
            data=self._encode_request(messages),
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(
            request,
            timeout=self.timeout,
        ) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)

        return self._parse_response(raw)

    def _encode_request(self, messages):
        payload = {
            "model": self.model_name,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_predict": 2048,
                "num_ctx": self.num_ctx,
            },
        }

        return json.dumps(payload).encode()

    def _parse_response(self, raw):
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Ollama response exceeded size limit")

        data = json.loads(raw)

        self.last_usage = {
            "prompt_tokens": data.get("prompt_eval_count"),
            "output_tokens": data.get("eval_count"),
            "num_ctx": self.num_ctx,
        }

        content = data["message"]["content"]

        if not isinstance(content, str):
            raise ValueError("Ollama response has no text content")

        return content