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
FETCH_WINDOW_HOURS = 48          # 최근 N시간 기사 수집 (여유 있게 48h)
MAX_ARTICLES_TO_SCORE = 15       # LLM 혁신도 평가 최대 기사 수
MAX_BODY_CHARS = 10000           # 본문 최대 글자 수 (토큰 절약)
HTTP_TIMEOUT = 15                # 요청 타임아웃(초)
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


# ─────────────────────────────────────────────
# 2. 중복 기사 필터링
# ─────────────────────────────────────────────
def load_seen() -> set[str]:
    if SEEN_CACHE.exists():
        with open(SEEN_CACHE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set[str]) -> None:
    with open(SEEN_CACHE, "w", encoding="utf-8") as f:
        # 최근 200개만 유지
        json.dump(list(seen)[-200:], f)


# ─────────────────────────────────────────────
# 3. Gemini: 기사 혁신도 평가 → 최고 기사 선정
# ─────────────────────────────────────────────
def select_best_article(articles: list[dict], genai_client, model: str) -> dict | None:
    if not articles:
        return None

    # 점수 매길 기사 샘플링
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
        # JSON 블록 추출
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
def fetch_article_body(url: str) -> str:
    """URL에서 본문 텍스트 추출. 실패 시 빈 문자열 반환."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # 불필요한 요소 제거
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "ads"]):
            tag.decompose()

        # 본문 후보 태그 순서대로 시도
        for selector in ["article", "main", ".post-content", ".entry-content", "body"]:
            container = soup.select_one(selector)
            if container:
                text = re.sub(r"\s+", " ", container.get_text()).strip()
                if len(text) > 500:
                    return text[:MAX_BODY_CHARS]

        # fallback
        return re.sub(r"\s+", " ", soup.get_text()).strip()[:MAX_BODY_CHARS]
    except Exception as e:
        log.warning(f"본문 크롤링 실패 ({url}): {e}")
        return ""


# ─────────────────────────────────────────────
# 5. Gemini: 페르소나 기반 한국어 포스트 생성
# ─────────────────────────────────────────────
def generate_post(article: dict, body: str, genai_client, model: str) -> str:
    """14년 차 백엔드 개발자 페르소나로 한국어 블로그 포스트 생성."""
    source_content = f"""[원문 제목] {article['title']}
[출처] {article['source']}
[원문 URL] {article['link']}
[요약] {article['summary']}

[원문 내용]
{body if body else "본문을 가져오지 못했습니다. 요약 내용을 기반으로 작성해주세요."}"""

    persona_prompt = f"""당신은 14년 차 Java/Kotlin 백엔드 아키텍트입니다.
대형 이커머스 플랫폼에서 MSA 전환, 트래픽 1000만 RPS 대응, 카산드라/Redis/Kafka 운영 경험이 있습니다.
현재 기술 블로그 'sectoreye's log'를 운영하며, 해외 빅테크 사례를 한국 실무 환경에 맞게 재해석하는 글을 씁니다.

아래 원문을 읽고, **반드시 다음 구조**로 한국어 블로그 포스트를 작성하세요.

─────────────── 작성 규칙 ───────────────
1. 말투: 정중하면서도 실무적인 통찰이 느껴지는 시니어 톤. "~입니다", "~합니다" 체.
2. 단순 번역 절대 금지. 직접 경험한 것처럼, 또는 비판적인 시각을 섞어 작성.
3. Java/Kotlin, Spring Boot, JVM 환경과의 비교·분석 반드시 포함.
4. 마크다운 형식으로 작성. 소제목은 ## 레벨 사용.
5. 글 길이: 1500~2500자 사이.

─────────────── 포스트 구조 ───────────────
## 왜 이 기술이 등장했는가? (문제 정의)
- 기존 방식의 한계, 해결하려는 문제 배경을 구체적으로 서술

## 핵심 아키텍처 분석
- 기술의 핵심 구조와 동작 원리를 다이어그램 없이도 이해되게 설명
- 복잡한 개념은 실생활 비유 또는 코드 수준 예시 활용

## 실무 관점의 시사점
- Java/Kotlin, Spring 생태계에서 이 기술을 적용하거나 벤치마킹할 수 있는 방법
- 한국 엔터프라이즈 환경(레거시 전환, 규제 이슈, 팀 역량 등)에서의 현실적 고려사항

## 14년 차 개발자의 한 줄 평
> (날카롭고 기억에 남는 한 줄. 칭찬과 비판을 균형있게.)

