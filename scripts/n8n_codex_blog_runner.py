#!/usr/bin/env python3
"""Small local HTTP runner for n8n-triggered Codex blog generation."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import urllib.parse
import urllib.request
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
PUBLISH_BRANCH = os.environ.get("BLOG_RUNNER_PUBLISH_BRANCH", "main")
VALID_TRACKS = {"issue", "tech"}

_lock = threading.Lock()


def _terminate_process_group(process: subprocess.Popen, grace_seconds: float = 5.0) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=grace_seconds)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _run_step(name: str, command: list[str], env: dict[str, str]) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        raise subprocess.TimeoutExpired(command, TIMEOUT_SECONDS) from exc
    return {
        "name": name,
        "command": command,
        "exitCode": process.returncode,
        "startedAt": started_at,
        "stdoutTail": stdout[-4000:],
        "stderrTail": stderr[-4000:],
    }


def _publish_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    safe_paths: list[str] = []
    for value in paths:
        path = Path(value)
        resolved = path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()
        rel = resolved.relative_to(REPO_ROOT).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            raise ValueError(f"refusing to publish git internals: {rel}")
        if rel not in seen and resolved.exists():
            seen.add(rel)
            safe_paths.append(rel)
    return safe_paths


def _created_files(step: dict[str, Any]) -> list[str]:
    stdout = str(step.get("stdoutTail") or "")
    files = re.findall(r"^CREATED_FILE=(.+)$", stdout, flags=re.MULTILINE)
    return _publish_paths(files)


def _frontmatter_value(lines: list[str], key: str) -> str:
    prefix = f"{key}:"
    for line in lines[:40]:
        if line.startswith(prefix):
            return line[len(prefix):].strip().strip('"')
    return ""


def _latest_scores(count: int) -> list[str]:
    path = SCRIPT_DIR / ".quality_history.jsonl"
    scores: list[tuple[str, float]] = []
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("status") == "selected":
                avg = (item.get("scores") or {}).get("avg")
                if isinstance(avg, int | float):
                    scores.append((str(item.get("created_at") or ""), float(avg)))
    return [f"{score:.1f}" for _, score in sorted(scores)[-count:]]


def _telegram_chat_id(token: str, env: dict[str, str]) -> str:
    chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
    if chat_id:
        return chat_id
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    for update in reversed(payload.get("result") or []):
        message = update.get("message") or update.get("edited_message") or update.get("channel_post")
        chat = (message or {}).get("chat") or {}
        if chat.get("id") is not None:
            return str(chat["id"])
    raise RuntimeError("TELEGRAM_CHAT_ID missing and no Telegram updates found; send /start to the bot first")


def _send_telegram_notification(track: str, paths: list[str], env: dict[str, str]) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return {"name": "send telegram notification", "command": ["telegram", "sendMessage"], "exitCode": 1, "startedAt": started_at, "stdoutTail": "", "stderrTail": "TELEGRAM_BOT_TOKEN missing"}

    posts = [Path(path) for path in _publish_paths(paths) if path.startswith("content/posts/")]
    scores = _latest_scores(len(posts))
    lines = [f"n8n blog publish complete ({track})", f"published posts: {len(posts)}", ""]
    for index, post in enumerate(posts):
        content = (REPO_ROOT / post).read_text(errors="replace").splitlines()
        score = scores[index] if index < len(scores) else "unknown"
        lines.append(f"- {_frontmatter_value(content, 'title')} ({score}점)")
        description = _frontmatter_value(content, "description")
        if description:
            lines.append(f"  {description}")
    data = urllib.parse.urlencode({"chat_id": _telegram_chat_id(token, env), "text": "\n".join(lines)})
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data.encode("utf-8"))
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()
    return {"name": "send telegram notification", "command": ["telegram", "sendMessage"], "exitCode": 0, "startedAt": started_at, "stdoutTail": "\n".join(lines), "stderrTail": ""}


def _publish_blog_changes(track: str, paths: list[str], env: dict[str, str]) -> list[dict[str, Any]]:
    publish_paths = _publish_paths(paths + ["scripts/.seen_articles.json", "scripts/.seen_articles_issue.json"])
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    message = f"feat: publish n8n blog posts ({timestamp})"
    body = f"- track: {track}\n- files: {', '.join(publish_paths)}"
    steps = []
    commands = [
        ("git configure bot", ["git", "config", "user.name", "n8n Blog Runner"]),
        ("git configure email", ["git", "config", "user.email", "bot@gnosyslambda.github.io"]),
        ("git fetch main", ["git", "fetch", "origin", f"{PUBLISH_BRANCH}:refs/remotes/origin/{PUBLISH_BRANCH}"]),
        ("verify fast-forward publish", ["git", "merge-base", "--is-ancestor", f"origin/{PUBLISH_BRANCH}", "HEAD"]),
        ("stage published files", ["git", "add", "--", *publish_paths]),
    ]
    for name, command in commands:
        step = _run_step(name, command, env)
        steps.append(step)
        if step["exitCode"] != 0:
            return steps
    check = _run_step("check staged changes", ["git", "diff", "--cached", "--quiet"], env)
    steps.append(check)
    if check["exitCode"] == 0:
        return steps
    if check["exitCode"] == 1:
        check["exitCode"] = 0
        check["stdoutTail"] = "staged changes present"
    else:
        return steps
    for name, command in [
        ("commit published files", ["git", "commit", "-m", message, "-m", body]),
        ("push published files", ["git", "push", "origin", f"HEAD:{PUBLISH_BRANCH}"]),
    ]:
        step = _run_step(name, command, env)
        steps.append(step)
        if step["exitCode"] != 0:
            return steps
    return steps


def run_blog_job(
    force: bool = False,
    track: str = "tech",
    publish: bool = False,
    notify: bool = False,
    publish_paths: list[str] | None = None,
    publish_only: bool = False,
) -> tuple[int, dict[str, Any]]:
    if not _lock.acquire(blocking=False):
        return 409, {"ok": False, "error": "blog runner is already running"}

    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    env["BLOG_TRACK"] = track
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
            ("build hugo site", [HUGO_BIN, "--minify"]),
        ])
        if not publish_only:
            commands.insert(-1, ("write post with codex", [PYTHON_BIN, "scripts/trend_writer.py", "--track", track]))

        for name, command in commands:
            step = _run_step(name, command, env)
            steps.append(step)
            if step["exitCode"] != 0:
                return 500, {"ok": False, "track": track, "failedStep": name, "steps": steps}

        if publish or publish_only:
            paths = list(publish_paths or [])
            if not publish_only:
                paths.extend(_created_files(next(step for step in steps if step["name"] == "write post with codex")))
            for step in _publish_blog_changes(track, paths, env):
                steps.append(step)
                if step["exitCode"] != 0:
                    return 500, {"ok": False, "track": track, "failedStep": step["name"], "steps": steps}
            if notify:
                step = _send_telegram_notification(track, paths, env)
                steps.append(step)
                if step["exitCode"] != 0:
                    return 500, {"ok": False, "track": track, "failedStep": step["name"], "steps": steps}

        return 200, {"ok": True, "track": track, "steps": steps}
    except subprocess.TimeoutExpired as exc:
        return 504, {"ok": False, "track": track, "error": f"timeout after {TIMEOUT_SECONDS}s", "step": exc.cmd, "steps": steps}
    except Exception as exc:
        return 500, {"ok": False, "track": track, "error": str(exc), "steps": steps}
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
        if not isinstance(body, dict):
            body = {}

        track = str(body.get("track") or "tech").strip().lower()
        if track not in VALID_TRACKS:
            _json_response(self, 400, {"ok": False, "error": f"invalid track: {track}"})
            return

        publish_paths = body.get("publishPaths") or []
        if not isinstance(publish_paths, list):
            publish_paths = []

        status, payload = run_blog_job(
            force=bool(body.get("force")),
            track=track,
            publish=bool(body.get("publish")),
            notify=bool(body.get("notify")),
            publish_only=bool(body.get("publishOnly")),
            publish_paths=[str(path) for path in publish_paths],
        )
        _json_response(self, status, payload)


def main() -> None:
    if HOST not in {"127.0.0.1", "localhost", "::1"} and not TOKEN:
        raise SystemExit("BLOG_RUNNER_TOKEN is required when BLOG_RUNNER_HOST is not loopback")

    server = ThreadingHTTPServer((HOST, PORT), BlogRunnerHandler)
    print(f"n8n Codex blog runner listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
