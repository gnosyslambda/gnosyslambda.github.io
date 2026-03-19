#!/usr/bin/env python3
"""
trend_writer.py
================
해외 테크 블로그 RSS 피드를 수집하고, Gemini API를 통해
14년 차 백엔드 개발자 시각의 한국어 포스트를 생성하는 스크립트.

Usage:
    GEMINI_API_KEY=<key> python scripts/trend_writer.py
"""

import os
import re
import sys
import json
import random
import logging
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
import google.genai as genai
from google.genai import types as genai_types

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

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
FETCH_WINDOW_HOURS = 336         # 최근 14일 기사 수집 (빅테크 블로그 발행 빈도 고려)
MAX_ARTICLES_TO_SCORE = 20       # LLM 혁신도 평가 최대 기사 수
MAX_BODY_CHARS = 10000           # 본문 최대 글자 수 (토큰 절약)
HTTP_TIMEOUT = 15                # 요청 타임아웃(초)
MAX_SUPPORTING_ARTICLES = 3      # 보조 레퍼런스로 붙일 추가 글 수
MAX_SUPPORTING_BODY_CHARS = 2500 # 보조 글 본문 최대 길이
SEEN_EXPIRE_DAYS = 60            # seen 항목 만료 기간 (60일 후 재선정 가능)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
}


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

            article = {
                "source": feed_name,
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "summary": _clean_html(entry.get("summary", "")),
                "published": pub_date.isoformat() if pub_date else "",
                "tags": feed_meta.get("tags", []),
                "uid": _uid(entry.get("link", entry.get("title", ""))),
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
    return re.sub(r"\s+", " ", soup.get_text()).strip()[:800]


def _uid(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _tokenize_korean_english(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-+.#]{1,}|[가-힣]{2,}", text)
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


# ─────────────────────────────────────────────
# 3. Gemini: 기사 혁신도 평가 → 최고 기사 선정
# ─────────────────────────────────────────────
def select_best_article(articles: list[dict], genai_client, model: str) -> dict | None:
    if not articles:
        return None

    # 랜덤 셔플 후 상위 N개만 평가 (항상 같은 기사 반복 방지)
    candidates = articles[:MAX_ARTICLES_TO_SCORE]
    bullets = "\n".join(
        f"{i+1}. [{a['source']}] {a['title']}\n   요약: {a['summary'][:200]}"
        for i, a in enumerate(candidates)
    )

    prompt = f"""당신은 Java/Kotlin 기반 백엔드 시스템을 14년간 운영한 시니어 개발자입니다.
아래 해외 테크 블로그 기사 목록을 검토하고, 다음 기준에 따라 **가장 가치 있는 기사 1개**를 선정해주세요.

선정 기준:
1. 기술적 혁신성 (새로운 아키텍처, 패턴, 접근법)
2. 실무 적용 가능성 (Java/Kotlin, Spring Boot, JVM 환경)
3. 스케일 및 신뢰성 문제 해결 사례
4. 백엔드 개발자에게 인사이트를 주는 내용

기사 목록:
{bullets}

응답 형식 (JSON만 출력, 다른 텍스트 없이):
{{
  "selected_index": 1,
  "reason": "선정 이유 (한국어, 2-3문장)"
}}"""

    try:
        response = genai_client.models.generate_content(
            model=model,
            contents=prompt,
        )
        raw = response.text.strip()
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


def select_supporting_articles(primary: dict, articles: list[dict]) -> list[dict]:
    primary_tokens = _tokenize_korean_english(
        f"{primary['title']} {primary['summary']} {' '.join(primary.get('tags', []))}"
    )
    scored: list[tuple[int, dict]] = []

    for article in articles:
        if article["uid"] == primary["uid"]:
            continue

        article_tokens = _tokenize_korean_english(
            f"{article['title']} {article['summary']} {' '.join(article.get('tags', []))}"
        )
        overlap = len(primary_tokens & article_tokens)
        source_bonus = 1 if article["source"] != primary["source"] else 0
        freshness_bonus = 1 if article.get("published") else 0
        score = overlap * 3 + source_bonus + freshness_bonus

        if score > 0:
            scored.append((score, article))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [article for _, article in scored[:MAX_SUPPORTING_ARTICLES]]


def build_supporting_context(articles: list[dict]) -> str:
    if not articles:
        return "보조 레퍼런스 없음"

    blocks: list[str] = []
    for idx, article in enumerate(articles, start=1):
        article_body, _ = fetch_article_body(article["link"])
        excerpt = article_body[:MAX_SUPPORTING_BODY_CHARS] if article_body else article["summary"]
        blocks.append(
            "\n".join(
                [
                    f"[보조 레퍼런스 {idx}]",
                    f"제목: {article['title']}",
                    f"출처: {article['source']}",
                    f"URL: {article['link']}",
                    f"요약: {article['summary']}",
                    f"발췌: {excerpt or '본문 확보 실패'}",
                ]
            )
        )

    return "\n\n".join(blocks)


# ─────────────────────────────────────────────
# 5. Gemini: 페르소나 기반 한국어 포스트 생성
# ─────────────────────────────────────────────
def generate_post(article: dict, body: str, supporting_context: str, genai_client, model: str) -> str:
    source_content = f"""[원문 제목] {article['title']}
[출처] {article['source']}
[원문 URL] {article['link']}
[요약] {article['summary']}

[원문 내용]
{body if body else "본문을 가져오지 못했습니다. 요약 내용을 기반으로 작성해주세요."}"""

    persona_prompt = f"""당신은 해외 빅테크 엔지니어링 블로그를 전문적으로 번역·큐레이션하는 기술 에디터입니다.
원문의 핵심 내용을 **충실하게 한국어로 번역**하되, 단순 직역이 아닌 한국 개발자가 읽기 좋은 형태로 다듬어 제공합니다.

━━━ 핵심 원칙 ━━━
1. **원문 충실성**: 원문의 핵심 내용, 수치, 사례를 정확히 전달. 없는 내용 창작 금지.
2. **가독성 높은 번역**: 기술 용어는 영문 병기 (예: "서킷 브레이커(Circuit Breaker)"), 문장은 자연스러운 한국어로.
3. **구조 재구성**: 원문 구조를 그대로 옮기지 말고, 한국 독자가 읽기 좋게 재구성할 것.
4. **코드/다이어그램 보존**: 원문의 코드 스니펫, 아키텍처 다이어그램은 반드시 포함. 없으면 핵심 동작을 설명하는 Mermaid 다이어그램 1개 추가.

━━━ 절대 금지 사항 ━━━
- 원문에 없는 수치, 제품명, API명, 사용자 사례 창작 금지
- "결론적으로", "요약하자면", "이처럼", "자 이제", "살펴보겠습니다" 등 AI 냄새 나는 표현 금지
- 마케팅 문구, 과장, 감탄사 금지
- 뻔한 도입부("최근 X분야에서 Y가 주목받고 있습니다") 금지

━━━ 글쓰기 스타일 ━━━
1. **문단**: 한 문단 최대 3문장. 호흡 짧게.
2. **스캐닝 가능하게**: 불릿(-), 굵은글씨(**), 인용블록(>), 표를 적극 활용
3. **기술 용어**: 첫 등장 시 영문 병기, 이후 한국어만 사용
4. 길이: 최소 **2,500자** 이상 (원문의 깊이를 충분히 전달)

━━━ 포스트 구조 ━━━

> **TL;DR** — 이 글의 핵심을 2-3문장으로 요약

## 배경과 문제 정의
- 원문이 다루는 문제의 맥락과 배경
- 왜 이 문제가 중요한지

## 핵심 내용
- 원문의 주요 기술적 내용을 충실하게 번역·전달
- **[필수]** 원문의 코드 스니펫 또는 아키텍처 다이어그램 포함 (없으면 Mermaid로 재구성)
- 복잡한 내용은 단계별로 쪼개서 설명

## 실무 적용 포인트
- 이 기술/접근법을 실제로 적용할 때 고려할 사항
- 도입하면 좋은 상황 vs. 불필요한 상황
- 트레이드오프와 운영 리스크

## 마치며
> (원문의 핵심 메시지를 한 마디로 정리)

---

> **출처**: 이 글은 [{article['source']}]({article['link']})의 원문을 한국어로 번역·재구성한 글입니다.
> 원문의 저작권은 원저자에게 있으며, 이 글은 학습과 정보 공유 목적으로 작성되었습니다.

*참고자료*
- 원문: [{article['title']}]({article['link']})
- 보조 레퍼런스에서 실제로 활용한 링크 추가

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[보조 레퍼런스]
{supporting_context}

[원문]
{source_content}

위 구조와 원칙에 따라 포스트 **본문만** 출력하세요. 제목(title)은 포함하지 마세요."""

    try:
        response = genai_client.models.generate_content(
            model=model,
            contents=persona_prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.80,
                max_output_tokens=8192,
            ),
        )
        return response.text.strip()
    except Exception as e:
        log.error(f"포스트 생성 실패: {e}")
        raise


# ─────────────────────────────────────────────
# 6. Hugo frontmatter + 파일 저장
# ─────────────────────────────────────────────
def build_title_and_slug(article: dict, body: str, genai_client, model: str) -> dict:
    prompt = f"""아래 해외 기술 블로그 원문을 기반으로 다음 4가지를 JSON 형식으로 추출해주세요.

1. **title**: 원문 제목을 자연스러운 한국어로 번역한 제목 (최대 40자, 기술적 핵심이 드러나게, 과장 없이)
2. **slug**: URL에 사용할 영문 SEO 슬러그 (소문자, 알파벳과 하이픈만 포함, 3~6단어 길이의 핵심 키워드 압축)
3. **keywords**: 구글 검색 노출을 위한 SEO 최적화된 핵심 기술 키워드 (영문/한글 혼합 가능, 5~7개)
4. **description**: 원문의 핵심 내용을 한국어로 요약 (1~2문장, "[출처명] 번역" 형태 포함)

기사 제목: {article['title']}
기사 요약: {article['summary'][:300]}

응답 형식 (오직 JSON만 출력):
{{
  "title": "한국어 제목",
  "slug": "english-seo-friendly-slug",
  "keywords": ["키워드1", "keyword2"],
  "description": "이 글은 ..."
}}"""
    try:
        response = genai_client.models.generate_content(model=model, contents=prompt)
        raw = response.text.strip()
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
    base = article.get("tags", [])
    combined = list(set(base + new_keywords + ["해외기술블로그", "백엔드", "아키텍처"]))
    return [t.replace(" ", "-") for t in combined]


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")[:60]


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

    tags_yaml = "\n".join(f'  - "{t}"' for t in build_tags(article, meta.get("keywords", [])))
    frontmatter = f"""---
date: '{date_str}'
draft: false
title: '{title.replace("'", "''")}'
tags:
{tags_yaml}
categories:
  - "해외 테크 블로그 번역"
description: "{meta.get('description', article['summary'][:150]).replace('"', "'")}"
source:
  name: "{article.get('source', '')}"
  url: "{article.get('link', '')}"
  title: "{article.get('title', '').replace('"', "'")}"
cover:
  image: "{meta.get('cover_image', '')}"
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
POST_COOLDOWN_MINUTES = 55   # 이 시간 이내에 이미 포스트가 생성됐으면 스킵


def main():
    if not GEMINI_API_KEY:
        log.error("❌ GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")
        sys.exit(1)

    genai_client = genai.Client(api_key=GEMINI_API_KEY)

    flash_model_name = "gemini-3-flash-preview"
    pro_model_name = "gemini-3-flash-preview"

    # 0. 쿨다운 체크: 최근 55분 이내 포스트가 이미 생성됐으면 스킵 (30분 스케줄 중복 방지)
    posts = sorted(POSTS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if posts:
        last_mtime = datetime.fromtimestamp(posts[0].stat().st_mtime, tz=timezone.utc)
        elapsed_min = (datetime.now(tz=timezone.utc) - last_mtime).total_seconds() / 60
        if elapsed_min < POST_COOLDOWN_MINUTES:
            log.info(f"⏭️  최근 {int(elapsed_min)}분 전 포스트가 이미 생성됨 — 이번 실행은 건너뜁니다.")
            sys.exit(0)
        log.info(f"⏱️  마지막 포스트로부터 {int(elapsed_min)}분 경과 — 새 포스트 생성 시작")

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
    best = select_best_article(fresh, genai_client, flash_model_name)
    if not best:
        log.error("❌ 포스팅할 기사를 선정하지 못했습니다.")
        sys.exit(1)

    # 5. 본문 크롤링
    log.info(f"🌐 본문 크롤링: {best['link']}")
    body_raw, cover_image = fetch_article_body(best["link"])
    log.info(f"   추출 길이: {len(body_raw)}자 / 커버 이미지: {'O' if cover_image else 'X'}")

    supporting_articles = select_supporting_articles(best, fresh)
    supporting_context = build_supporting_context(supporting_articles)
    log.info(f"📚 보조 레퍼런스 {len(supporting_articles)}건 확보")

    # 6. 메타데이터 생성
    log.info("📝 메타데이터(제목, 슬러그, 커스텀 SEO 키워드) 생성 중...")
    meta = build_title_and_slug(best, body_raw, genai_client, flash_model_name)
    meta['cover_image'] = cover_image
    log.info(f"📝 생성된 제목: {meta['title']}")
    log.info(f"🔗 SEO 슬러그: {meta['slug']}")

    # 7. 포스트 본문 생성
    log.info("✍️  포스트 작성 중 (원문 + 보조 레퍼런스 기반 전문 분석)...")
    post_body = generate_post(best, body_raw, supporting_context, genai_client, pro_model_name)

    # 8. 파일 저장
    saved_path = save_post(meta, best, post_body)

    # 9. seen 캐시 업데이트
    seen.add(best["uid"])
    save_seen(seen)

    log.info(f"🎉 완료! 생성된 파일: {saved_path}")
    print(f"CREATED_FILE={saved_path}")


if __name__ == "__main__":
    main()