---
*원문: [{article['source']}]({article['link']})*
─────────────────────────────────────────

[원문]
{source_content}

위 규칙과 구조에 따라 포스트 **본문만** 출력하세요. 제목(title)은 포함하지 마세요."""

    try:
        response = genai_client.models.generate_content(
            model=model,
            contents=persona_prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.85,
                max_output_tokens=4096,
            ),
        )
        return response.text.strip()
    except Exception as e:
        log.error(f"포스트 생성 실패: {e}")
        raise


# ─────────────────────────────────────────────
# 6. Hugo frontmatter + 파일 저장
# ─────────────────────────────────────────────
def build_title(article: dict, genai_client, model: str) -> str:
    """한국어 블로그 제목 생성."""
    prompt = f"""아래 영문 기사를 보고, 한국 개발자 블로그에 어울리는 **한국어 제목**을 1개만 만들어주세요.
- 클릭을 유도하되 과장 없이, 기술적 핵심이 드러나야 합니다.
- 부제목 없이 제목 하나만 출력하세요.
- 최대 40자.

기사 제목: {article['title']}
출처: {article['source']}

한국어 제목:"""
    try:
        response = genai_client.models.generate_content(model=model, contents=prompt)
        return response.text.strip().strip('"').strip("'")
    except Exception:
        return article["title"]


def build_tags(article: dict) -> list[str]:
    base = article.get("tags", [])
    # 공통 태그 추가
    return list(set(base + ["해외-기술-블로그", "백엔드", "아키텍처"]))


def slugify(text: str) -> str:
    """제목을 파일명 슬러그로 변환."""
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")[:60]


def save_post(title: str, article: dict, body: str) -> Path:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(tz=kst)
    date_str = now_kst.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    date_prefix = now_kst.strftime("%Y-%m-%d")

    slug = slugify(title) or slugify(article["title"])
    filename = f"{date_prefix}-{slug}.md"
    filepath = POSTS_DIR / filename

    # 겹치면 숫자 붙이기
    counter = 1
    while filepath.exists():
        filepath = POSTS_DIR / f"{date_prefix}-{slug}-{counter}.md"
        counter += 1

    tags_yaml = "\n".join(f'  - "{t}"' for t in build_tags(article))
    frontmatter = f"""---
date: '{date_str}'
draft: false
title: '{title.replace("'", "''")}'
tags:
{tags_yaml}
categories:
  - "글로벌 테크 인사이트"
description: "{article['summary'][:150].replace('"', "'")}"
cover:
  image: ""
  alt: ""
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
def main():
    if not GEMINI_API_KEY:
        log.error("❌ GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")
        sys.exit(1)

    genai_client = genai.Client(api_key=GEMINI_API_KEY)
    model = "gemini-2.5-flash"

    # 1. 이미 처리한 기사 로드
    seen = load_seen()

    # 2. RSS 수집
    feeds = load_feeds()
    articles = fetch_recent_articles(feeds)

    if not articles:
        log.warning("⚠️  최근 기사를 찾지 못했습니다. 윈도우를 72시간으로 확장합니다.")
        articles = fetch_recent_articles(feeds, hours=72)

    # 3. 이미 작성된 기사 제외
    fresh = [a for a in articles if a["uid"] not in seen]
    if not fresh:
        log.info("오늘 처리할 새 기사가 없습니다. 모든 기사를 다시 후보로 사용합니다.")
        fresh = articles

    # 4. 최고 기사 선정
    best = select_best_article(fresh, genai_client, model)
    if not best:
        log.error("❌ 포스팅할 기사를 선정하지 못했습니다.")
        sys.exit(1)

    # 5. 본문 크롤링
    log.info(f"🌐 본문 크롤링: {best['link']}")
    body_raw = fetch_article_body(best["link"])
    log.info(f"   추출 길이: {len(body_raw)}자")

    # 6. 한국어 제목 생성
    ko_title = build_title(best, genai_client, model)
    log.info(f"📝 생성된 제목: {ko_title}")

    # 7. 포스트 본문 생성
    log.info("✍️  포스트 작성 중 (Gemini)...")
    post_body = generate_post(best, body_raw, genai_client, model)

    # 8. 파일 저장
    saved_path = save_post(ko_title, best, post_body)

    # 9. seen 캐시 업데이트
    seen.add(best["uid"])
    save_seen(seen)

    log.info(f"🎉 완료! 생성된 파일: {saved_path}")
    print(f"CREATED_FILE={saved_path}")  # GitHub Actions에서 파싱용


if __name__ == "__main__":
    main()
