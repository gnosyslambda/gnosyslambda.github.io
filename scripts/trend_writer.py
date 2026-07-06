#!/usr/bin/env python3
"""
trend_writer.py
================
해외 테크 블로그 RSS 피드를 수집하고, 로컬 Codex CLI를 통해
14년 차 백엔드 개발자 시각의 한국어 포스트를 생성하는 스크립트.

Usage:
    python scripts/trend_writer.py
"""

import os
import re
import sys
import argparse
import json
import random
import logging
import hashlib
import subprocess
import tempfile
import signal
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
FEEDS_PATH = SCRIPT_DIR / "feeds.json"
POSTS_DIR = REPO_ROOT / "content" / "posts"
REVIEW_DRAFTS_DIR = REPO_ROOT / "local_drafts" / "review"
SEEN_CACHE = SCRIPT_DIR / ".seen_articles.json"

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "xhigh")
CODEX_TIMEOUT_SECONDS = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "900"))
TOPICS_PER_RUN = min(10, max(1, int(os.environ.get("TOPICS_PER_RUN", "3"))))
POST_VARIANTS = min(5, max(1, int(os.environ.get("POST_VARIANTS", "5"))))
POST_REPAIR_VARIANTS = min(POST_VARIANTS, max(1, int(os.environ.get("POST_REPAIR_VARIANTS", "2"))))
POST_JUDGES = min(5, max(1, int(os.environ.get("POST_JUDGES", "2"))))
POST_MIN_SCORE = float(os.environ.get("POST_MIN_SCORE", "85"))
POST_REVIEW_MIN_SCORE = float(os.environ.get("POST_REVIEW_MIN_SCORE", "80"))
POST_POLISH_MIN_SCORE = float(os.environ.get("POST_POLISH_MIN_SCORE", "85"))
POST_MAX_ROUNDS = max(1, int(os.environ.get("POST_MAX_ROUNDS", "2")))
CANDIDATE_TIMEOUT_SECONDS = int(os.environ.get("CANDIDATE_TIMEOUT_SECONDS", str(CODEX_TIMEOUT_SECONDS)))
JUDGE_TIMEOUT_SECONDS = int(os.environ.get("JUDGE_TIMEOUT_SECONDS", str(CODEX_TIMEOUT_SECONDS)))
QUALITY_HISTORY_PATH = SCRIPT_DIR / ".quality_history.jsonl"
FETCH_WINDOW_HOURS = 336         # 최근 14일 기사 수집 (빅테크 블로그 발행 빈도 고려)
DEFAULT_TRACK = "tech"
TRACKS = {"tech", "issue"}
CURRENT_TRACK = DEFAULT_TRACK
QUALITY_HISTORY_MAX_ROWS = 1000
QUALITY_MEMORY_MAX_ROWS = 50
QUALITY_MEMORY_MAX_CHARS = 2000
QUALITY_STRATEGIES = (
    "community-first",
    "risk-analysis",
    "practical-explainer",
    "contrarian-check",
)
ISSUE_CODE_RULES = (
    ("single_source_summary", ("single_source_summary", "단일", "요약", "single", "source", "회사", "article")),
    ("weak_community_angle", ("weak_community_angle", "커뮤니티", "논쟁", "반응", "hn", "reddit", "github")),
    ("generic_conclusion", ("generic_conclusion", "결론", "일반적", "generic", "뻔", "추상")),
    ("missing_counterpoint", ("missing_counterpoint", "반론", "counter", "찬반", "균형", "우려")),
    ("thin_evidence", ("thin_evidence", "근거", "수치", "사례", "evidence", "출처")),
    ("stale_structure", ("stale_structure", "구조", "반복", "나열", "템플릿", "흐름", "고정", "획일", "천편일률")),
    ("ai_tone", ("ai_tone", "ai 말투", "챗봇", "기계적", "번역투", "상투적", "클리셰", "수사 의문문", "양비론")),
)
MAX_ARTICLES_TO_SCORE = 20       # LLM 혁신도 평가 최대 기사 수
MAX_BODY_CHARS = 10000           # 본문 최대 글자 수 (토큰 절약)
HTTP_TIMEOUT = 15                # 요청 타임아웃(초)
MAX_SUPPORTING_ARTICLES = 5      # 비슷한 주제로 묶을 추가 글 수
MAX_SUPPORTING_BODY_CHARS = 1800 # 보조 글 본문 최대 길이
MIN_SUPPORTING_SCORE = 4         # 대표 글과 연결할 최소 관련도 점수
SEEN_EXPIRE_DAYS = 60            # seen 항목 만료 기간 (60일 후 재선정 가능)
TOPIC_STOPWORDS = {
    "a", "an", "and", "any", "are", "as", "at", "be", "by", "enabling", "for", "from",
    "get", "how", "in", "into", "is", "it", "more", "new", "now", "of", "on", "our",
    "public", "the", "this", "to", "using", "we", "what", "with", "your",
    "amazon", "aws", "blog", "cloud", "developer", "developers", "development", "devops",
    "engineering", "infrastructure", "introducing", "june", "july", "roundup", "service",
    "services", "support", "supports", "update", "updates", "weekly",
    "개발", "기술", "블로그", "서비스", "인프라", "클라우드",
}
COMMUNITY_SIGNAL_SOURCES = {
    "Hacker News Best",
    "DEV Community",
    "GeekNews",
    "Lobsters",
    "Stack Overflow Blog",
    "GitHub Blog Engineering",
    "Reddit LocalLLaMA",
    "Reddit OpenAI",
    "Reddit MachineLearning",
    "Reddit Technology",
    "Reddit Programming",
    "YouTube Two Minute Papers",
    "YouTube AI Explained",
    "YouTube Computerphile",
    "YouTube Fireship",
    "GitHub Trending",
}
REFERENCE_ONLY_SOURCES = {
    "Netflix Tech Blog",
    "Uber Engineering",
    "Meta Engineering",
    "DoorDash Engineering",
    "Google Developers",
    "AWS Blog",
    "LinkedIn Engineering",
    "Airbnb Tech",
    "Cloudflare Blog",
    "Shopify Engineering",
    "Stripe Blog",
    "Discord Blog",
    "Spotify Engineering",
    "Pinterest Engineering",
    "Slack Engineering",
    "Twitter Engineering",
    "Dropbox Tech",
    "OpenAI Blog",
    "Anthropic News",
    "Anthropic Engineering",
    "Hugging Face Blog",
    "Databricks Blog",
}
COMMUNITY_SIGNAL_KEYWORDS = (
    "github", "hn", "hacker news", "lobsters", "reddit", "discussion", "debate",
    "issue", "issues", "proposal", "rfc", "incident", "outage", "postmortem",
    "vulnerability", "cve", "exploit", "breaking change", "release", "fork",
)
ARCHITECTURE_SIGNAL_KEYWORDS = (
    "architecture", "architectural", "distributed", "microservice", "monolith",
    "database", "postgres", "mysql", "cache", "queue", "event", "streaming",
    "kubernetes", "container", "runtime", "compiler", "observability", "security",
    "scalability", "performance", "latency", "consistency", "migration",
)
ISSUE_SIGNAL_KEYWORDS = (
    "ai", "model", "government", "policy", "regulation", "ban", "blocked",
    "lawsuit", "copyright", "privacy", "safety", "moderation", "platform",
    "community", "controversy", "backlash", "debate", "openai", "anthropic",
    "google", "meta", "x", "twitter", "reddit", "youtube", "tiktok", "security", "breach",
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
}

TRACK_FEED_SOURCES = {
    "issue": {
        "Hacker News Best",
        "GeekNews",
        "The Pragmatic Engineer",
        "Krebs on Security",
        "Schneier on Security",
        "TechCrunch",
        "a16z Blog",
        "OpenAI Blog",
        "Anthropic News",
        "Hugging Face Blog",
        "WIRED AI",
        "WIRED Ideas",
        "WIRED Security",
        "MIT News AI",
        "Reddit LocalLLaMA",
        "Reddit OpenAI",
        "Reddit MachineLearning",
        "Reddit Technology",
        "Reddit Programming",
        "YouTube Two Minute Papers",
        "YouTube AI Explained",
        "YouTube Computerphile",
        "YouTube Fireship",
        "GitHub Trending",
    },
    "tech": set(),
}

QUALITY_RUBRIC = """
100점 기준 평가목록:
- 독자 문제와 검색 의도 6
- 핵심 주장/관점 7
- 제목/메타/슬러그 적합성 4
- 사실관계/기술 정확성 10
- 시점/범위/전제 명시 4
- 근거/출처/검증 8
- 구체적 사례와 재현 가능한 맥락 7
- 보안/데이터/운영/플랫폼 리스크 6
- 실무 적용 가치 8
- 대안/트레이드오프/한계 5
- 초반 흡입력/갈등 또는 긴장 제시 5
- 사례에서 원칙으로 끌어올리는 힘 6
- 주제 맞춤 구조: 소제목 문구·순서·개수가 이 글의 주제에서 나온 설계인가 6
- 개성 있는 문체: 단정하는 문장, 짧은 문단, 사람이 쓴 리듬 5
- 문장 압축력과 한국어 자연스러움 6
- 결말 회수와 여운/행동 유도 7

실패 패턴 점검 목록 (평가 시 각 항목을 하나씩 확인하고, 발견하면 패턴 이름을 blocker 문장에 그대로 인용):
- stale_structure: 다른 글에도 그대로 붙일 수 있는 범용 소제목 세트를 순서까지 그대로 반복한 템플릿형 구조
- weak_community_angle: 커뮤니티/업계에서 실제로 갈린 반응 없이 "반응이 뜨겁다" 수준으로 뭉갠 서술
- thin_evidence: 핵심 주장에 수치·사례·출처가 붙지 않고 단정만 남은 서술
- generic_conclusion: 어느 주제에 붙여도 되는 결론("지켜볼 필요가 있다"류)으로 끝나는 마무리
- missing_counterpoint: 반대 관점이나 한계를 한 번도 다루지 않은 일방적 전개
- single_source_summary: 선정 글감 하나를 순서대로 요약한 수준에 그친 글
- ai_tone: 상투적 전환어 반복, 수사 의문문 남발, 알맹이 없는 양비론, 클리셰 마무리, 모든 문단의 불릿화 같은 기계적 문체

구조 다양성 원칙: 소제목 문구·순서·개수가 이전 글들과 다른 것은 결함이 아니라 가점 요인입니다.
고정 템플릿을 따르지 않았다는 이유로 감점하지 말고, 주제에 맞게 새로 설계된 구조인지로 평가하세요.
초안 작성 단계부터 85점 이상을 목표로 삼고, 부족한 항목은 본문 안에서 보강하세요.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a Korean blog post from RSS candidates.")
    parser.add_argument(
        "--track",
        choices=sorted(TRACKS),
        default=os.environ.get("BLOG_TRACK", DEFAULT_TRACK),
        help="Writing workflow track: issue checks trending community topics, tech checks broad technical topics.",
    )
    return parser.parse_args()


def configure_track(track: str) -> None:
    global CURRENT_TRACK, SEEN_CACHE

    CURRENT_TRACK = track
    if track == DEFAULT_TRACK:
        SEEN_CACHE = SCRIPT_DIR / ".seen_articles.json"
    else:
        SEEN_CACHE = SCRIPT_DIR / f".seen_articles_{track}.json"
    log.info(f"🧭 글쓰기 트랙: {CURRENT_TRACK} (seen: {SEEN_CACHE.name})")


def filter_feeds_for_track(feeds: list[dict]) -> list[dict]:
    selected: list[dict] = []

    for feed in feeds:
        feed_tracks = feed.get("tracks")
        if feed_tracks:
            if CURRENT_TRACK in feed_tracks:
                selected.append(feed)
            continue

        if CURRENT_TRACK == "tech" or feed.get("name") in TRACK_FEED_SOURCES.get(CURRENT_TRACK, set()):
            selected.append(feed)

    return selected


def track_context() -> str:
    if CURRENT_TRACK == "issue":
        return """
