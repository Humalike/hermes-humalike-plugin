"""Public /soul command contract tests with a real local Personas HTTP server."""

import asyncio
import importlib.util
import json
import os
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("soul_command_test", _ROOT / "soul" / "__init__.py")
soul = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = soul
_SPEC.loader.exec_module(soul)

_SEED = "You are a concise technical expert."


class PersonaServer:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner._respond(self)

            def do_GET(self):
                owner._respond(self)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def _respond(self, handler):
        self.requests.append((handler.command, handler.path, handler.headers.get("Authorization")))
        status, body, content_type = self.responses.pop(0)
        encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(encoded)))
        handler.end_headers()
        handler.wfile.write(encoded)


@contextmanager
def _command_env(url, soul_path):
    keys = {"HUMALIKE_API_URL": url, "HUMALIKE_API_KEY": "ak_test", "HERMES_SOUL_PATH": str(soul_path)}
    old = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _command(server):
    with TemporaryDirectory() as directory:
        path = Path(directory) / "SOUL.md"
        path.write_text(_SEED)
        with _command_env(server.url, path):
            reply = asyncio.run(soul.command("enhance"))
        return reply, path.read_text(), path.with_suffix(".md.bak").exists()


def test_enhance_command_writes_completed_persona():
    with PersonaServer([
        (200, {"id": "job-1", "status": "pending"}, "application/json"),
        (200, {"status": "succeeded", "persona": {"system_prompt": "Be precise."}}, "application/json"),
    ]) as server:
        reply, written, backed_up = _command(server)
    assert reply.startswith("✅ Enhanced your persona")
    assert written == "Be precise.\n"
    assert backed_up
    assert server.requests == [
        ("POST", "/v1/personas/actions/enhance", "Bearer ak_test"),
        ("GET", "/v1/personas/repositories/Enhancement/by-id/job-1", "Bearer ak_test"),
    ]


def test_enhance_command_explains_http_failures():
    cases = {
        401: "API key rejected",
        402: "Not enough Humalike credits",
        403: "Humalike denied this request",
        429: "Too many requests",
        503: "Persona service is temporarily unavailable",
    }
    for status, expected in cases.items():
        with PersonaServer([(status, {"error": {"code": "ERROR"}}, "application/json")]) as server:
            reply, written, backed_up = _command(server)
        assert expected in reply
        assert written == _SEED
        assert not backed_up


def test_enhance_command_explains_failed_job_and_invalid_response():
    with PersonaServer([
        (200, {"id": "job-1", "status": "pending"}, "application/json"),
        (200, {"status": "failed", "error": "provider_error"}, "application/json"),
    ]) as server:
        reply, written, backed_up = _command(server)
    assert "enhancement failed on our side" in reply
    assert written == _SEED
    assert not backed_up

    with PersonaServer([(200, b"<html>blocked</html>", "text/html")]) as server:
        reply, written, backed_up = _command(server)
    assert "invalid response" in reply
    assert written == _SEED
    assert not backed_up

    with PersonaServer([
        (200, {"id": "job-1", "status": "pending"}, "application/json"),
        (200, b"<html>blocked</html>", "text/html"),
    ]) as server:
        reply, written, backed_up = _command(server)
    assert "invalid response" in reply
    assert written == _SEED
    assert not backed_up


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all passed")
