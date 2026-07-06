#!/usr/bin/env python3
"""Small local HTTP runner for n8n-triggered Codex blog generation."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socketserver
import subprocess
import tempfile
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
ENV_FILE = Path(os.environ.get("BLOG_RUNNER_ENV_FILE", str(Path.home() / ".config" / "blog-runner" / "env")))

_lock = threading.Lock()


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


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


def _load_env_file(env: dict[str, str]) -> None:
    if not ENV_FILE.exists():
        return
    for raw_line in ENV_FILE.read_text(errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and not env.get(key):
            env[key] = value.strip().strip("'\"")


def _run_step(name: str, command: list[str], env: dict[str, str], cwd: Path = REPO_ROOT) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    process = subprocess.Popen(
        command,
        cwd=cwd,
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


def _collected_file(step: dict[str, Any]) -> str:
    stdout = str(step.get("stdoutTail") or "")
    match = re.search(r"^COLLECTED_FILE=(.+)$", stdout, flags=re.MULTILINE)
    if not match:
        return ""
    files = _publish_paths([match.group(1)])
    return files[0] if files else ""


def _collected_count(step: dict[str, Any]) -> int:
    stdout = str(step.get("stdoutTail") or "")
    match = re.search(r"^CANDIDATE_COUNT=(\d+)$", stdout, flags=re.MULTILINE)
    return int(match.group(1)) if match else 0


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


_ISSUE_LABELS = {
    "single_source_summary": "단일 출처 요약",
    "weak_community_angle": "커뮤니티 반응 약함",
    "generic_conclusion": "일반적 결론",
    "missing_counterpoint": "반론 부재",
    "thin_evidence": "근거 부족",
    "stale_structure": "구조 진부",
    "ai_tone": "AI 말투",
}


def _html_escape(value: Any) -> str:
    return str("" if value is None else value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _post_history() -> dict[str, dict[str, Any]]:
    """saved_post_relpath -> {avg, issue_codes} from the latest 'selected' quality entry."""
    path = SCRIPT_DIR / ".quality_history.jsonl"
    out: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            rel = item.get("saved_post_relpath")
            if item.get("status") == "selected" and rel:
                out[rel] = {"avg": (item.get("scores") or {}).get("avg"), "issue_codes": item.get("issue_codes") or []}
    return out


def _blog_alert(track: str, ok: bool, paths: list[str], *, failed_step: str = "", error: str = "") -> str:
    posts = [Path(path) for path in _publish_paths(paths) if path.startswith("content/posts/")]
    label = "Gnosys 이슈" if track == "issue" else "Gnosys 기술"
    if not ok:
        lines = [f"🔴 <b>{_html_escape(label)} 발행 실패</b>"]
        if failed_step:
            lines.append(f"⛔ 단계: {_html_escape(failed_step)}")
        if error:
            lines.append(f"⚠️ 오류: {_html_escape(error[:400])}")
        return "\n".join(lines)
    published = bool(posts)
    header = "✅ <b>발행 완료</b>" if published else "📝 <b>검토 대기 · 미발행</b>"
    hist = _post_history()
    out = [f"🔔 {header}", f"🗂 블로그: <b>{_html_escape(label)}</b>", ""]
    for post in posts:
        content = (REPO_ROOT / post).read_text(errors="replace").splitlines()
        title = _frontmatter_value(content, "title") or post.stem
        description = _frontmatter_value(content, "description")
        info = hist.get(post.as_posix(), {})
        avg = info.get("avg")
        emoji = "🟢" if isinstance(avg, (int, float)) and avg >= 90 else ("🟡" if isinstance(avg, (int, float)) and avg >= 85 else "🔴")
        out.append(f"{emoji} <b>{_html_escape(title)}</b>")
        if isinstance(avg, (int, float)):
            out.append(f"   ├ 점수: <b>{avg:.1f}점</b>")
        if description:
            out.append(f"   ├ 📝 {_html_escape(description[:200])}")
        labels = [_ISSUE_LABELS.get(code, code) for code in (info.get("issue_codes") or [])][:4]
        if labels:
            out.append(f"   ├ 📊 지적: {_html_escape(', '.join(labels))}")
        out.append(f'   └ 🔗 <a href="https://gnosyslambda.github.io/posts/{_html_escape(post.stem)}/">글 보기</a>')
        out.append("")
    if not posts:
        out.append("발행된 글 없음 (검토 대기/자동 발행 스킵)")
    return "\n".join(out).strip()


def _collect_alert(
    track: str,
    ok: bool,
    collected_file: str = "",
    candidate_count: int = 0,
    *,
    failed_step: str = "",
    error: str = "",
) -> str:
    label = "Gnosys 이슈" if track == "issue" else "Gnosys 기술"
    lines = [
        f"{'✅' if ok else '❌'} {label} 후보 수집",
        f"상태: {'성공' if ok else '실패'}",
        f"후보: {candidate_count}개",
        f"파일: {collected_file or '없음'}",
        "발행글: 0개",
    ]
    if failed_step:
        lines.append(f"실패 단계: {failed_step}")
    if error:
        lines.append(f"오류: {error}")
    return "\n".join(lines)


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


def _send_telegram_text(text: str, env: dict[str, str]) -> None:
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    data = urllib.parse.urlencode({
        "chat_id": _telegram_chat_id(token, env),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data.encode("utf-8"))
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


def _send_telegram_notification(track: str, paths: list[str], env: dict[str, str]) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    text = _blog_alert(track, True, paths)
    try:
        _send_telegram_text(text, env)
    except Exception as exc:
        return {"name": "send telegram notification", "command": ["telegram", "sendMessage"], "exitCode": 1, "startedAt": started_at, "stdoutTail": "", "stderrTail": exc.__class__.__name__}
    return {"name": "send telegram notification", "command": ["telegram", "sendMessage"], "exitCode": 0, "startedAt": started_at, "stdoutTail": text, "stderrTail": ""}


def _copy_publish_files(publish_paths: list[str], publish_worktree: Path) -> None:
    for rel in publish_paths:
        source = REPO_ROOT / rel
        target = publish_worktree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)


def _publish_blog_changes(track: str, paths: list[str], env: dict[str, str]) -> list[dict[str, Any]]:
    publish_paths = _publish_paths(paths + ["scripts/.seen_articles.json", "scripts/.seen_articles_issue.json"])
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    message = f"feat: publish n8n blog posts ({timestamp})"
    body = f"- track: {track}\n- files: {', '.join(publish_paths)}"
    steps = []
    temp_dir = Path(tempfile.mkdtemp(prefix="blog-runner-publish-"))
    worktree_created = False
    try:
        for name, command in [
            ("git fetch publish branch", ["git", "fetch", "origin", f"refs/heads/{PUBLISH_BRANCH}:refs/remotes/origin/{PUBLISH_BRANCH}"]),
            ("create publish worktree", ["git", "worktree", "add", "--detach", str(temp_dir), f"origin/{PUBLISH_BRANCH}"]),
        ]:
            step = _run_step(name, command, env)
            steps.append(step)
            if step["exitCode"] != 0:
                return steps
            if name == "create publish worktree":
                worktree_created = True

        started_at = datetime.now(timezone.utc).isoformat()
        try:
            _copy_publish_files(publish_paths, temp_dir)
        except Exception as exc:
            steps.append({
                "name": "copy publish files",
                "command": ["copy", *publish_paths],
                "exitCode": 1,
                "startedAt": started_at,
                "stdoutTail": "",
                "stderrTail": exc.__class__.__name__,
            })
            return steps
        steps.append({
            "name": "copy publish files",
            "command": ["copy", *publish_paths],
            "exitCode": 0,
            "startedAt": started_at,
            "stdoutTail": f"copied {len(publish_paths)} files",
            "stderrTail": "",
        })

        for name, command in [
            ("git configure publish bot", ["git", "config", "user.name", "n8n Blog Runner"]),
            ("git configure publish email", ["git", "config", "user.email", "bot@gnosyslambda.github.io"]),
            ("build publish worktree", [HUGO_BIN, "--minify"]),
            ("stage published files", ["git", "add", "--", *publish_paths]),
        ]:
            step = _run_step(name, command, env, cwd=temp_dir)
            steps.append(step)
            if step["exitCode"] != 0:
                return steps

        check = _run_step("check staged changes", ["git", "diff", "--cached", "--quiet"], env, cwd=temp_dir)
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
            step = _run_step(name, command, env, cwd=temp_dir)
            steps.append(step)
            if step["exitCode"] != 0:
                return steps
    finally:
        if worktree_created:
            subprocess.run(["git", "worktree", "remove", "--force", str(temp_dir)], cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)
    return steps


def run_blog_job(
    force: bool = False,
    track: str = "tech",
    publish: bool = False,
    notify: bool = False,
    publish_paths: list[str] | None = None,
    publish_only: bool = False,
    collect_only: bool = False,
) -> tuple[int, dict[str, Any]]:
    if not _lock.acquire(blocking=False):
        return 409, {"ok": False, "error": "blog runner is already running"}

    env = os.environ.copy()
    _load_env_file(env)
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    env["BLOG_TRACK"] = track
    if force:
        env["FORCE_RUN"] = "1"

    steps: list[dict[str, Any]] = []
    try:
        commands = []
        if "BLOG_RUNNER_PYTHON" not in os.environ:
            commands.append(("prepare python venv", [SYSTEM_PYTHON_BIN, "-m", "venv", str(VENV_DIR)]))
        commands.append(("install dependencies", [PYTHON_BIN, "-m", "pip", "install", "-q", "-r", "scripts/requirements.txt"]))
        if collect_only:
            commands.append(("collect topic candidates", [PYTHON_BIN, "-B", "scripts/collect_topics.py", "--track", track]))
        else:
            commands.extend([
                ("verify humanizer gate", [PYTHON_BIN, "-B", "scripts/test_humanizer_gate.py"]),
                ("build hugo site", [HUGO_BIN, "--minify"]),
            ])
        if not collect_only and not publish_only:
            commands.insert(-1, ("write post with codex", [PYTHON_BIN, "scripts/trend_writer.py", "--track", track]))

        for name, command in commands:
            step = _run_step(name, command, env)
            steps.append(step)
            if step["exitCode"] != 0:
                alert = _collect_alert(track, False, failed_step=name) if collect_only else _blog_alert(track, False, [], failed_step=name)
                return 500, {"ok": False, "track": track, "collectOnly": collect_only, "failedStep": name, "alert": alert, "steps": steps}

        if collect_only:
            collect_step = next(step for step in steps if step["name"] == "collect topic candidates")
            collected_file = _collected_file(collect_step)
            candidate_count = _collected_count(collect_step)
            return 200, {
                "ok": True,
                "track": track,
                "collectOnly": True,
                "collectedFile": collected_file,
                "candidateCount": candidate_count,
                "alert": _collect_alert(track, True, collected_file, candidate_count),
                "steps": steps,
            }

        paths: list[str] = []
        if publish or publish_only:
            paths = list(publish_paths or [])
            if not publish_only:
                paths.extend(_created_files(next(step for step in steps if step["name"] == "write post with codex")))
            for step in _publish_blog_changes(track, paths, env):
                steps.append(step)
                if step["exitCode"] != 0:
                    return 500, {"ok": False, "track": track, "failedStep": step["name"], "createdFiles": paths, "alert": _blog_alert(track, False, paths, failed_step=step["name"]), "steps": steps}
            if notify:
                step = _send_telegram_notification(track, paths, env)
                steps.append(step)
                if step["exitCode"] != 0:
                    return 500, {"ok": False, "track": track, "failedStep": step["name"], "createdFiles": paths, "alert": _blog_alert(track, False, paths, failed_step=step["name"]), "steps": steps}

        return 200, {"ok": True, "track": track, "createdFiles": paths, "alert": _blog_alert(track, True, paths), "steps": steps}
    except subprocess.TimeoutExpired as exc:
        error = f"timeout after {TIMEOUT_SECONDS}s"
        alert = _collect_alert(track, False, error=error) if collect_only else _blog_alert(track, False, [], error=error)
        return 504, {"ok": False, "track": track, "collectOnly": collect_only, "error": error, "step": exc.cmd, "alert": alert, "steps": steps}
    except Exception as exc:
        alert = _collect_alert(track, False, error=str(exc)) if collect_only else _blog_alert(track, False, [], error=str(exc))
        return 500, {"ok": False, "track": track, "collectOnly": collect_only, "error": str(exc), "alert": alert, "steps": steps}
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
        if self.path == "/notify":
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

            text = str(body.get("text") or "").strip()
            if not text:
                _json_response(self, 400, {"ok": False, "error": "text is required"})
                return

            env = os.environ.copy()
            _load_env_file(env)
            try:
                _send_telegram_text(text, env)
            except Exception as exc:
                _json_response(self, 502, {"ok": False, "error": "telegram send failed", "errorType": exc.__class__.__name__})
                return

            _json_response(self, 200, {"ok": True})
            return

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
            collect_only=bool(body.get("collectOnly")),
            publish_paths=[str(path) for path in publish_paths],
        )
        _json_response(self, status, payload)


def main() -> None:
    if HOST not in {"127.0.0.1", "localhost", "::1"} and not TOKEN:
        raise SystemExit("BLOG_RUNNER_TOKEN is required when BLOG_RUNNER_HOST is not loopback")

    server = LocalThreadingHTTPServer((HOST, PORT), BlogRunnerHandler)
    print(f"n8n Codex blog runner listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
