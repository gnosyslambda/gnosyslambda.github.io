#!/usr/bin/env python3
"""Small local HTTP runner for n8n-triggered Codex blog generation."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
HOST = os.environ.get("BLOG_RUNNER_HOST", "127.0.0.1")
PORT = int(os.environ.get("BLOG_RUNNER_PORT", "8765"))
TOKEN = os.environ.get("BLOG_RUNNER_TOKEN", "")
TIMEOUT_SECONDS = int(os.environ.get("BLOG_RUNNER_TIMEOUT_SECONDS", "3600"))
VENV_DIR = REPO_ROOT / ".venv"
SYSTEM_PYTHON_BIN = os.environ.get("BLOG_RUNNER_SYSTEM_PYTHON", "/opt/homebrew/bin/python3")
PYTHON_BIN = os.environ.get("BLOG_RUNNER_PYTHON", str(VENV_DIR / "bin" / "python3"))
HUGO_BIN = os.environ.get("BLOG_RUNNER_HUGO", "/opt/homebrew/bin/hugo")

_lock = threading.Lock()


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _run_step(name: str, command: list[str], env: dict[str, str]) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=TIMEOUT_SECONDS,
    )
    return {
        "name": name,
        "command": command,
        "exitCode": result.returncode,
        "startedAt": started_at,
        "stdoutTail": result.stdout[-4000:],
        "stderrTail": result.stderr[-4000:],
    }


def run_blog_job(force: bool = False) -> tuple[int, dict[str, Any]]:
    if not _lock.acquire(blocking=False):
        return 409, {"ok": False, "error": "blog runner is already running"}

    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    if force:
        env["FORCE_RUN"] = "1"

    steps: list[dict[str, Any]] = []
    try:
        commands = []
        if "BLOG_RUNNER_PYTHON" not in os.environ:
            commands.append(("prepare python venv", [SYSTEM_PYTHON_BIN, "-m", "venv", str(VENV_DIR)]))
        commands.extend([
            ("install dependencies", [PYTHON_BIN, "-m", "pip", "install", "-q", "-r", "scripts/requirements.txt"]),
            ("verify humanizer gate", [PYTHON_BIN, "-B", "scripts/test_humanizer_gate.py"]),
            ("write post with codex", [PYTHON_BIN, "scripts/trend_writer.py"]),
            ("build hugo site", [HUGO_BIN, "--minify"]),
        ])

        for name, command in commands:
            step = _run_step(name, command, env)
            steps.append(step)
            if step["exitCode"] != 0:
                return 500, {"ok": False, "failedStep": name, "steps": steps}

        return 200, {"ok": True, "steps": steps}
    except subprocess.TimeoutExpired as exc:
        return 504, {"ok": False, "error": f"timeout after {TIMEOUT_SECONDS}s", "step": exc.cmd, "steps": steps}
    except Exception as exc:
        return 500, {"ok": False, "error": str(exc), "steps": steps}
    finally:
        _lock.release()


class BlogRunnerHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/health":
            _json_response(self, 404, {"ok": False, "error": "not found"})
            return
        _json_response(self, 200, {"ok": True, "service": "n8n-codex-blog-runner"})

    def do_POST(self) -> None:
        if self.path != "/run":
            _json_response(self, 404, {"ok": False, "error": "not found"})
            return

        if TOKEN and self.headers.get("X-Blog-Runner-Token") != TOKEN:
            _json_response(self, 401, {"ok": False, "error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            body = {}

        status, payload = run_blog_job(force=bool(body.get("force")))
        _json_response(self, status, payload)


def main() -> None:
    if HOST not in {"127.0.0.1", "localhost", "::1"} and not TOKEN:
        raise SystemExit("BLOG_RUNNER_TOKEN is required when BLOG_RUNNER_HOST is not loopback")

    server = ThreadingHTTPServer((HOST, PORT), BlogRunnerHandler)
    print(f"n8n Codex blog runner listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