이번 글은 issue 트랙입니다.
- 무조건 논쟁만 찾지 말고, 커뮤니티와 업계에서 화제가 된 사건/정책/제품/플랫폼 변화를 찾습니다.
- 예: AI 모델 차단, 정부 규제, 플랫폼 정책 변경, 보안 사고, 저작권/개인정보 이슈, 커뮤니티 반응이 붙은 출시.
- 기술 해설보다 "왜 사람들이 반응했는가", "무엇이 불편하거나 중요한가", "앞으로 어떤 판단이 필요한가"를 중심에 둡니다.
- 날짜, 당사자, 정책 범위, 확인된 사실과 추정을 구분합니다.
"""

    return """
이번 글은 tech 트랙입니다.
- 특정 회사 기술 블로그 하나를 요약하지 말고, 여러 자료를 묶어 넓은 기술 주제를 잡습니다.
- 우선 주제: Kubernetes, 보안, 블록체인, 하네스/테스트 자동화, AI 에이전트, 인프라, 데이터베이스, 관측성.
- 회사 글은 사례/근거로만 쓰고, 제목과 결론은 주제/쟁점/실무 판단 중심으로 둡니다.
- 아키텍처, 운영 리스크, 대안, 도입 조건을 구체적으로 씁니다.
"""


def terminate_process_group(process: subprocess.Popen, grace_seconds: float = 5.0) -> None:
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


def article_uid(article: dict) -> str:
    raw = article.get("uid") or article.get("link") or article.get("title") or ""
    return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:16]


def load_quality_history(limit: int = QUALITY_HISTORY_MAX_ROWS) -> list[dict]:
    if not QUALITY_HISTORY_PATH.exists():
        return []
    rows: list[dict] = []
    for line in QUALITY_HISTORY_PATH.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def trim_quality_history() -> None:
    if not QUALITY_HISTORY_PATH.exists():
        return
    lines = QUALITY_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    if len(lines) <= QUALITY_HISTORY_MAX_ROWS and QUALITY_HISTORY_PATH.stat().st_size <= 5 * 1024 * 1024:
        return
    QUALITY_HISTORY_PATH.write_text("\n".join(lines[-QUALITY_HISTORY_MAX_ROWS:]) + "\n", encoding="utf-8")


def append_quality_record(record: dict) -> None:
    QUALITY_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with QUALITY_HISTORY_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    trim_quality_history()


def normalize_issue_codes(evaluations: list[dict]) -> list[str]:
    text = " ".join(
        str(evaluation.get("reason", ""))
        + " "
        + " ".join(str(issue) for issue in evaluation.get("critical_issues", []))
        for evaluation in evaluations
    ).lower()
    codes = [code for code, needles in ISSUE_CODE_RULES if any(needle.lower() in text for needle in needles)]
    return codes[:5] or ["unclear_feedback"]


def choose_quality_strategy(used_strategies: set[str] | None = None) -> str:
    used = used_strategies or set()
    rows = [row for row in load_quality_history() if row.get("track") == CURRENT_TRACK]
    counts = {strategy: 0 for strategy in QUALITY_STRATEGIES}
    rewards = {strategy: [] for strategy in QUALITY_STRATEGIES}
    for row in rows:
        strategy = row.get("strategy")
        if strategy not in counts:
            continue
        counts[strategy] += 1
        reward = row.get("reward_delta")
        if isinstance(reward, (int, float)):
            rewards[strategy].append(float(reward))

    available = [strategy for strategy in QUALITY_STRATEGIES if strategy not in used] or list(QUALITY_STRATEGIES)
    for strategy in available:
        if counts[strategy] < 3:
            return strategy
    if random.random() < 0.2:
        return random.choice(available)
    return max(
        available,
        key=lambda strategy: sum(rewards[strategy]) / len(rewards[strategy]) if rewards[strategy] else 0.0,
    )


def build_quality_prompt_memory() -> str:
    rows = [
        row
        for row in load_quality_history(QUALITY_MEMORY_MAX_ROWS * 4)
        if row.get("track") == CURRENT_TRACK
    ][-QUALITY_MEMORY_MAX_ROWS:]
    if not rows:
        return ""

    failure_counts: dict[str, int] = {}
    success_counts: dict[str, int] = {}
    for row in rows:
        scores = row.get("scores", {})
        avg = float(scores.get("avg", 0.0) or 0.0)
        if avg >= POST_MIN_SCORE:
            strategy = str(row.get("strategy", ""))
            if strategy:
                success_counts[strategy] = success_counts.get(strategy, 0) + 1
            continue
        for code in row.get("issue_codes", []):
            failure_counts[code] = failure_counts.get(code, 0) + 1

    lines: list[str] = []
    if failure_counts:
        top_failures = sorted(failure_counts.items(), key=lambda item: item[1], reverse=True)[:5]
        lines.append("최근 같은 트랙에서 감점된 유형: " + ", ".join(f"{code}({count})" for code, count in top_failures))
    if success_counts:
        top_successes = sorted(success_counts.items(), key=lambda item: item[1], reverse=True)[:3]
        lines.append("최근 통과 전략: " + ", ".join(f"{strategy}({count})" for strategy, count in top_successes))
    return "\n".join(lines)[:QUALITY_MEMORY_MAX_CHARS]


def build_quality_record(
    run_id: str,
    article: dict,
    candidate: dict,
    run_avg: float,
    status: str,
) -> dict:
    scores = [float(evaluation.get("score", 0.0) or 0.0) for evaluation in candidate.get("evaluations", [])]
    avg = float(candidate.get("score", 0.0) or 0.0)
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "track": CURRENT_TRACK,
        "round": candidate.get("round"),
        "variant": candidate.get("variant"),
        "strategy": candidate.get("strategy"),
        "source_uid": article_uid(article),
        "candidate_hash": hashlib.sha256(candidate.get("body", "").encode("utf-8")).hexdigest()[:16],
        "scores": {"avg": avg, "min": min(scores) if scores else 0.0, "per_judge": scores},
        "run_avg": run_avg,
        "reward_delta": avg - run_avg,
        "issue_codes": normalize_issue_codes(candidate.get("evaluations", [])),
        "status": status,
        "elapsed_seconds": round(float(candidate.get("elapsed_seconds", 0.0) or 0.0), 2),
        "saved_post_relpath": "",
    }


def post_structure() -> str:
    if CURRENT_TRACK == "issue":
        return """
구조는 고정 템플릿이 아니라 기능적 비트의 집합입니다. 아래 비트는 소제목 라벨이 아니라 이 글이 소화해야 할 역할 목록입니다.

1. 도입: 이 이슈 고유의 긴장이나 질문 하나를 첫 문단에서 구체적으로 세운다. "이런 이슈가 있었다"식 일반 서두 금지.
2. 사실 정리: 사건, 당사자, 시점, 정책/제품/플랫폼 범위를 구체적으로. 확인된 사실과 아직 추정인 해석을 분리.
3. 갈린 반응: 커뮤니티가 불편해한 지점과 기대한 지점을 신뢰, 권한, 비용, 사용성, 규제 리스크 같은 축으로 정리.
4. 관점: 이번 이슈를 하나의 원칙이나 반복되는 패턴으로 끌어올리고, 놓치면 안 되는 지점을 분명하게 단정.
5. 회수: 도입에서 세운 긴장을 결론에서 직접 회수해 답한다. 불릿 나열형 "정리" 섹션으로 끝내지 말 것.
6. ## 참고 자료: 실제로 본문에 사용한 링크만.

구조 설계 규칙:
- 소제목(H2)은 위 비트의 라벨을 그대로 옮기지 말고, 이 글의 주제에 대한 구체적 주장이나 질문으로 새로 짓는다.
- 비트 두 개를 한 섹션으로 합치거나 순서를 바꿔도 된다. H2는 참고 자료를 제외하고 3~5개 사이에서 주제가 요구하는 만큼만 쓴다.
- 범용 소제목("무슨 일이 있었나", "왜 사람들이 반응했나", "정리")을 재사용하지 않는다. 매 글의 소제목 구성은 달라야 한다.
"""

    return """
구조는 고정 템플릿이 아니라 기능적 비트의 집합입니다. 아래 비트는 소제목 라벨이 아니라 이 글이 소화해야 할 역할 목록입니다.

