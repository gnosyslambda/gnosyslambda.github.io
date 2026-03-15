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
MAX_SUPPORTING_ARTICLES = 3      # 보조 레퍼런스로 붙일 추가 글 수
MAX_SUPPORTING_BODY_CHARS = 2500 # 보조 글 본문 최대 길이
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


def _tokenize_korean_english(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-+.#]{1,}|[가-힣]{2,}", text)
    }


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
def fetch_article_body(url: str) -> tuple[str, str]:
    """URL에서 본문 텍스트와 커버 이미지 URL 추출. 실패 시 빈 문자열 반환."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # 본문 추출 전 이미지/메타데이터 추출
        cover_image = _extract_cover_image(soup)

        # 불필요한 요소 제거
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "ads"]):
            tag.decompose()

        # 본문 후보 태그 순서대로 시도
        for selector in ["article", "main", ".post-content", ".entry-content", "body"]:
            container = soup.select_one(selector)
            if container:
                text = re.sub(r"\s+", " ", container.get_text()).strip()
                if len(text) > 500:
                    return text[:MAX_BODY_CHARS], cover_image

        # fallback
        return re.sub(r"\s+", " ", soup.get_text()).strip()[:MAX_BODY_CHARS], cover_image
    except Exception as e:
        log.warning(f"본문 크롤링 실패 ({url}): {e}")
        return "", ""


def _extract_cover_image(soup) -> str:
    """원문 HTML에서 og:image 또는 첫 번째 적절한 이미지 URL을 추출합니다."""
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
    """주제적으로 가까운 다른 블로그 글을 보조 레퍼런스로 고른다."""
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
    """14년 차 백엔드 개발자 페르소나로 한국어 블로그 포스트 생성."""
    source_content = f"""[원문 제목] {article['title']}
[출처] {article['source']}
[원문 URL] {article['link']}
[요약] {article['summary']}

[원문 내용]
{body if body else "본문을 가져오지 못했습니다. 요약 내용을 기반으로 작성해주세요."}"""

    persona_prompt = f"""당신은 IT 산업의 흐름과 아키텍처를 날카롭게 분석하는 기술 전문 블로거입니다.
현재 기술 블로그 'gnosyslambda's log'를 운영하며, 해외 빅테크 사례를 한국 실무 환경에 맞게 재해석하는 인사이트 가득한 글을 씁니다.

**중요 지시사항:**
1.  주어진 [원문]과 [보조 레퍼런스]만 근거로 사용하세요.
2.  제공되지 않은 사실, 수치, 제품명, 버전, 사용자 사례를 상상해서 쓰지 마세요.
3.  단순 요약을 넘어서, 여러 소스를 비판적으로 종합하고 한국 실무 관점의 판단을 분명하게 제시하세요.
4.  전문 기술 블로그 편집자처럼 쓰세요. 마케팅 문구, 과장, 감탄사는 금지합니다.

─────────────── 핵심 작성 규칙 (가독성 및 통찰력 최우선) ───────────────
1.  **짧은 문단**: 한 문단은 절대 3문장을 넘지 않게 짧게 끊어 쓰세요.
2.  **시각적 요소 적극 활용**: 불릿 포인트(-), 굵은 글씨(**), 인용구(>)를 사용하여 스캐닝(Scanning)하기 좋게 작성하세요. 긴 글은 읽히지 않습니다.
3.  **기술적 증명 (코드/구조 필수)**: 글의 핵심을 설명할 때 반드시 1개 이상의 **코드 스니펫(예시 코드)**이나 **Mermaid 다이어그램**을 마크다운 문법으로 포함하세요. 다만 실제 소스에 없는 API 이름을 지어내지 말고, 개념 설명용 예시임을 자연스럽게 드러내세요.
4.  **글쓰기 스타일 (매우 중요)**: "결론적으로", "요약하자면", "이처럼", "자, 이제" 같은 전형적인 AI 스타일의 접속사나 진부한 문구를 **절대** 사용하지 마세요. 사람이 직접 쓴 것처럼 자연스럽고, 거시적인 통찰력(Insight)이 돋보여야 합니다.
5.  단순 번역은 금지합니다. 원문의 기술적 배경을 한국 실무 환경 혹은 글로벌 IT 트렌드와 연관지어 고민한 흔적을 담아주세요.
6.  마크다운 형식으로 작성. 소제목은 ## 레벨 사용.
7.  독자가 바로 실무 판단에 쓸 수 있게 써야 합니다. 추상적 칭찬보다 적용 조건, 트레이드오프, 운영 리스크를 우선하세요.
8.  본문 길이는 최소 1,200자 이상으로 작성하세요.

─────────────── 포스트 구조 ───────────────
## 이 기술이 던지는 화두 (문제 정의와 배경)
- 기존 방식의 한계, 이 기술이 해결하려는 핵심 맥락
- *단답형, 불릿으로 눈에 띄게 정리할 것*

