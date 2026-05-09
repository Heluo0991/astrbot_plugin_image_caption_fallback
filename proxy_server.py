"""HTTP proxy server that routes image caption requests with automatic fallback.

Runs an OpenAI-compatible /v1/chat/completions endpoint. Forwards each request
to the primary model first; on failure, iterates through fallback models.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler


class ImageCaptionProxy:
    """Local HTTP proxy with primary → fallback routing for vision models."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11435,
        primary: dict | None = None,
        fallbacks: list[dict] | None = None,
        logger=None,
    ) -> None:
        self.host = host
        self.port = port
        self.primary = primary or {}
        self.fallbacks = fallbacks or []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._log = logger
        self._lock = threading.Lock()
        self._stats: dict[str, dict[str, int]] = {}  # model -> {success, fail}

    def _info(self, msg: str) -> None:
        if self._log:
            self._log.info(msg)

    def _warning(self, msg: str) -> None:
        if self._log:
            self._log.warning(msg)

    def start(self) -> bool:
        """Start the proxy server in a daemon thread. Returns True on success."""
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    self._handle_health()
                elif self.path == "/stats":
                    self._handle_stats()
                else:
                    self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                if self.path in ("/v1/chat/completions", "/chat/completions"):
                    self._handle_chat()
                elif self.path == "/health":
                    self._handle_health()
                elif self.path == "/stats":
                    self._handle_stats()
                elif self.path == "/admin/primary":
                    self._handle_admin_primary()
                elif self.path == "/admin/fallback/add":
                    self._handle_admin_fallback_add()
                elif self.path == "/admin/fallback/del":
                    self._handle_admin_fallback_del()
                elif self.path == "/admin/fallback/clear":
                    self._handle_admin_fallback_clear()
                elif self.path == "/admin/stats/reset":
                    self._handle_admin_stats_reset()
                else:
                    self.send_error(404)

            def _handle_chat(self) -> None:  # noqa: N802
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length))
                except Exception:
                    self.send_error(400)
                    return

                result, provider_name = proxy._try_all(body)

                if result is None:
                    proxy._warning("All image caption providers failed")
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        json.dumps(
                            {"error": {"message": "All image caption providers failed"}}
                        ).encode()
                    )
                    return

                proxy._info(f"Image caption succeeded via {provider_name}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())

            def _handle_health(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok"}).encode())

            def _handle_stats(self) -> None:  # noqa: N802
                stats = proxy.get_stats()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(stats).encode())

            def _handle_admin_primary(self) -> None:  # noqa: N802
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length))
                except Exception:
                    self.send_error(400)
                    return
                proxy.update_primary(
                    body.get("api_base", ""),
                    body.get("api_key", ""),
                    body.get("model", ""),
                    int(body.get("timeout", 5)),
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "model": body.get("model")}).encode())

            def _handle_admin_fallback_add(self) -> None:  # noqa: N802
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length))
                except Exception:
                    self.send_error(400)
                    return
                idx = proxy.add_fallback(
                    body.get("api_base", ""),
                    body.get("api_key", ""),
                    body.get("model", ""),
                    int(body.get("timeout", 60)),
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "index": idx - 1, "total": idx}).encode())

            def _handle_admin_fallback_del(self) -> None:  # noqa: N802
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length))
                except Exception:
                    self.send_error(400)
                    return
                removed = proxy.remove_fallback(int(body.get("index", -1)))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({
                        "ok": removed is not None,
                        "removed": removed.get("model") if removed else None,
                    }).encode()
                )

            def _handle_admin_fallback_clear(self) -> None:  # noqa: N802
                count = proxy.clear_fallbacks()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "cleared": count}).encode())

            def _handle_admin_stats_reset(self) -> None:  # noqa: N802
                count = proxy.reset_stats()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "cleared": count}).encode())

            def log_message(self, format, *args) -> None:  # noqa: A002
                pass  # suppress default stderr logs

        try:
            self._server = HTTPServer((self.host, self.port), Handler)
        except OSError as e:
            self._warning(f"Failed to bind {self.host}:{self.port}: {e}")
            return False

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._info(f"Image caption proxy started on {self.host}:{self.port}")
        return True

    def stop(self) -> None:
        """Shutdown the proxy server."""
        if self._server:
            self._server.shutdown()
            self._server = None
            self._info("Image caption proxy stopped")

    # ---- Hot-swap API ----

    def update_primary(self, api_base: str, api_key: str, model: str, timeout: int) -> None:
        """Hot-swap the primary model without restarting the proxy."""
        self.primary = {
            "api_base": api_base,
            "api_key": api_key,
            "model": model,
            "timeout": timeout,
        }
        self._info(f"Primary model hot-swapped to {model} (timeout={timeout}s)")

    def update_fallbacks(self, fallbacks: list[dict]) -> None:
        """Replace the entire fallback chain at runtime."""
        self.fallbacks = fallbacks

    def add_fallback(self, api_base: str, api_key: str, model: str, timeout: int) -> int:
        """Append a fallback model. Returns the new chain length."""
        self.fallbacks.append({
            "api_base": api_base,
            "api_key": api_key,
            "model": model,
            "timeout": timeout,
        })
        return len(self.fallbacks)

    def remove_fallback(self, index: int) -> dict | None:
        """Remove a fallback by 0-based index. Returns the removed entry or None."""
        if 0 <= index < len(self.fallbacks):
            return self.fallbacks.pop(index)
        return None

    def clear_fallbacks(self) -> int:
        """Remove all fallbacks. Returns the count of removed entries."""
        count = len(self.fallbacks)
        self.fallbacks = []
        return count

    def reset_stats(self) -> int:
        """Clear all stats counters. Returns the number of models cleared."""
        with self._lock:
            count = len(self._stats)
            self._stats = {}
            return count

    def get_stats(self) -> dict:
        """Return a snapshot of per-model success/fail counts."""
        with self._lock:
            return {k: dict(v) for k, v in self._stats.items()}

    def _record(self, model: str, success: bool) -> None:
        with self._lock:
            entry = self._stats.setdefault(model, {"success": 0, "fail": 0})
            if success:
                entry["success"] += 1
            else:
                entry["fail"] += 1

    def _try_all(self, request_body: dict) -> tuple[dict | None, str]:
        """Try primary then fallbacks. Returns (result, provider_name) or (None, '').

        Each provider dict may include a 'timeout' key (seconds). Falls back to 60s.
        """
        timeout = self.primary.get("timeout", 60)
        result = self._call_provider(self.primary, request_body, timeout)
        if result is not None:
            self._record(self.primary.get("model", "primary"), True)
            return result, self.primary.get("model", "primary")
        self._record(self.primary.get("model", "primary"), False)

        for fb in self.fallbacks:
            timeout = fb.get("timeout", 60)
            result = self._call_provider(fb, request_body, timeout)
            if result is not None:
                self._record(fb.get("model", "fallback"), True)
                return result, fb.get("model", "fallback")
            self._record(fb.get("model", "fallback"), False)

        return None, ""

    @staticmethod
    def _build_request_body(provider: dict, request_body: dict) -> dict:
        """Replace model in request body with the target provider's model."""
        body = dict(request_body)
        body["model"] = provider.get("model", "")
        return body

    def _call_provider(
        self, provider: dict, request_body: dict, timeout: int = 60
    ) -> dict | None:
        """Call a single provider. Returns parsed JSON response or None on failure."""
        api_base = provider.get("api_base", "").rstrip("/")
        api_key = provider.get("api_key", "")
        model = provider.get("model", "")

        if not api_base or not model:
            return None

        url = f"{api_base}/chat/completions"
        body = self._build_request_body(provider, request_body)
        data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {api_key}")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            self._warning(f"Provider {model} failed: {e}")
            return None
        except Exception as e:
            self._warning(f"Provider {model} unexpected error: {e}")
            return None
