"""DeepSeek transport safety: thinking, truncation, redirects and redaction."""

from __future__ import annotations

import io
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bot.adapters.llm import DeepSeekLLM, _FatalHTTP, _safe_error_text
from bot.adapters.types import LLMOutputError
from bot.budget import BudgetGuard
from tests.helpers import cleanup, new_env


class _RedirectHandler(BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]] = []

    def do_POST(self) -> None:  # noqa: N802
        # Drain the request body before replying.  Replying while the client is
        # still sending can make Windows reset the connection (WinError 10053)
        # instead of delivering the intended 302 to the redirect-refusal test.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length:
            self.rfile.read(length)
        self.seen.append((self.path, self.headers.get("Authorization")))
        if self.path == "/chat/completions":
            self.send_response(302)
            self.send_header("Location", "/leak")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"choices": []}')

    def log_message(self, fmt: str, *args) -> None:
        pass


class DeepSeekTransportTests(unittest.TestCase):
    def _llm(self, env):
        env.config.secrets.deepseek_api_key = "sk-test-secret-value"
        guard = BudgetGuard(env.db, env.config, env.clock)
        return DeepSeekLLM(env.config, env.clock, guard, sleep=lambda _s: None)

    def test_thinking_is_explicitly_disabled_by_default(self) -> None:
        env = new_env("ds-thinking")
        try:
            llm = self._llm(env)
            bodies: list[dict] = []

            def fake_post(body, key):
                bodies.append(body)
                return {
                    "model": "deepseek-flash",
                    "choices": [{"message": {"content": '{"title": "ok"}'}, "finish_reason": "stop"}],
                }

            llm._post = fake_post  # type: ignore[assignment]
            llm.complete_json(purpose="draft", payload={"evidence": []})
            self.assertEqual(bodies[0]["thinking"], {"type": "disabled"})

            env.config.data["llm"]["thinking_disabled"] = False
            llm.complete_json(purpose="draft", payload={"evidence": []})
            self.assertNotIn("thinking", bodies[1])
        finally:
            cleanup(env)

    def test_finish_reason_length_is_rejected_before_content_acceptance(self) -> None:
        env = new_env("ds-length")
        try:
            llm = self._llm(env)
            llm._post = lambda body, key: {  # type: ignore[assignment]
                "choices": [
                    {
                        "message": {"content": '{"title": "partial"}'},
                        "finish_reason": "length",
                    }
                ]
            }
            with self.assertRaises(LLMOutputError):
                llm.complete_json(purpose="draft", payload={"evidence": []})
        finally:
            cleanup(env)

    def test_safe_error_text_redacts_and_truncates(self) -> None:
        secret = "sk-test-secret-value"
        body = ("Authorization: Bearer " + secret + " " + ("x" * 600)).encode()
        exc = urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions",
            400,
            "Bad Request",
            {},
            io.BytesIO(body),
        )
        text = _safe_error_text(exc, secret=secret)
        self.assertNotIn(secret, text)
        self.assertIn("[redacted]", text)
        self.assertLessEqual(len(text), 300)

    def test_redirect_is_refused_and_bearer_is_not_forwarded(self) -> None:
        env = new_env("ds-redirect")
        server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            _RedirectHandler.seen.clear()
            env.config.data["llm"]["base_url"] = f"http://127.0.0.1:{server.server_address[1]}"
            llm = self._llm(env)
            with self.assertRaises(_FatalHTTP) as ctx:
                llm._post({"model": "x"}, "sk-test-secret-value")
            self.assertNotIn("sk-test-secret-value", str(ctx.exception))
            paths = [path for path, _ in _RedirectHandler.seen]
            self.assertEqual(paths, ["/chat/completions"])
        finally:
            server.shutdown()
            server.server_close()
            cleanup(env)


if __name__ == "__main__":
    unittest.main()
