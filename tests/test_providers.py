"""LiteLLM client against a local fake proxy: no account or key is needed."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from harness.providers import OllamaClient, ProxyClient, create_client


@pytest.fixture
def proxy():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply(200, {"data": [{"id": "model-b"}, {"id": "model-a"}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers["Authorization"], body))
            if body["model"] == "denied":
                return self.reply(401, {"error": "invalid key"})
            return self.reply(
                200,
                {
                    "choices": [{"message": {"content": '{"final": "ok"}'}}],
                    "usage": {"total_tokens": 7},
                },
            )

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/", seen
    server.shutdown()


def test_chat_call_sends_the_key_and_returns_the_text(proxy):
    url, seen = proxy
    model = ProxyClient("gpt-x", url, "sk-test", timeout=5)
    assert model([{"role": "user", "content": "hi"}]) == '{"final": "ok"}'
    path, authorization, body = seen[0]
    assert path == "/chat/completions" and authorization == "Bearer sk-test"
    assert body["model"] == "gpt-x" and model.usage_summary == {"total_tokens": 7}


def test_http_error_is_clear_and_never_contains_the_key(proxy):
    url, _ = proxy
    with pytest.raises(ValueError, match="HTTP 401") as error:
        ProxyClient("denied", url, "sk-secret", timeout=5)([])
    assert "sk-secret" not in str(error.value)


def test_models_can_be_listed(proxy):
    assert ProxyClient("", proxy[0], "sk-test", timeout=5).available_models() == [
        "model-a",
        "model-b",
    ]


def test_settings_choose_the_client_and_credentials_come_from_the_environment(monkeypatch):
    config = {"timeout": 5, "name": "x", "endpoint": "http://o", "num_ctx": 1}
    assert isinstance(create_client({**config, "provider": "ollama"}), OllamaClient)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    monkeypatch.delenv("LITELLM_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="LITELLM_BASE_URL, LITELLM_API_KEY"):
        create_client({**config, "provider": "litellm"})
    monkeypatch.setenv("LITELLM_BASE_URL", "http://proxy")
    monkeypatch.setenv("LITELLM_API_KEY", "sk-test")
    monkeypatch.setenv("LITELLM_MODEL", "from-env")
    assert create_client({**config, "provider": "litellm"}).model_id == "from-env"