## 기술의 핵심 동작 원리 (아키텍처/코드 파고들기)
- 복잡한 것을 직관적으로 설명 (거시적 관점)
- **[필수] 동작이나 구조를 보여주는 가상 코드나 Mermaid 다이어그램(````mermaid ... ````) 포함**

## 실무 적용과 남겨진 과제 (인사이트)
- 이 기술을 실제 개발/운영 환경에 적용할 때 얻을 수 있는 가치와 현실적인 한계점
- 다른 기술 스택과의 비교 우위, 앞으로의 발전 방향 등
- 가능하면 **언제 도입해야 하고, 언제 굳이 도입하지 않아도 되는지**를 분리해서 설명할 것

## 마치며
> (전체를 관통하는 날카롭고 깊이 있는 한 줄 평. 뻔한 칭찬 지양.)

---
*참고자료*
- [{article['source']}]({article['link']})
- 보조 레퍼런스에서 실제로 활용한 링크를 1~3개 추가
─────────────────────────────────────────

[보조 레퍼런스]
{supporting_context}

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
def build_title_and_slug(article: dict, body: str, genai_client, model: str) -> dict:
    """한국어 제목, 영문 SEO 슬러그, SEO 키워드를 한 번에 생성합니다."""
    prompt = f"""아래 원문을 기반으로 다음 4가지를 JSON 형식으로 추출해주세요.

1. **title**: 클릭을 유도하되 과장 없이 기술적 핵심이 드러나는 한국어 제목 (최대 40자, 부제목 없이)
2. **slug**: URL에 사용할 영문 SEO 슬러그 (소문자, 알파벳과 하이픈만 포함, 3~6단어 길이의 핵심 키워드 압축)
3. **keywords**: 구글 검색 노출을 위한 SEO 최적화된 핵심 기술 키워드 (영문/한글 혼합 가능, 5~7개)
4. **description**: 프론트매터 최상단에 노출될 한국어 요약 (글의 통찰력을 담은 1~2문장 짜리 간결한 설명)

기사 제목: {article['title']}
기사 요약: {article['summary'][:300]}

응답 형식 (오직 JSON만 출력):
{{
  "title": "한국어 제목",
  "slug": "english-seo-friendly-slug",
  "keywords": ["키워드1", "keyword2", ...],
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

    # Fallback
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
    """Fallback filename slug."""
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")[:60]


def save_post(meta: dict, article: dict, body: str) -> Path:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(tz=kst)
    date_str = now_kst.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    date_prefix = now_kst.strftime("%Y-%m-%d")

    # Use AI generated english slug, fallback to title slug
    title = meta["title"]
    slug = meta.get("slug") or slugify(title) or slugify(article["title"])
    slug = slugify(slug)  # ensure safe characters
    
    filename = f"{date_prefix}-{slug}.md"
    filepath = POSTS_DIR / filename

    # 겹치면 숫자 붙이기
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
  - "글로벌 테크 인사이트"
description: "{meta.get('description', article['summary'][:150]).replace('"', "'")}"
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
def main():
    if not GEMINI_API_KEY:
        log.error("❌ GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")
        sys.exit(1)

    genai.configure(api_key=GEMINI_API_KEY)
    
    # 모델을 역할에 따라 분리
    # - Preview Flash: 기사 선정, 메타데이터 생성 등 빠른 작업
    # - Preview Pro: 최종 포스트 생성 등 고품질 분석 작업
    flash_model_name = "gemini-3.1-flash-preview"
    pro_model_name = "gemini-3.1-pro-preview"
    flash_model = genai.GenerativeModel(flash_model_name)
    pro_model = genai.GenerativeModel(pro_model_name)

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
        log.info("이미 처리한 기사만 남아 있어 이번 실행은 건너뜁니다.")
        return

    # 4. 최고 기사 선정 (Flash 모델 사용)
    best = select_best_article(fresh, flash_model, flash_model_name)
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

    # 6. 메타데이터 (제목, 슬러그, SEO 키워드) 추출 (Flash 모델 사용)
    log.info("📝 메타데이터(제목, 슬러그, 커스텀 SEO 키워드) 생성 중...")
    meta = build_title_and_slug(best, body_raw, flash_model, flash_model_name)
    meta['cover_image'] = cover_image
    log.info(f"📝 생성된 제목: {meta['title']}")
    log.info(f"🔗 SEO 슬러그: {meta['slug']}")

    # 7. 포스트 본문 생성 (Pro 모델 사용)
    log.info("✍️  포스트 작성 중 (원문 + 보조 레퍼런스 기반 전문 분석)...")
    post_body = generate_post(best, body_raw, supporting_context, pro_model, pro_model_name)

    # 8. 파일 저장
    saved_path = save_post(meta, best, post_body)

    # 9. seen 캐시 업데이트
    seen.add(best["uid"])
    save_seen(seen)

    log.info(f"🎉 완료! 생성된 파일: {saved_path}")
    print(f"CREATED_FILE={saved_path}")  # GitHub Actions에서 파싱용


if __name__ == "__main__":
    main()