1. 도입: 이 주제 고유의 긴장이나 질문 하나를 첫 문단에서 구체적으로 세운다. 핵심을 한두 문장으로 요약한 인용구(>)로 열어도 좋다.
2. 맥락: 왜 지금 이 주제에 GitHub/Hacker News/개발자 커뮤니티에서 말이 붙는지, 실무 문제와 어떻게 연결되는지.
3. 갈리는 지점: 찬성/반대, 기대/우려, 기존 방식과 새 방식의 차이. 특정 회사 관점으로 몰지 말고 여러 레퍼런스를 주제별로 엮을 것.
4. 아키텍처: 시스템 설계, 데이터 흐름, 장애 격리, 성능, 운영 복잡도 관점. 코드 스니펫 또는 Mermaid 다이어그램 1개 필수.
5. 실무 조건: 도입 전에 확인할 조건, 트레이드오프, 실패하기 쉬운 지점.
6. 회수: 도입에서 세운 긴장을 결론에서 직접 회수해 답한다. 불릿 나열형 "정리" 섹션으로 끝내지 말 것.
7. ## 참고 자료: 실제로 본문에 사용한 링크만.

구조 설계 규칙:
- 소제목(H2)은 위 비트의 라벨을 그대로 옮기지 말고, 이 글의 주제에 대한 구체적 주장이나 질문으로 새로 짓는다. 검색될 법한 키워드를 소제목에 자연스럽게 담는다.
- 비트 두 개를 한 섹션으로 합치거나 순서를 바꿔도 된다. H2는 참고 자료를 제외하고 3~6개 사이에서 주제가 요구하는 만큼만 쓴다.
- 범용 소제목("왜 지금 이슈인가", "커뮤니티에서 갈리는 지점", "아키텍처 관점에서 볼 점", "실무에서 볼 점", "정리")을 재사용하지 않는다. 매 글의 소제목 구성은 달라야 한다.
"""


def run_codex_prompt(prompt: str, task_name: str, timeout: int = CODEX_TIMEOUT_SECONDS) -> str:
    """Run local Codex CLI and return only the final assistant message."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as output_file:
        output_path = Path(output_file.name)

    cmd = [
        CODEX_BIN,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "-C",
        str(REPO_ROOT),
        "--output-last-message",
        str(output_path),
        "--config",
        f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"',
    ]
    if CODEX_MODEL:
        cmd.extend(["--model", CODEX_MODEL])
    cmd.append("-")

    wrapped_prompt = f"""당신은 Mac mini에서 비대화형으로 실행되는 Codex 글쓰기 작업자입니다.
파일을 수정하거나 명령을 실행하지 말고, 아래 요청의 최종 출력만 답하세요.

{prompt}
"""

    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")

    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=REPO_ROOT,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(wrapped_prompt, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            terminate_process_group(process)
            raise RuntimeError(f"Codex {task_name} 시간이 초과됐습니다: {timeout}s") from exc

        if process.returncode != 0:
            stderr_tail = stderr.strip()[-2000:]
            stdout_tail = stdout.strip()[-2000:]
            raise RuntimeError(
                f"Codex {task_name} 실패(exit {process.returncode}). "
                f"stderr={stderr_tail!r} stdout={stdout_tail!r}"
            )

        response = output_path.read_text(encoding="utf-8").strip()
        if not response:
            response = stdout.strip()
        if not response:
            raise ValueError(f"Codex {task_name} empty response")
        return response
    except FileNotFoundError as exc:
        raise RuntimeError(f"Codex CLI를 찾지 못했습니다: {CODEX_BIN}") from exc
    finally:
        output_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# 1. RSS 피드 수집
# ─────────────────────────────────────────────
def load_feeds() -> list[dict]:
    with open(FEEDS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data["feeds"]


def fetch_recent_articles(feeds: list[dict], hours: int = FETCH_WINDOW_HOURS) -> list[dict]:
    """각 피드에서 최근 N시간 이내 기사를 수집한다."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    articles = []

    for feed_meta in feeds:
        feed_name = feed_meta["name"]
        feed_url = feed_meta["url"]
        log.info(f"📡 피드 수집 중: {feed_name}")

        try:
            parsed = feedparser.parse(feed_url, agent=HEADERS["User-Agent"])
        except Exception as e:
            log.warning(f"  ⚠️  파싱 실패 ({feed_name}): {e}")
            continue

        for entry in parsed.entries:
            pub_date = _parse_date(entry)
            if pub_date and pub_date < cutoff:
                continue  # 오래된 기사 스킵

            title = _clean_text(entry.get("title", ""))
            link = entry.get("link", "")
            article = {
                "source": feed_name,
                "title": title,
                "link": link,
                "summary": _clean_html(entry.get("summary", "")),
                "published": pub_date.isoformat() if pub_date else "",
                "tags": feed_meta.get("tags", []),
                "blog_category": feed_meta.get("blog_category", "기술 블로그"),
                "uid": _uid(link or title),
            }
            if article["title"] and article["link"]:
                articles.append(article)

    # 셔플로 항상 같은 기사가 상위에 오는 문제 방지
    random.shuffle(articles)
    log.info(f"✅ 총 {len(articles)}개 기사 수집 완료")
    return articles


def fetch_github_trending_articles() -> list[dict]:
    """Collect repositories from GitHub Trending for issue track."""
    if CURRENT_TRACK != "issue":
        return []

    try:
        resp = requests.get("https://github.com/trending", headers=HEADERS, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f" ⚠️ GitHub Trending 수집 실패: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    now = datetime.now(tz=timezone.utc).isoformat()
    articles: list[dict] = []

    for rank, row in enumerate(soup.select("article.Box-row")[:10], start=1):
        repo_link = row.select_one("h2 a")
        if not repo_link:
            continue

        repo = _clean_text(repo_link.get_text(" ", strip=True)).replace(" / ", "/")
        link = f"https://github.com{repo_link.get('href', '')}"
        description = _clean_text(row.select_one("p").get_text(" ", strip=True)) if row.select_one("p") else ""
        language = row.select_one("[itemprop=programmingLanguage]")
        stars = row.select_one('a[href$="/stargazers"]')
        today = row.select_one("span.d-inline-block.float-sm-right")
        language_text = _clean_text(language.get_text(" ", strip=True)) if language else ""
        stars_text = _clean_text(stars.get_text(" ", strip=True)) if stars else ""
        today_text = _clean_text(today.get_text(" ", strip=True)) if today else ""

        if repo and link:
            articles.append(
                {
                    "source": "GitHub Trending",
                    "title": f"#{rank} {repo}",
                    "link": link,
                    "summary": (
                        f"description: {description}\n"
                        f"language: {language_text}\n"
                        f"stars: {stars_text}\n"
                        f"today: {today_text}"
                    ),
                    "published": now,
                    "tags": ["github", "trending", "open-source", language_text.lower()],
                    "blog_category": "GitHub · 트렌딩",
                    "uid": _uid(link),
                }
            )

    log.info(f"✅ GitHub Trending {len(articles)}개 수집 완료")
    return articles


def _parse_date(entry) -> datetime | None:
    for field in ("published_parsed", "updated_parsed"):
        t = getattr(entry, field, None)
        if t:
            import time
            return datetime(*t[:6], tzinfo=timezone.utc)
    raw = entry.get("published") or entry.get("updated") or ""
    try:
        return dateparser.parse(raw).astimezone(timezone.utc) if raw else None
    except Exception:
        return None


def _clean_html(text: str) -> str:
    """HTML 태그 제거 후 plain text 반환."""
    soup = BeautifulSoup(text, "lxml")
    return _clean_text(soup.get_text())[:800]


def _clean_text(text: str) -> str:
    text = "".join(
        char
        for char in str(text)
        if unicodedata.category(char) not in {"Cf", "Cc"}
    )
    return re.sub(r"\s+", " ", text).strip()


def _uid(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _tokenize_korean_english(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-+.#]{1,}|[가-힣]{2,}", text)
        if token.lower() not in TOPIC_STOPWORDS
    }


# ─────────────────────────────────────────────
# 2. seen 캐시 관리 (타임스탬프 기반, 60일 만료)
# ─────────────────────────────────────────────
def load_seen() -> set[str]:
    """seen_articles.json에서 만료되지 않은 uid set을 반환한다.

    포맷: {"uid": "ISO timestamp"} — 60일 이상 된 항목은 자동 제외.
    하위 호환: 구버전 list 포맷도 지원.
    """
    if not SEEN_CACHE.exists():
        return set()

    data = json.loads(SEEN_CACHE.read_text(encoding="utf-8"))

    # 구버전 포맷 (list of uid strings)
    if isinstance(data, list):
        log.info("⚙️  seen_articles.json 구버전 포맷 감지 → 신버전으로 마이그레이션")
        now_ts = datetime.now(tz=timezone.utc).isoformat()
        data = {u: now_ts for u in data}
        SEEN_CACHE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # 만료 필터링
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=SEEN_EXPIRE_DAYS)
    active = {
        uid for uid, ts in data.items()
        if _parse_ts(ts) > cutoff
    }
    expired = len(data) - len(active)
    if expired:
        log.info(f"🗑️  만료된 seen 항목 {expired}개 제외")
    log.info(f"📋 seen 항목 수: {len(active)}개 (유효)")
    return active


def save_seen(seen: set[str]) -> None:
    """seen set을 {uid: timestamp} dict로 저장. 기존 타임스탬프 보존."""
    existing: dict[str, str] = {}
    if SEEN_CACHE.exists():
        data = json.loads(SEEN_CACHE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            existing = data

    now_ts = datetime.now(tz=timezone.utc).isoformat()
    result = {uid: existing.get(uid, now_ts) for uid in seen}
    SEEN_CACHE.write_text(json.dumps(result, indent=2), encoding="utf-8")


def _parse_ts(ts: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def _article_signal_text(article: dict) -> str:
    tags = " ".join(article.get("tags", []))
    return f"{article.get('source', '')} {article.get('title', '')} {article.get('summary', '')} {tags}".lower()


def _article_topic_priority(article: dict) -> tuple[int, str]:
    source = article.get("source", "")
    text = _article_signal_text(article)
    score = 0

    if CURRENT_TRACK == "issue":
        if source in TRACK_FEED_SOURCES["issue"]:
            score += 8
        if any(keyword in text for keyword in ISSUE_SIGNAL_KEYWORDS):
            score += 5
        if any(keyword in text for keyword in COMMUNITY_SIGNAL_KEYWORDS):
            score += 3
        if source in REFERENCE_ONLY_SOURCES and source not in TRACK_FEED_SOURCES["issue"]:
            score -= 4
        return score, article.get("published", "")

    if source in COMMUNITY_SIGNAL_SOURCES:
        score += 6
    if "github" in text:
        score += 4
    if any(keyword in text for keyword in COMMUNITY_SIGNAL_KEYWORDS):
        score += 3
    if any(keyword in text for keyword in ARCHITECTURE_SIGNAL_KEYWORDS):
        score += 3
    if source in REFERENCE_ONLY_SOURCES:
        score -= 2

    return score, article.get("published", "")


def _article_signal_label(article: dict) -> str:
    source = article.get("source", "")
    text = _article_signal_text(article)
    labels: list[str] = []

    if CURRENT_TRACK == "issue":
        if source in TRACK_FEED_SOURCES["issue"]:
            labels.append("issue-source")
        if any(keyword in text for keyword in ISSUE_SIGNAL_KEYWORDS):
            labels.append("issue")

    if source in COMMUNITY_SIGNAL_SOURCES:
        labels.append("community")
    if "github" in text:
        labels.append("github")
    if any(keyword in text for keyword in ARCHITECTURE_SIGNAL_KEYWORDS):
        labels.append("architecture")
    if any(keyword in text for keyword in COMMUNITY_SIGNAL_KEYWORDS):
        labels.append("discussion")
    if source in REFERENCE_ONLY_SOURCES:
        labels.append("reference")

    return ", ".join(labels) if labels else "general"


# ─────────────────────────────────────────────
# 3. Codex: 기사 혁신도 평가 → 최고 기사 선정
# ─────────────────────────────────────────────
def select_best_article(articles: list[dict]) -> dict | None:
    if not articles:
        return None

    # 커뮤니티/GitHub/아키텍처 신호가 있는 글감을 먼저 평가한다.
    candidates = sorted(articles, key=_article_topic_priority, reverse=True)[:MAX_ARTICLES_TO_SCORE]
    bullets = "\n".join(
        (
            f"{i+1}. [{a['source']}] {a['title']}\n"
            f"  신호: {_article_signal_label(a)}\n"
            f"  요약: {a['summary'][:260]}"
        )
        for i, a in enumerate(candidates)
    )

    prompt = f"""당신은 한국어 블로그 편집자입니다.
{track_context()}

{QUALITY_RUBRIC}

아래 RSS 후보를 검토하고, 현재 트랙에 맞는 글감 1개를 고르세요.
선택된 항목은 글의 시작점일 뿐이며, 원문은 근거 자료로만 참고합니다.

선정 기준 (우선순위 순):
1. 단일 기사 요약으로 끝나지 않고 하나의 관점으로 확장 가능한 주제
2. 최근 커뮤니티, 업계, 개발자, 정책권에서 반응이 붙을 만한 변화
3. 한국 독자가 검색할 법한 키워드가 분명한 주제
4. 출처가 약하거나 홍보성만 강한 후보는 낮게 평가

후보 목록:
{bullets}

응답 형식 (JSON만 출력, 다른 텍스트 없이):
{{
  "selected_index": 1,
  "reason": "왜 지금 커뮤니티형 주제로 쓸 가치가 있는지 한국어로 2-3문장",
  "topic_angle": "최종 글에서 잡을 쟁점/각도",
  "seo_keywords": ["검색될만한 핵심 키워드1", "키워드2", "키워드3"]
}}"""

    try:
        raw = run_codex_prompt(prompt, "기사 선정", timeout=CODEX_TIMEOUT_SECONDS)
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            log.warning("LLM 응답에서 JSON을 찾지 못했습니다.")
            return candidates[0]
        data = json.loads(json_match.group())
        idx = int(data.get("selected_index", 1)) - 1
        idx = max(0, min(idx, len(candidates) - 1))
        log.info(f"🏆 선정 기사: [{candidates[idx]['source']}] {candidates[idx]['title']}")
        log.info(f"   선정 이유: {data.get('reason', '')}")
        return candidates[idx]
    except Exception as e:
        log.warning(f"기사 선정 중 오류 발생, 첫 번째 기사 사용: {e}")
        return candidates[0]


# ─────────────────────────────────────────────
# 4. 원문 크롤링
# ─────────────────────────────────────────────
def fetch_article_body(url: str) -> tuple[str, str]:
    """URL에서 본문 텍스트와 커버 이미지 URL 추출. 실패 시 빈 문자열 반환."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        cover_image = _extract_cover_image(soup)

        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "ads"]):
            tag.decompose()

        for selector in ["article", "main", ".post-content", ".entry-content", "body"]:
            container = soup.select_one(selector)
            if container:
                text = re.sub(r"\s+", " ", container.get_text()).strip()
                if len(text) > 500:
                    return text[:MAX_BODY_CHARS], cover_image

        return re.sub(r"\s+", " ", soup.get_text()).strip()[:MAX_BODY_CHARS], cover_image
    except Exception as e:
        log.warning(f"본문 크롤링 실패 ({url}): {e}")
        return "", ""


