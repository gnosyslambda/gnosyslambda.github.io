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

    prompt = f"""당신은 기술 블로그 SEO 전략가입니다.
아래 해외 테크 블로그 기사 목록을 검토하고, 다음 기준에 따라 **가장 가치 있는 기사 1개**를 선정해주세요.

선정 기준 (우선순위 순):
1. **SEO 검색 수요**: 한국 개발자들이 구글/네이버에서 자주 검색하는 키워드와 연관된 주제
   - 예: "MSA", "쿠버네티스", "AI 개발", "성능 최적화", "보안", "데이터베이스", "캐싱", "CI/CD"
2. **실용적 깊이**: 개념 소개가 아닌 실제 구현/운영 사례가 있는 글
3. **기술적 혁신성**: 새로운 아키텍처, 패턴, 접근법
4. **백엔드 실무 관련성**: Java/Kotlin, Spring Boot, JVM 환경 적용 가능

기사 목록:
{bullets}

응답 형식 (JSON만 출력, 다른 텍스트 없이):
{{
  "selected_index": 1,
  "reason": "선정 이유 (한국어, 2-3문장)",
  "seo_keywords": ["검색될만한 핵심 키워드1", "키워드2", "키워드3"]
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

    persona_prompt = f"""해외 테크 블로그 기사를 읽고, 이를 바탕으로 기술 블로그 포스트를 씁니다.
단순 번역이 아니라, 원문 내용을 이해한 뒤 자신의 언어로 풀어쓰고 실무적 시각을 녹여낸 글입니다.

━━━ 글쓰기 방식 ━━━
1. **원문 내용 충실히 전달**: 핵심 개념, 수치, 사례는 정확하게 소화해서 설명
2. **나만의 시각 추가**: 원문을 읽고 든 생각, 실무에서 겪을 법한 유사 상황, 동의하거나 의문을 가진 부분을 자연스럽게 녹임
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

## 이 주제를 꺼낸 이유
- 왜 이 기사가 눈에 들어왔는지, 어떤 문제의식과 연결되는지
- 독자가 왜 읽어야 하는지

## 핵심 내용 정리
- 원문의 주요 내용을 내 언어로 풀어서 설명
- 코드 스니펫 또는 Mermaid 다이어그램 포함 **[필수]**
- 복잡한 개념은 단계별로 쪼개서

## 내 생각 & 실무 관점
- 원문을 읽고 든 생각, 동의하는 부분과 의문이 드는 부분
- 비슷한 상황을 겪어본 경험이 있다면 자연스럽게 언급
- 이 접근법의 트레이드오프와 실제 도입 시 주의할 점

## 정리
- 핵심 메시지를 간결하게 마무리
- 독자가 당장 해볼 수 있는 것, 생각해볼 것 한 가지

## 참고 자료
- [원문] [{article['title']}]({article['link']}) — {article['source']}
- 보조 레퍼런스에서 실제로 활용한 링크를 `- [관련] 제목 — 출처` 형식으로 추가 (활용하지 않았으면 생략)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[보조 레퍼런스]
{supporting_context}

[원문]
{source_content}

위 구조와 방식에 따라 포스트 **본문만** 출력하세요. 제목(title)은 포함하지 마세요."""

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
    prompt = f"""아래 해외 기술 블로그 원문을 기반으로 다음 4가지를 JSON 형식으로 생성해주세요.
**목표: 한국 개발자가 구글/네이버 검색 시 상위 노출될 수 있도록 SEO를 최우선으로 고려하세요.**

1. **title**: 검색 노출에 최적화된 한국어 제목
   - 개발자가 실제로 검색할 법한 핵심 키워드를 제목 앞쪽에 배치
   - 최대 40자, 구체적이고 명확하게 (예: "쿠버네티스 스케줄러 동작 원리 완전 정리")
2. **slug**: 검색 노출을 위한 영문 SEO 슬러그
   - 소문자 + 하이픈만, 3~6단어, 핵심 기술 키워드 포함
3. **keywords**: 이 글로 유입될 수 있는 검색 키워드 7~10개
   - 한국어 검색어 + 영문 기술 용어 혼합
   - 구체적인 롱테일 키워드 포함 (예: "쿠버네티스 파드 스케줄링", "kubernetes scheduler 동작")
4. **description**: 검색 결과 스니펫에 노출될 메타 설명 (1~2문장, 160자 이내)
   - 핵심 키워드 자연스럽게 포함, 클릭을 유도하는 문장

기사 제목: {article['title']}
기사 요약: {article['summary'][:300]}

응답 형식 (오직 JSON만 출력):
{{
  "title": "SEO 최적화된 한국어 제목",
  "slug": "seo-optimized-slug",
  "keywords": ["한국어키워드1", "keyword2", "롱테일 키워드3"],
  "description": "검색 스니펫용 설명..."
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
    combined = list(set(base + new_keywords + ["백엔드", "아키텍처", "개발"]))
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
  - "기술 블로그"
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
    if not GEMINI_API_KEY:
        log.error("❌ GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")
        sys.exit(1)

    genai_client = genai.Client(api_key=GEMINI_API_KEY)

    flash_model_name = "gemini-3-flash-preview"
    pro_model_name = "gemini-3-flash-preview"

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
