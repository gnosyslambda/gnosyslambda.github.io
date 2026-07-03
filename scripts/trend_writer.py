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
import json
import random
import logging
import hashlib
import subprocess
import tempfile
import unicodedata
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
SEEN_CACHE = SCRIPT_DIR / ".seen_articles.json"

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_MODEL = os.environ.get("CODEX_MODEL", "")
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "medium")
CODEX_TIMEOUT_SECONDS = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "900"))
FETCH_WINDOW_HOURS = 336         # 최근 14일 기사 수집 (빅테크 블로그 발행 빈도 고려)
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
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
}


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
        result = subprocess.run(
            cmd,
            input=wrapped_prompt,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr.strip()[-2000:]
            stdout_tail = result.stdout.strip()[-2000:]
            raise RuntimeError(
                f"Codex {task_name} 실패(exit {result.returncode}). "
                f"stderr={stderr_tail!r} stdout={stdout_tail!r}"
            )

        response = output_path.read_text(encoding="utf-8").strip()
        if not response:
            response = result.stdout.strip()
        if not response:
            raise ValueError(f"Codex {task_name} empty response")
        return response
    except FileNotFoundError as exc:
        raise RuntimeError(f"Codex CLI를 찾지 못했습니다: {CODEX_BIN}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Codex {task_name} 시간이 초과됐습니다: {timeout}s") from exc
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

    prompt = f"""당신은 한국어 기술 블로그 편집자입니다.
아래 RSS 후보를 검토하고, 단일 회사 블로그 요약이 아니라 **커뮤니티에서 이슈화된 기술 주제 1개**를 고르세요.
선택된 항목은 글의 시작점일 뿐이며, 회사 블로그는 근거 자료로만 참고합니다.

선정 기준 (우선순위 순):
1. GitHub, Hacker News, GeekNews, DEV, Stack Overflow 등 개발자 커뮤니티에서 말이 붙을 만한 주제
2. 최근 아키텍처 변화, 오픈소스 릴리스/논쟁, 장애/보안/성능/런타임 변화
3. 한국 개발자가 검색할 법한 키워드: "Kubernetes", "PostgreSQL", "Redis", "LLM infra", "CI/CD", "보안 취약점", "아키텍처"
4. 회사 기술 블로그는 단독 홍보/요약감이면 낮게 평가하고, 커뮤니티 쟁점과 연결될 때만 선택

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
def generate_post(article: dict, body: str, supporting_context: str) -> str:
    source_content = f"""[선정 글감 제목] {article['title']}
[출처] {article['source']}
[URL] {article['link']}
[요약] {article['summary']}

[선정 글감 내용]
{body if body else "본문을 가져오지 못했습니다. 요약 내용을 기반으로 작성해주세요."}"""

    persona_prompt = f"""여러 RSS 후보를 같은 기술 쟁점으로 읽고, Codex가 편집자처럼 하나의 한국어 기술 블로그 포스트로 재구성합니다.
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
2. **나만의 시각 추가**: 자료 묶음을 읽고 든 생각, 실무에서 겪을 법한 유사 상황, 동의하거나 의문을 가진 부분을 자연스럽게 녹임
3. **실무 관점 코멘트**: "실제로 이런 상황에서는", "현업에서 비슷한 고민을 하다 보면" 같은 자연스러운 코멘트 삽입 — 나이/연차/회사 이름은 절대 언급하지 말 것
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

━━━ SEO 최적화 ━━━
- 핵심 키워드를 본문 앞부분(첫 200자 이내)에 자연스럽게 포함
- H2/H3 소제목에 검색 키워드가 포함되도록 작성
- 독자가 실제로 검색할 법한 질문("왜 X를 써야 할까?", "X vs Y 차이점") 형식의 소제목 활용

━━━ 글쓰기 스타일 ━━━
- 문단 호흡 짧게 (한 문단 3문장 이내)
- 불릿(-), **굵은 글씨**, 인용블록(>), 표 적극 활용
- 기술 용어 첫 등장 시 영문 병기: "서킷 브레이커(Circuit Breaker)"
- 분량: **최소 2,500자** (깊이 있게 쓸 것)

━━━ 포스트 구조 ━━━

> **한 줄 요약** — 이 글에서 말하고 싶은 핵심을 딱 한두 문장으로

## 왜 지금 이슈인가
- GitHub, Hacker News, 개발자 커뮤니티에서 말이 붙을 만한 배경
- 단순 뉴스가 아니라 실무 문제와 어떻게 연결되는지

## 커뮤니티에서 갈리는 지점
- 찬성/반대, 기대/우려, 기존 방식과 새 방식의 차이를 정리
- 특정 회사 관점으로 몰지 말고 여러 레퍼런스를 주제별로 엮을 것

## 아키텍처 관점에서 볼 점
- 시스템 설계, 데이터 흐름, 장애 격리, 성능, 운영 복잡도 관점으로 설명
- 코드 스니펫 또는 Mermaid 다이어그램 포함 **[필수]**

## 실무에서 볼 점
- 도입 전에 확인할 조건, 트레이드오프, 실패하기 쉬운 지점
- 비슷한 상황을 겪어본 경험이 있다면 과장 없이 자연스럽게 언급

## 정리
- 핵심 메시지를 간결하게 마무리
- 독자가 당장 확인해볼 것 한 가지

## 참고 자료
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
        return run_codex_prompt(persona_prompt, "포스트 생성", timeout=CODEX_TIMEOUT_SECONDS)
    except Exception as e:
        log.error(f"포스트 생성 실패: {e}")
        raise


def humanize_post(body: str) -> str:
    """Apply the humanizer gate before any post is saved."""
    prompt = f"""다음 마크다운 기술 블로그 초안을 humanizer 스킬 기준으로 교정하세요.

규칙:
- 의미, 코드블록, 링크, 표, 제목 구조는 보존
- 과장된 의미 부여, 광고 문구, 막연한 출처, AI 단어, not only/but 패턴, 3개 나열 강박, em dash 남발, 굵은 글씨 남발, 이모지, 챗봇 말투, 지식 컷오프 문구, 뻔한 긍정 결론 제거
- 문장은 실제 사람이 쓴 한국어 기술 블로그처럼 구체적으로 정리
- 출력은 교정된 Markdown 본문만. frontmatter, 설명, 변경 요약은 쓰지 말 것

[초안]
{body}
"""
    try:
        humanized = run_codex_prompt(prompt, "humanizer", timeout=CODEX_TIMEOUT_SECONDS)
        if not humanized:
            raise ValueError("empty humanizer response")
        return humanized
    except Exception as e:
        log.error(f"humanizer 적용 실패: {e}")
        raise


# ─────────────────────────────────────────────
# 6. Hugo frontmatter + 파일 저장
# ─────────────────────────────────────────────
def build_title_and_slug(article: dict, body: str, supporting_context: str) -> dict:
    prompt = f"""아래 선정 글감과 보조 레퍼런스를 함께 읽고, 최종 편집 주제에 맞는 다음 4가지를 JSON 형식으로 생성해주세요.
**목표: 커뮤니티에서 이슈화된 기술 주제를 한국 개발자가 검색할 법한 제목으로 정리하세요.**

1. **title**: 검색 노출에 최적화된 한국어 제목
- 개발자가 실제로 검색할 법한 핵심 키워드를 제목 앞쪽에 배치
- 회사명이나 원문 제목을 그대로 앞세우지 말고, 기술 쟁점과 아키텍처 관점을 제목에 반영
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


def main():
    model_label = f" ({CODEX_MODEL})" if CODEX_MODEL else ""
    log.info(f"🤖 Codex CLI 사용: {CODEX_BIN}{model_label}")

    # 0. 쿨다운 체크: FORCE=true 환경변수가 있으면 스킵
    if not os.environ.get("FORCE_RUN"):
        _check_cooldown()

    # 1. seen 로드 (타임스탬프 기반, 만료된 항목 자동 제외)
    seen = load_seen()

    # 2. RSS 수집 (14일 윈도우)
    feeds = load_feeds()
    articles = fetch_recent_articles(feeds)

    if not articles:
        log.warning("⚠️  최근 기사를 찾지 못했습니다. 윈도우를 28일로 확장합니다.")
        articles = fetch_recent_articles(feeds, hours=672)

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

    # 4. 최고 기사 선정 (셔플된 후보 풀에서)
    best = select_best_article(fresh)
    if not best:
        log.error("❌ 포스팅할 기사를 선정하지 못했습니다.")
        sys.exit(1)

    # 5. 본문 크롤링
    log.info(f"🌐 본문 크롤링: {best['link']}")
    body_raw, cover_image = fetch_article_body(best["link"])
    log.info(f"   추출 길이: {len(body_raw)}자 / 커버 이미지: {'O' if cover_image else 'X'}")

    supporting_articles = select_supporting_articles(best, articles)
    supporting_context = build_supporting_context(supporting_articles)
    log.info(f"📚 보조 레퍼런스 {len(supporting_articles)}건 확보")
    for article in supporting_articles:
        log.info(f"  - [{article['source']}] {article['title']}")

    # 6. 메타데이터 생성
    log.info("📝 메타데이터(제목, 슬러그, 커스텀 SEO 키워드) 생성 중...")
    meta = build_title_and_slug(best, body_raw, supporting_context)
    meta['cover_image'] = cover_image
    log.info(f"📝 생성된 제목: {meta['title']}")
    log.info(f"🔗 SEO 슬러그: {meta['slug']}")

    # 7. 포스트 본문 생성
    log.info("✍️  포스트 작성 중 (선정 글감 + 보조 레퍼런스 기반 전문 분석)...")
    post_body = generate_post(best, body_raw, supporting_context)

    # 8. humanizer 필수 적용
    log.info("🧹 humanizer 스킬 적용 중...")
    post_body = humanize_post(post_body)

    # 9. 파일 저장
    saved_path = save_post(meta, best, post_body)

    # 10. seen 캐시 업데이트
    seen.add(best["uid"])
    save_seen(seen)

    log.info(f"🎉 완료! 생성된 파일: {saved_path}")
    print(f"CREATED_FILE={saved_path}")


if __name__ == "__main__":
    main()