def _extract_cover_image(soup) -> str:
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        return og_image["content"]

    twitter_image = soup.find("meta", attrs={"name": "twitter:image"})
    if twitter_image and twitter_image.get("content"):
        return twitter_image["content"]

    for img in soup.find_all("img"):
        src = img.get("src")
        if src and src.startswith("http") and not any(x in src.lower() for x in ["icon", "avatar", "logo", "pixel"]):
            return src

    return ""


def _article_topic_text(article: dict) -> str:
    return " ".join(
        [
            article.get("title", ""),
            article.get("summary", ""),
            article.get("category", ""),
            article.get("blog_category", ""),
            " ".join(article.get("tags", [])),
        ]
    )


def _similarity_score(primary: dict, candidate: dict) -> int:
    primary_tokens = _tokenize_korean_english(_article_topic_text(primary))
    candidate_tokens = _tokenize_korean_english(_article_topic_text(candidate))
    if not primary_tokens or not candidate_tokens:
        return 0

    primary_title_tokens = _tokenize_korean_english(primary.get("title", ""))
    candidate_title_tokens = _tokenize_korean_english(candidate.get("title", ""))
    token_overlap = primary_tokens & candidate_tokens
    title_overlap = primary_title_tokens & candidate_title_tokens
    anchor_overlap = primary_title_tokens & candidate_tokens
    primary_tags = {tag for tag in primary.get("tags", []) if tag.lower() not in TOPIC_STOPWORDS}
    candidate_tags = {tag for tag in candidate.get("tags", []) if tag.lower() not in TOPIC_STOPWORDS}
    tag_overlap = primary_tags & candidate_tags
    different_source = primary.get("source") != candidate.get("source")

    if not anchor_overlap:
        return 0

    return (
        len(anchor_overlap) * 5
        + len(title_overlap) * 6
        + len(token_overlap) * 2
        + len(tag_overlap) * 2
        + (1 if different_source else 0)
    )


def _is_same_topic(primary: dict, candidate: dict) -> bool:
    if article_uid(primary) == article_uid(candidate):
        return True
    if primary.get("link") and primary.get("link") == candidate.get("link"):
        return True

    primary_title_tokens = _tokenize_korean_english(primary.get("title", ""))
    candidate_title_tokens = _tokenize_korean_english(candidate.get("title", ""))
    if len(primary_title_tokens & candidate_title_tokens) >= 3:
        return True

    return _similarity_score(primary, candidate) >= 18


def select_articles_for_run(articles: list[dict], count: int) -> list[dict]:
    """Select multiple distinct topics for one run."""
    selected: list[dict] = []
    remaining = list(articles)

    while remaining and len(selected) < count:
        best = select_best_article(remaining)
        if not best:
            break

        selected.append(best)
        remaining = [
            article
            for article in remaining
            if not any(_is_same_topic(chosen, article) for chosen in selected)
        ]

    return selected


def select_supporting_articles_with_codex(primary: dict, candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []

    bullets = "\n".join(
        (
            f"{idx}. [{article['source']}] {article['title']}\n"
            f"요약: {article['summary'][:350]}\n"
            f"태그: {', '.join(article.get('tags', []))}"
        )
        for idx, article in enumerate(candidates, start=1)
    )
    prompt = f"""선정된 글감과 같은 커뮤니티/아키텍처 쟁점을 직접 보강할 레퍼런스를 고르세요.

선정된 글감:
- 제목: {primary['title']}
- 출처: {primary['source']}
- 요약: {primary['summary'][:500]}
- 태그: {', '.join(primary.get('tags', []))}

후보:
{bullets}

선정 기준:
- GitHub/커뮤니티 논쟁, 릴리스, 장애, 보안, 성능, 아키텍처 변화와 직접 연결되는 글을 우선 고르세요.
- 회사 블로그는 주장을 검증하거나 사례를 보강할 때만 선택하세요.
- 같은 회사, 같은 클라우드, 같은 AI/개발 카테고리라는 이유만으로 고르지 마세요.
- 선정된 글감의 핵심 기술 문제를 실제로 보강하거나 비교할 수 있는 글만 고르세요.
- 관련성이 애매하면 과감히 제외하세요. 0개여도 됩니다.
- 최대 {MAX_SUPPORTING_ARTICLES}개만 고르세요.

응답 형식(JSON만 출력):
{{
  "selected_indexes": [1, 3],
  "reason": "선정/제외 기준을 한 문장으로 설명"
}}"""

    try:
        raw = run_codex_prompt(prompt, "보조 레퍼런스 선정", timeout=CODEX_TIMEOUT_SECONDS)
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            raise ValueError("JSON 응답을 찾지 못했습니다.")
        data = json.loads(json_match.group())
        selected_indexes = data.get("selected_indexes", [])
        selected: list[dict] = []
        for raw_idx in selected_indexes[:MAX_SUPPORTING_ARTICLES]:
            try:
                idx = int(raw_idx) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(candidates):
                selected.append(candidates[idx])
        log.info(f"   보조 레퍼런스 선정 기준: {data.get('reason', '')}")
        return selected
    except Exception as e:
        log.warning(f"보조 레퍼런스 Codex 선정 실패, 점수 기반 후보 사용: {e}")
        return candidates[:MAX_SUPPORTING_ARTICLES]


def select_supporting_articles(primary: dict, articles: list[dict]) -> list[dict]:
    scored: list[tuple[int, str, dict]] = []

    for article in articles:
        if article["uid"] == primary["uid"]:
            continue

        score = _similarity_score(primary, article)
        if score >= MIN_SUPPORTING_SCORE:
            scored.append((score, article.get("published", ""), article))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    candidates = [article for _, _, article in scored[:20]]
    return select_supporting_articles_with_codex(primary, candidates)


def build_supporting_context(articles: list[dict]) -> str:
    if not articles:
        return "보조 레퍼런스 없음"

    blocks: list[str] = []
    for idx, article in enumerate(articles, start=1):
        article_body, _ = fetch_article_body(article["link"])
        excerpt = article_body[:MAX_SUPPORTING_BODY_CHARS] if article_body else article["summary"]
        tags = ", ".join(article.get("tags", [])) or "태그 없음"
        blocks.append(
            "\n".join(
                [
                    f"[보조 레퍼런스 {idx}]",
                    f"제목: {article['title']}",
                    f"출처: {article['source']}",
                    f"URL: {article['link']}",
                    f"카테고리: {article.get('blog_category', article.get('category', ''))}",
                    f"태그: {tags}",
                    f"요약: {article['summary']}",
                    f"발췌: {excerpt or '본문 확보 실패'}",
                ]
            )
        )

    return "\n\n".join(blocks)


# ─────────────────────────────────────────────
# 5. Codex: 페르소나 기반 한국어 포스트 생성
# ─────────────────────────────────────────────
def generate_post(
    article: dict,
    body: str,
    supporting_context: str,
    variant_index: int = 1,
    total_variants: int = 1,
    quality_feedback: str = "",
    strategy: str = "community-first",
) -> str:
    source_content = f"""[선정 글감 제목] {article['title']}
[출처] {article['source']}
[URL] {article['link']}
[요약] {article['summary']}

[선정 글감 내용]
{body if body else "본문을 가져오지 못했습니다. 요약 내용을 기반으로 작성해주세요."}"""
    variant_note = ""
    if total_variants > 1:
        variant_note = f"""
이번 출력은 후보 {variant_index}/{total_variants}입니다.
- 같은 자료에서 출발하되, 다른 후보와 제목감, 초반 긴장, 결론 관점을 다르게 잡으세요.
- 억지로 튀지 말고, 85점 이상 평가를 받을 수 있는 가장 설득력 있는 한 가지 관점에 집중하세요.
"""
    feedback_note = ""
    if quality_feedback:
        feedback_note = f"""
이전 라운드 judge 피드백입니다. 아래 문제를 본문 구조와 문장 안에서 직접 보완하세요.
{quality_feedback}
"""

    memory = build_quality_prompt_memory()
    memory_note = f"""
품질 메모리(본문에 언급하지 말고 편집 판단에만 반영):
{memory}

""" if memory else ""
    strategy_note = f"""
이번 후보의 편집 전략: {strategy}
- 이 전략명, judge, 점수, 이전 글, 피드백 같은 메타 표현은 본문에 쓰지 마세요.

"""

    persona_prompt = f"""{track_context()}

{QUALITY_RUBRIC}
{variant_note}
{feedback_note}

{memory_note}

{strategy_note}

여러 RSS 후보를 같은 트랙의 주제로 읽고, Codex가 편집자처럼 하나의 한국어 블로그 포스트로 재구성합니다.
선정된 글감은 주제의 진입점일 뿐입니다. 회사 기술 블로그나 공식 문서는 주장 검증과 사례 보강에만 사용합니다.
단일 기사 요약이 아니라, GitHub/개발자 커뮤니티에서 왜 말이 붙는지와 아키텍처적으로 무엇을 봐야 하는지를 중심으로 씁니다.

중요:
- 선정된 글감 하나만 요약하는 글처럼 쓰지 말 것
- 보조 레퍼런스가 있으면 최소 2개 이상을 본문 논지에 실제로 연결할 것
- 회사 블로그는 사례/근거 위치로만 두고, 글의 제목과 결론을 회사 중심으로 잡지 말 것
- 보조 레퍼런스가 없거나 관련성이 약하면 억지로 꾸며내지 말고 커뮤니티 쟁점 중심으로 쓸 것
- "A 기사에서는..., B 기사에서는..." 식의 목록 나열보다, 주제별로 편집해서 자연스럽게 엮을 것
- 겹치는 주장, 다른 관점, 실무에서 확인해야 할 지점을 분리해서 편집할 것

━━━ 글쓰기 방식 ━━━
1. **쟁점 내용 충실히 전달**: 선정된 글감과 보조 레퍼런스의 핵심 개념, 수치, 사례는 정확하게 소화해서 설명
2. **관점을 세우고 단정하기**: 자료를 근거로 하나의 판단을 평서문으로 분명히 말하고 그 판단 뒤에 선다. 모든 문장을 "~일 수도 있다", "~로 보인다"로 흐리지 말 것. 단, 단정의 근거는 반드시 선정 글감과 보조 레퍼런스 안에서 가져올 것 — 자료에 없는 경험이나 사례를 지어내지 말 것
3. **실무 관점 코멘트**: 도입 조건, 운영 리스크, 확인 포인트를 구체적으로 짚되, 직접 겪은 것처럼 쓰지 말 것 — 나이/연차/회사 이름도 절대 언급하지 말 것
4. **코드/다이어그램**: 원문 코드가 있으면 포함. 없으면 개념을 설명하는 Mermaid 다이어그램 1개 직접 작성

━━━ 절대 금지 ━━━
- "결론적으로", "요약하자면", "살펴보겠습니다", "중요합니다", "주목할 만합니다" 등 AI 느낌 나는 표현
- 뻔한 도입부("최근 X가 주목받고 있습니다", "X 시대가 도래했습니다")
- 근거 없는 수치·사례 창작
- 원문에도 없고 실제 경험도 아닌 내용을 마치 경험한 것처럼 쓰는 것
- 연차, 경력 연수 언급 ("N년 넘게", "수년간", "오랫동안" 등) — 절대 쓰지 말 것
- **굵은 글씨**를 단어/개념 강조 목적으로 남발하는 것 — 특히 `**'단어'**` 같은 패턴 절대 금지
  - 굵은 글씨는 소제목 안에서만, 또는 정말 핵심 명령어/코드 옆에서만 허용
  - 일반 문장 안에서 `**X**와 **Y**` 식으로 키워드를 굵게 처리하지 말 것
- 작은따옴표로 단어를 감싸는 패턴 (`'규모'`, `'정확성'`) — 그냥 단어 그대로 쓸 것
- 상투적 전환어 남발: 또한/게다가/뿐만 아니라/결론적으로/정리하자면/요컨대/다양한 관점에서 같은 표현을 문단마다 반복하는 것
- 알맹이 없는 양비론: "장점도 있고 단점도 있다"식으로 판단 없이 끝내는 것 — 근거가 기우는 쪽을 분명히 말할 것
- 수사 의문문 남발: "~일까?", "~이 아닐까?" 반복 사용 — 글 전체에서 최대 1회
- 클리셰 마무리 문구: "지켜볼 필요가 있다", "귀추가 주목된다", "앞으로가 기대된다" 등
- 모든 문단을 불릿 목록으로 정리하려는 강박 — 불릿은 진짜 목록에만 쓰고, 논지는 문장으로 전개할 것
- 문단 시작 패턴의 획일성: 연속된 문단을 같은 접속사/같은 문형으로 시작하는 것

━━━ SEO 최적화 ━━━
- 핵심 키워드를 본문 앞부분(첫 200자 이내)에 자연스럽게 포함
- H2/H3 소제목에 검색 키워드가 포함되도록 작성
- 독자가 실제로 검색할 법한 질문("왜 X를 써야 할까?", "X vs Y 차이점") 형식의 소제목 활용

━━━ 글쓰기 스타일 ━━━
- 확신 있는 문장: 주장을 평서문으로 단정하고, 근거를 바로 뒤에 붙인다. 조건이 필요하면 조건을 구체적으로 명시하고 얼버무리지 않는다
- 문단은 대부분 2~4문장. 강조하고 싶은 한 문장은 의도적으로 한 줄 문단으로 세운다
- 대구와 대조: 두 대상을 같은 문형으로 맞세우는 문장(예: "A는 X를 샀고, B는 X를 잃었다")을 핵심 지점에서 한두 번만 사용 — 매 문장에 쓰면 효과가 죽는다
- 막연한 추상어 대신 구체적 수치, 이름, 동작을 쓴다
- 불릿(-), 인용블록(>), 표는 필요한 곳에만 사용하고, 굵은 글씨는 남발하지 않는다
- 기술 용어 첫 등장 시 영문 병기: "서킷 브레이커(Circuit Breaker)"
- 분량: **최소 2,500자** (깊이 있게 쓸 것)

━━━ 포스트 구조 ━━━
{post_structure()}

━━━ 참고 자료(마지막 H2) 작성 규칙 ━━━
- [선정 글감] [{article['title']}]({article['link']}) — {article['source']}
- 보조 레퍼런스에서 실제로 활용한 링크를 `- [관련] 제목 — 출처` 형식으로 추가
- 보조 레퍼런스가 없으면 관련 링크를 추가하지 말 것

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[같은 주제 보조 레퍼런스]
{supporting_context}

[선정 글감]
{source_content}

위 구조와 방식에 따라 포스트 **본문만** 출력하세요. 제목(title)은 포함하지 마세요."""

    try:
        return run_codex_prompt(persona_prompt, "포스트 생성", timeout=CANDIDATE_TIMEOUT_SECONDS)
    except Exception as e:
        log.error(f"포스트 생성 실패: {e}")
        raise


def humanize_post(body: str) -> str:
    """Apply the humanizer gate before any post is saved."""
    prompt = f"""다음 마크다운 블로그 초안을 humanizer 스킬 기준으로 교정하세요.

규칙:
- 의미, 코드블록, 링크, 표, 제목 구조는 보존
- 과장된 의미 부여, 광고 문구, 막연한 출처, AI 단어, not only/but 패턴, 3개 나열 강박, em dash 남발, 굵은 글씨 남발, 이모지, 챗봇 말투, 지식 컷오프 문구, 뻔한 긍정 결론 제거
- 상투적 전환어 남발 제거: 또한/게다가/뿐만 아니라/결론적으로/정리하자면/요컨대/다양한 관점에서 같은 표현이 문단마다 반복되면 삭제하거나 문장 연결을 다시 쓰기
- 알맹이 없는 양비론 제거: "장점도 있고 단점도 있다"식 결론 없는 균형 잡기는 본문 근거가 기우는 쪽으로 정리
- 수사 의문문 정리: "~일까?", "~이 아닐까?"가 글 전체에서 2회 이상이면 평서문으로 교체
- 클리셰 마무리 제거: "지켜볼 필요가 있다", "귀추가 주목된다", "앞으로가 기대된다"류 문장은 도입의 질문에 답하는 구체적 문장으로 교체
- 불릿 강박 완화: 논지 전개까지 불릿으로 쪼갠 부분은 문장으로 풀기 (진짜 목록은 불릿 유지)
- 문단 시작 획일성 해소: 연속된 문단이 같은 접속사나 같은 문형으로 시작하면 시작을 다르게 바꾸기
- 새 사실, 새 수치, 새 경험담을 추가하지 말 것
- 문장은 실제 사람이 쓴 한국어 블로그처럼 구체적으로 정리
- 출력은 교정된 Markdown 본문만. frontmatter, 설명, 변경 요약은 쓰지 말 것

[초안]
{body}
"""
    try:
        humanized = run_codex_prompt(prompt, "humanizer", timeout=CANDIDATE_TIMEOUT_SECONDS)
        if not humanized:
            raise ValueError("empty humanizer response")
        return humanized
    except Exception as e:
        log.error(f"humanizer 적용 실패: {e}")
        raise


# ─────────────────────────────────────────────
# 6. Hugo frontmatter + 파일 저장
# ─────────────────────────────────────────────
def generate_post_candidate(
    article: dict,
    body: str,
    supporting_context: str,
    variant_index: int,
    total_variants: int,
    quality_feedback: str = "",
    strategy: str = "community-first",
) -> dict:
    started = time.monotonic()
    log.info(f"✍️ 후보 {variant_index}/{total_variants} 작성 시작")
    draft = generate_post(
        article,
        body,
        supporting_context,
        variant_index,
        total_variants,
        quality_feedback,
        strategy,
    )
    log.info(f"🧹 후보 {variant_index}/{total_variants} humanizer 적용 시작")
    return {
        "variant": variant_index,
        "strategy": strategy,
        "body": humanize_post(draft),
        "evaluations": [],
        "score": 0.0,
        "min_score": 0.0,
        "elapsed_seconds": time.monotonic() - started,
    }


def parse_post_evaluation(raw: str, judge_index: int) -> dict:
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not json_match:
        raise ValueError("judge response did not contain JSON")
    data = json.loads(json_match.group())
    score = max(0.0, min(100.0, float(data.get("score", 0))))
    blockers = data.get("blockers") or data.get("critical_issues", [])
    if not isinstance(blockers, list):
        blockers = [str(blockers)]
    blockers = [str(blocker).strip() for blocker in blockers if str(blocker).strip()]
    if blockers:
        score = min(score, POST_MIN_SCORE - 1)
    elif score < POST_MIN_SCORE:
        score = POST_MIN_SCORE
    critical_issues = data.get("critical_issues") or blockers
    if not isinstance(critical_issues, list):
        critical_issues = [str(critical_issues)]
    top_3_fixes = data.get("top_3_fixes", [])
    if not isinstance(top_3_fixes, list):
        top_3_fixes = [str(top_3_fixes)]
    do_not_change = data.get("do_not_change", [])
    if not isinstance(do_not_change, list):
        do_not_change = [str(do_not_change)]
    minor_suggestions = data.get("minor_suggestions", [])
    if not isinstance(minor_suggestions, list):
        minor_suggestions = [str(minor_suggestions)]
    raw_publishable = data.get("publishable", score >= POST_MIN_SCORE)
    if isinstance(raw_publishable, str):
        publishable = raw_publishable.strip().lower() in {"true", "yes", "1"}
    else:
        publishable = bool(raw_publishable)
    publishable = publishable and not blockers
    return {
        "judge": judge_index,
        "score": score,
        "publishable": bool(publishable),
        "blockers": blockers[:5],
        "top_3_fixes": [str(item) for item in top_3_fixes[:3]],
        "do_not_change": [str(item) for item in do_not_change[:3]],
        "minor_suggestions": [str(item) for item in minor_suggestions[:5]],
        "reason": str(data.get("reason", "")),
        "critical_issues": [str(issue) for issue in critical_issues[:5]],
    }


def evaluate_post(article: dict, body: str, supporting_context: str, judge_index: int) -> dict:
    prompt = f"""{track_context()}

당신은 블로그 글 품질을 평가하는 독립 judge 에이전트 {judge_index}입니다.
85점 이상은 완벽한 글이 아니라, 사람이 바로 게시해도 되는 수준입니다.
선호 차이, 더 좋아질 여지, 사소한 문장 polish는 85점 미만 사유가 아닙니다.
85점 미만을 주려면 게시를 막는 구체적 publish blocker를 blockers에 최소 1개 적어야 합니다.
publish blocker가 없다면 score는 반드시 85점 이상이고 publishable은 true입니다.
blockers는 사실 오류, 핵심 주장 검증 불가, 제목/본문 불일치, 구조 붕괴, 게시 품질 미달 문체, 출처가 필요한 핵심 주장 누락, 중복/환각/명백한 생성 흔적, 그리고 아래 루브릭의 실패 패턴에만 씁니다.
막연한 총평으로 점수를 매기지 말고, 루브릭의 실패 패턴 7가지(stale_structure, weak_community_angle, thin_evidence, generic_conclusion, missing_counterpoint, single_source_summary, ai_tone)를 하나씩 점검하세요.
패턴이 실제로 보이면 blocker 문장 안에 해당 패턴 이름을 그대로 인용하고, 본문의 어느 부분이 그런지 근거를 짧게 붙이세요.
소제목 구성이 다른 글과 다르거나 고정 템플릿을 따르지 않는 것은 감점 사유가 아닙니다. 주제에 맞게 구조를 새로 설계했다면 오히려 가점하세요.
본문에는 frontmatter와 제목이 없으므로 제목/메타/슬러그 항목은 본문이 검색 의도와 제목 생성에 충분한지로 평가하세요.

{QUALITY_RUBRIC}

선정 글감:
- 제목: {article['title']}
- 출처: {article['source']}
- URL: {article['link']}
- 요약: {article['summary']}

같은 주제 보조 레퍼런스:
{supporting_context[:5000]}

평가할 본문:
{body}

응답은 오직 JSON만 출력하세요.
{{
"score": 0,
"publishable": false,
"blockers": ["85점 미만이면 게시를 막는 구체적 결함. 없으면 빈 배열"],
"top_3_fixes": ["blocker를 제거하고 점수를 올릴 가장 큰 수정 3개 이하"],
"do_not_change": ["이미 좋은 부분 3개 이하"],
"minor_suggestions": ["게시를 막지 않는 선택적 개선점"],
"reason": "점수 근거를 한두 문장으로 요약",
"critical_issues": ["기존 호환용. blockers와 같은 내용을 넣되 없으면 빈 배열"]
}}"""
    try:
        raw = run_codex_prompt(prompt, f"judge {judge_index}", timeout=JUDGE_TIMEOUT_SECONDS)
        return parse_post_evaluation(raw, judge_index)
    except Exception as e:
        log.warning(f"judge {judge_index} 평가 실패: {e}")
        return {
            "judge": judge_index,
            "score": 0.0,
            "reason": f"evaluation failed: {e}",
            "critical_issues": ["judge evaluation failed"],
        }


def build_revision_feedback(candidate: dict) -> str:
    feedback = build_quality_feedback(candidate)
    score = float(candidate.get("score", 0.0) or 0.0)
    body = str(candidate.get("body", "")).strip()
    if score < POST_REVIEW_MIN_SCORE or not body:
        return feedback

    blockers: list[str] = []
    top_fixes: list[str] = []
    do_not_change: list[str] = []
    minor_suggestions: list[str] = []
    for evaluation in candidate.get("evaluations", []):
        blockers.extend(str(item).strip() for item in evaluation.get("blockers", []) if str(item).strip())
        top_fixes.extend(str(item).strip() for item in evaluation.get("top_3_fixes", []) if str(item).strip())
        do_not_change.extend(str(item).strip() for item in evaluation.get("do_not_change", []) if str(item).strip())
        minor_suggestions.extend(str(item).strip() for item in evaluation.get("minor_suggestions", []) if str(item).strip())

    def compact(items: list[str], limit: int) -> str:
        seen: set[str] = set()
        unique: list[str] = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            unique.append(item)
            if len(unique) >= limit:
                break
        return "\n".join(f"- {item}" for item in unique) or "- 없음"

    return f"""이전 라운드 최고 초안은 평균 {score:.1f}점입니다.
새 글을 처음부터 다시 쓰지 말고, 아래 초안을 유지하면서 publish blocker와 top fixes만 패치하세요.
이미 좋은 부분은 보존하고, minor suggestion은 blocker 수정 후 문장이 어색할 때만 반영하세요.
본문에는 judge, 점수, 이전 초안, 수정했다는 표현을 쓰지 마세요.

[publish blockers]
{compact(blockers, 5)}

[top fixes]
{compact(top_fixes, 3)}

[do not change]
{compact(do_not_change, 3)}

[minor suggestions]
{compact(minor_suggestions, 5)}

[judge 피드백]
{feedback}

[이전 최고 초안]
{body[:9000]}"""


def build_quality_gated_post(article: dict, body: str, supporting_context: str) -> tuple[str, dict]:
    log.info(
        f"🧪 품질 게이트: 후보 {POST_VARIANTS}개, judge {POST_JUDGES}개, "
        f"기준 {POST_MIN_SCORE:.1f}점, 최대 {POST_MAX_ROUNDS}라운드"
    )
    feedback = ""
    best_overall: dict | None = None
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{article_uid(article)}"

    for round_index in range(1, POST_MAX_ROUNDS + 1):
        log.info(f"🧪 품질 게이트 라운드 {round_index}/{POST_MAX_ROUNDS}")
        previous_has_blockers = any(
            evaluation.get("blockers")
            for evaluation in (best_overall or {}).get("evaluations", [])
        )
        is_polish_round = (
            round_index > 1
            and best_overall is not None
            and best_overall["score"] >= POST_POLISH_MIN_SCORE
            and not previous_has_blockers
        )
        variant_count = POST_VARIANTS if round_index == 1 else (1 if is_polish_round else POST_REPAIR_VARIANTS)
        if round_index > 1:
            mode = "polish" if is_polish_round else "repair"
            log.info(f"🔧 상위 초안 {mode} 라운드: 후보 {variant_count}개")
        candidates: list[dict] = []
        used_strategies: set[str] = set()
        strategies_by_index: dict[int, str] = {}
        for index in range(1, variant_count + 1):
            strategy = choose_quality_strategy(used_strategies)
            used_strategies.add(strategy)
            strategies_by_index[index] = strategy
        with ThreadPoolExecutor(max_workers=variant_count) as executor:
            futures = {
                executor.submit(
                    generate_post_candidate,
                    article,
                    body,
                    supporting_context,
                    index,
                    variant_count,
                    feedback,
                    strategies_by_index[index],
                ): index
                for index in range(1, variant_count + 1)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    candidates.append(future.result())
                    log.info(f"✅ 후보 {index}/{variant_count} 작성 완료")
                except Exception as e:
                    log.error(f"후보 {index}/{variant_count} 작성 실패: {e}")

        if not candidates:
            feedback = "이전 라운드는 모든 후보 작성에 실패했습니다. 더 짧고 안정적인 구조로 다시 작성하세요."
            continue

        max_workers = max(1, len(candidates) * POST_JUDGES)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(evaluate_post, article, candidate["body"], supporting_context, judge): candidate
                for candidate in candidates
                for judge in range(1, POST_JUDGES + 1)
            }
            for future in as_completed(futures):
                candidate = futures[future]
                candidate["evaluations"].append(future.result())

        for candidate in candidates:
            scores = [evaluation["score"] for evaluation in candidate["evaluations"]]
            candidate["score"] = sum(scores) / len(scores) if scores else 0.0
            candidate["min_score"] = min(scores) if scores else 0.0
            candidate["round"] = round_index
            score_text = ", ".join(f"{score:.1f}" for score in scores) or "none"
            log.info(
                f"🧪 후보 {round_index}-{candidate['variant']} 평가: "
                f"평균 {candidate['score']:.1f}점 "
                f"(judge: {score_text}, 최저 {candidate['min_score']:.1f}점)"
            )

        candidates.sort(key=lambda item: (item["score"], item["min_score"]), reverse=True)
        best = candidates[0]
        review_paths = save_review_candidates(article, candidates)
        if review_paths:
            log.info(f"🗂️ 라운드 {round_index} 수정 후보 {len(review_paths)}개 보존")

        if best_overall is None or (best["score"], best["min_score"]) > (
            best_overall["score"],
            best_overall["min_score"],
        ):
            best_overall = best

        run_avg = sum(candidate["score"] for candidate in candidates) / len(candidates)
        for candidate in candidates:
            status = "selected" if candidate is best and best["score"] >= POST_MIN_SCORE else "passed"
            if candidate["score"] < POST_MIN_SCORE:
                status = "failed"
            try:
                append_quality_record(build_quality_record(run_id, article, candidate, run_avg, status))
            except Exception as exc:
                log.warning(f"품질 기록 저장 실패: {exc}")

        if best["score"] >= POST_MIN_SCORE:
            log.info(
                f"🏁 후보 {round_index}-{best['variant']} 선택: 평균 {best['score']:.1f}점"
            )
            return best["body"], best

        feedback = build_revision_feedback(best)
        log.warning(
            f"⚠️ 라운드 {round_index} 최고 후보 {best['score']:.1f}점; "
            "judge 피드백으로 다음 라운드 재작성"
        )

    if best_overall:
        raise RuntimeError(
            f"quality gate failed: best candidate scored {best_overall['score']:.1f}, "
            f"required {POST_MIN_SCORE:.1f}. {build_quality_feedback(best_overall)}"
        )
    raise RuntimeError("all post variants failed")


def build_quality_feedback(candidate: dict) -> str:
    evaluations = candidate.get("evaluations") or []
    if not evaluations:
        return "judge 평가 결과가 없습니다. 단일 출처 의존, 근거 부족, 결론 약함을 우선 점검하세요."

    lines: list[str] = []
    for evaluation in evaluations:
        judge = evaluation.get("judge", "?")
        try:
            score = float(evaluation.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0

        reason = str(evaluation.get("reason", "")).strip()
        issues = evaluation.get("critical_issues") or []
        if not isinstance(issues, list):
            issues = [issues]
        issue_text = "; ".join(str(issue).strip() for issue in issues if str(issue).strip())

        parts = [f"judge {judge}: {score:.1f}점"]
        if reason:
            parts.append(f"근거: {reason}")
        if issue_text:
            parts.append(f"보완: {issue_text}")
        lines.append(" / ".join(parts))

    return "\n".join(f"- {line}" for line in lines)


def save_review_candidate(article: dict, candidate: dict) -> Path:
    REVIEW_DRAFTS_DIR.mkdir(parents=True, exist_ok=True)

    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(tz=kst)
    date_prefix = now_kst.strftime("%Y-%m-%d")
    base_slug = slugify(article.get("title", "")) or article_uid(article)
    candidate_hash = hashlib.sha256(candidate["body"].encode("utf-8")).hexdigest()[:10]
    filename = (
        f"{date_prefix}-{base_slug[:42]}-"
        f"r{candidate.get('round', 0)}v{candidate.get('variant', 0)}-{candidate_hash}.md"
    )
    filepath = REVIEW_DRAFTS_DIR / filename

    feedback = build_quality_feedback(candidate)
    content = f"""---
draft_review: true
track: {yaml_quote(CURRENT_TRACK)}
score: {candidate.get('score', 0):.1f}
min_score: {candidate.get('min_score', 0):.1f}
round: {candidate.get('round', 0)}
variant: {candidate.get('variant', 0)}
strategy: {yaml_quote(candidate.get('strategy', ''))}
source:
  name: {yaml_quote(article.get('source', ''))}
  url: {yaml_quote(article.get('link', ''))}
  title: {yaml_quote(article.get('title', ''))}
saved_at: {yaml_quote(now_kst.strftime("%Y-%m-%dT%H:%M:%S+09:00"))}
---

# 수정 후보: {article.get('title', '')}

## Judge Feedback

{feedback or '- judge feedback unavailable'}

---

{candidate["body"]}
"""
    filepath.write_text(content, encoding="utf-8")
    log.info(f"🗂️ 80점 이상 수정 후보 저장: {filepath}")
    return filepath


def save_review_candidates(article: dict, candidates: list[dict]) -> list[Path]:
    saved: list[Path] = []
    seen_hashes: set[str] = set()

    for candidate in candidates:
        score = float(candidate.get("score", 0.0))
        if not (POST_REVIEW_MIN_SCORE <= score < POST_MIN_SCORE):
            continue

        candidate_hash = hashlib.sha256(candidate["body"].encode("utf-8")).hexdigest()[:10]
        if candidate_hash in seen_hashes:
            continue
        seen_hashes.add(candidate_hash)

        saved.append(save_review_candidate(article, candidate))

    return saved


def build_title_and_slug(article: dict, body: str, supporting_context: str) -> dict:
    prompt = f"""{track_context()}

{QUALITY_RUBRIC}

아래 선정 글감과 보조 레퍼런스를 함께 읽고, 최종 편집 주제에 맞는 다음 4가지를 JSON 형식으로 생성해주세요.
**목표: 현재 트랙에 맞는 주제를 한국 독자가 검색할 법한 제목으로 정리하세요.**

1. **title**: 검색 노출에 최적화된 한국어 제목
- 독자가 실제로 검색할 법한 핵심 키워드를 제목 앞쪽에 배치
- 회사명이나 원문 제목을 그대로 앞세우지 말고, 주제의 쟁점과 관점을 제목에 반영
- 최대 40자, 구체적이고 명확하게 (예: "쿠버네티스 스케줄러 동작 원리 완전 정리")
2. **slug**: 검색 노출을 위한 영문 SEO 슬러그
   - 소문자 + 하이픈만, 3~6단어, 핵심 기술 키워드 포함
3. **keywords**: 이 글로 유입될 수 있는 검색 키워드 7~10개
   - 한국어 검색어 + 영문 기술 용어 혼합
   - 구체적인 롱테일 키워드 포함 (예: "쿠버네티스 파드 스케줄링", "kubernetes scheduler 동작")
4. **description**: 검색 결과 스니펫에 노출될 메타 설명 (1~2문장, 160자 이내)
   - 핵심 키워드 자연스럽게 포함, 클릭을 유도하는 문장

선정 글감 제목: {article['title']}
선정 글감 요약: {article['summary'][:300]}
선정 글감 일부:
{body[:2500]}

같은 주제 보조 레퍼런스:
{supporting_context[:2500]}

응답 형식 (오직 JSON만 출력):
{{
  "title": "SEO 최적화된 한국어 제목",
  "slug": "seo-optimized-slug",
  "keywords": ["한국어키워드1", "keyword2", "롱테일 키워드3"],
  "description": "검색 스니펫용 설명..."
}}"""
    try:
        raw = run_codex_prompt(prompt, "메타데이터 생성", timeout=CODEX_TIMEOUT_SECONDS)
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception as e:
        log.warning(f"메타데이터 생성 실패: {e}")

    return {
        "title": article["title"],
        "slug": "",
        "keywords": ["tech", "development", "backend"],
        "description": article["summary"][:150]
    }


def build_tags(article: dict, new_keywords: list[str]) -> list[str]:
    FIXED_TAGS = ["백엔드", "아키텍처", "개발"]
    # 피드 태그 중 의미있는 것만 (영문 기술 용어 위주, 최대 3개)
    feed_tags = [t for t in article.get("tags", []) if len(t) > 2][:3]
    # LLM 키워드 중 한국어 키워드 우선, 최대 4개
    kr_keywords = [k for k in new_keywords if re.search(r"[가-힣]", k)][:4]
    combined = list(dict.fromkeys(FIXED_TAGS + kr_keywords + feed_tags))
    return [t.replace(" ", "-") for t in combined[:8]]


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")[:60]


def yaml_quote(value: object) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def save_post(meta: dict, article: dict, body: str) -> Path:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(tz=kst)
    date_str = now_kst.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    date_prefix = now_kst.strftime("%Y-%m-%d")

    title = meta["title"]
    slug = meta.get("slug") or slugify(title) or slugify(article["title"])
    slug = slugify(slug)

    filename = f"{date_prefix}-{slug}.md"
    filepath = POSTS_DIR / filename

    counter = 1
    while filepath.exists():
        filepath = POSTS_DIR / f"{date_prefix}-{slug}-{counter}.md"
        counter += 1

    tags_yaml = "\n".join(f"  - {yaml_quote(t)}" for t in build_tags(article, meta.get("keywords", [])))
    description = meta.get("description", article["summary"][:150])
    frontmatter = f"""---
date: {yaml_quote(date_str)}
draft: false
title: {yaml_quote(title)}
tags:
{tags_yaml}
categories:
  - {yaml_quote(article.get('blog_category', '기술 블로그'))}
description: {yaml_quote(description)}
source:
  name: {yaml_quote(article.get('source', ''))}
  url: {yaml_quote(article.get('link', ''))}
  title: {yaml_quote(article.get('title', ''))}
cover:
  image: {yaml_quote(meta.get('cover_image', ''))}
  alt: "Cover image"
  relative: false
showToc: true
TocOpen: true
---

"""

    content = frontmatter + body
    filepath.write_text(content, encoding="utf-8")
    log.info(f"💾 포스트 저장 완료: {filepath}")
    return filepath


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
POST_COOLDOWN_MINUTES = 460  # 이 시간 이내에 이미 포스트가 생성됐으면 스킵 (8시간 - 여유 20분)


def _check_cooldown() -> None:
    """최근 커밋 중 content/posts/ 변경이 55분 이내에 있으면 스킵."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--since", f"{POST_COOLDOWN_MINUTES} minutes ago",
             "--", "content/posts/"],
            capture_output=True, text=True, cwd=REPO_ROOT
        )
        recent_commits = result.stdout.strip()
        if recent_commits:
            log.info(f"⏭️  최근 {POST_COOLDOWN_MINUTES}분 이내 포스트 커밋 존재 — 이번 실행은 건너뜁니다.")
            log.info(f"   커밋: {recent_commits.splitlines()[0]}")
            sys.exit(0)
        log.info(f"⏱️  {POST_COOLDOWN_MINUTES}분 이내 포스트 없음 — 새 포스트 생성 시작")
    except Exception as e:
        log.warning(f"쿨다운 체크 실패 (무시하고 계속): {e}")


DAILY_POST_LIMIT = int(os.environ.get("TREND_DAILY_POST_LIMIT", "2"))


def _check_daily_limit() -> None:
    """오늘(KST) 발행 수가 일일 상한에 도달했으면 이번 실행을 건너뜁니다."""
    kst = timezone(timedelta(hours=9))
    today = datetime.now(tz=kst).strftime("%Y-%m-%d")
    try:
        count = len(list(POSTS_DIR.glob(f"{today}-*.md")))
    except Exception as exc:
        log.warning(f"일일 상한 체크 실패 (무시하고 계속): {exc}")
        return
    if count >= DAILY_POST_LIMIT:
        log.info(f"⏭️  오늘 이미 {count}개 발행 (일일 상한 {DAILY_POST_LIMIT}) — 이번 실행은 건너뜁니다.")
        sys.exit(0)
    log.info(f"📅 오늘 발행 {count}/{DAILY_POST_LIMIT}개 — 계속")


def create_post_for_article(article: dict, articles: list[dict]) -> Path:
    log.info(f"🧵 주제 작성 시작: [{article['source']}] {article['title']}")

    # 5. 본문 크롤링
    log.info(f"🌐 본문 크롤링: {article['link']}")
    body_raw, cover_image = fetch_article_body(article["link"])
    log.info(f"   추출 길이: {len(body_raw)}자 / 커버 이미지: {'O' if cover_image else 'X'}")

    supporting_articles = select_supporting_articles(article, articles)
    supporting_context = build_supporting_context(supporting_articles)
    log.info(f"📚 보조 레퍼런스 {len(supporting_articles)}건 확보")
    for supporting_article in supporting_articles:
        log.info(f"  - [{supporting_article['source']}] {supporting_article['title']}")

    # 6. 메타데이터 생성
    log.info("📝 메타데이터(제목, 슬러그, 커스텀 SEO 키워드) 생성 중...")
    meta = build_title_and_slug(article, body_raw, supporting_context)
    meta["cover_image"] = cover_image
    log.info(f"📝 생성된 제목: {meta['title']}")
    log.info(f"🔗 SEO 슬러그: {meta['slug']}")

    # 7. 포스트 본문 생성
    log.info("✍️  포스트 작성 중 (선정 글감 + 보조 레퍼런스 기반 전문 분석)...")
    post_body, quality_result = build_quality_gated_post(article, body_raw, supporting_context)
    log.info(
        f"✅ 품질 게이트 통과: 후보 {quality_result['variant']} "
        f"평균 {quality_result['score']:.1f}점"
    )

    # 9. 파일 저장
    return save_post(meta, article, post_body)


def main():
    args = parse_args()
    configure_track(args.track)

    model_label = f" ({CODEX_MODEL})" if CODEX_MODEL else ""
    log.info(f"🤖 Codex CLI 사용: {CODEX_BIN}{model_label}")
    quality_variants_per_topic = POST_VARIANTS + max(0, POST_MAX_ROUNDS - 1) * POST_REPAIR_VARIANTS
    max_quality_calls = TOPICS_PER_RUN * quality_variants_per_topic * (2 + POST_JUDGES)
    log.info(
        f"⚙️ 품질 설정: topics={TOPICS_PER_RUN}, variants={POST_VARIANTS}, "
        f"repair_variants={POST_REPAIR_VARIANTS}, judges={POST_JUDGES}, "
        f"rounds={POST_MAX_ROUNDS}, polish_min={POST_POLISH_MIN_SCORE:.1f}, "
        f"candidate_timeout={CANDIDATE_TIMEOUT_SECONDS}s, "
        f"judge_timeout={JUDGE_TIMEOUT_SECONDS}s, max_codex_calls≈{max_quality_calls + (TOPICS_PER_RUN * 3)}"
    )

    # 0. 쿨다운 + 일일 상한 체크: FORCE_RUN 환경변수가 있으면 스킵
    if not os.environ.get("FORCE_RUN"):
        _check_cooldown()
        _check_daily_limit()

    # 1. seen 로드 (타임스탬프 기반, 만료된 항목 자동 제외)
    seen = load_seen()

    # 2. RSS 수집 (14일 윈도우)
    feeds = filter_feeds_for_track(load_feeds())
    log.info(f"🧾 {CURRENT_TRACK} 트랙 피드: {len(feeds)}개")
    articles = fetch_recent_articles(feeds)
    articles.extend(fetch_github_trending_articles())

    if not articles:
        log.warning("⚠️  최근 기사를 찾지 못했습니다. 윈도우를 28일로 확장합니다.")
        articles = fetch_recent_articles(feeds, hours=672)
        articles.extend(fetch_github_trending_articles())

    # 3. 미처리 기사 필터링
    fresh = [a for a in articles if a["uid"] not in seen]
    log.info(f"📰 fresh 기사: {len(fresh)}개 / 전체: {len(articles)}개")

    # fresh 소진 시: seen 초기화 후 전체 articles에서 재선정 (무조건 1개 생성 보장)
    if not fresh:
        log.warning("⚠️  처리할 새 기사가 없습니다. seen을 초기화하고 전체 기사에서 재선정합니다.")
        seen.clear()
        fresh = articles
        if not fresh:
            log.error("❌ RSS에서 수집된 기사가 없어 포스팅을 건너뜁니다.")
            sys.exit(0)

    # 4. 서로 다른 주제 선정 (셔플된 후보 풀에서)
    selected_articles = select_articles_for_run(fresh, TOPICS_PER_RUN)
    if not selected_articles:
        log.error("❌ 포스팅할 기사를 선정하지 못했습니다.")
        sys.exit(1)

    log.info(f"🧵 이번 실행 선정 주제: {len(selected_articles)}개 / 목표 {TOPICS_PER_RUN}개")
    saved_paths: list[Path] = []
    failed_topics: list[str] = []

    for index, article in enumerate(selected_articles, start=1):
        log.info(f"🧵 주제 {index}/{len(selected_articles)} 처리")
        try:
            saved_path = create_post_for_article(article, articles)
        except Exception as exc:
            failed_topics.append(f"[{article['source']}] {article['title']}: {exc}")
            log.error(f"❌ 주제 {index}/{len(selected_articles)} 생성 실패: {exc}")
            continue

        saved_paths.append(saved_path)
        seen.add(article["uid"])
        save_seen(seen)
        log.info(f"🎉 주제 {index}/{len(selected_articles)} 완료: {saved_path}")
        print(f"CREATED_FILE={saved_path}")

    if failed_topics:
        log.warning("⚠️ 실패한 주제:")
        for failed_topic in failed_topics:
            log.warning(f"  - {failed_topic}")

    if not saved_paths:
        raise RuntimeError("all selected topics failed quality gate")

    log.info(f"🎉 완료! 생성된 파일 {len(saved_paths)}개")
    print("CREATED_FILES=" + ",".join(str(path) for path in saved_paths))


if __name__ == "__main__":
    main()
