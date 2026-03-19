# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

Hugo 기반 기술 블로그 + AI 자동 포스트 생성 시스템. PaperMod 테마 사용, GitHub Pages로 배포.

## 빌드 & 개발 명령어

```bash
# 테마 서브모듈 초기화 (최초 1회)
git submodule update --init --recursive

# 로컬 개발 서버 (드래프트 포함)
hugo server -D

# 프로덕션 빌드
hugo --minify

# 새 포스트 생성
hugo new posts/my-post.md

# AI 자동 포스트 생성 (Python 3.11+)
cd scripts && pip install -r requirements.txt
python trend_writer.py  # GEMINI_API_KEY 환경변수 필요
```

## 아키텍처

### 이중 파이프라인

1. **배포 파이프라인** (`.github/workflows/deploy.yml`): `main` 푸시 시 Hugo 빌드 → GitHub Pages 배포
2. **자동 포스트 생성** (`.github/workflows/trend_writer.yml`): 매시간 정각 실행 → `scripts/trend_writer.py`가 해외 테크 블로그 RSS 수집 → Gemini API로 한국어 번역 포스트 생성 → 자동 커밋/푸시 → 배포 트리거

### Trend Writer 처리 흐름

`feeds.json`의 11개 RSS 소스 수집 → `.seen_articles.json`으로 중복 필터링 → Gemini Flash로 기사 선정(점수 기반) → 원문 크롤링 → 보조 레퍼런스 선정 → Gemini Pro로 고품질 한국어 번역 → Hugo frontmatter 포함 마크다운 저장

### 커스터마이제이션

- **스타일**: `assets/css/extended/custom_font.css` — Pretendard Variable(한글), JetBrains Mono(코드), 프리미엄 디자인 커스텀
- **레이아웃 오버라이드**: `layouts/partials/extend_head.html` — 네이버 사이트 검증 메타태그
- **테마**: `themes/PaperMod/` — Git 서브모듈, 직접 수정하지 말 것

## 포스트 frontmatter 규격

```yaml
---
date: '2026-03-19T17:00:00+09:00'
draft: false
title: '한국어 제목 (최대 40자)'
tags: ["태그1", "태그2"]
categories: ["카테고리"]
description: "SEO 설명"
source:
  name: "원문 출처"
  url: "원문 링크"
  title: "원문 제목"
cover:
  image: "커버 이미지 URL"
  alt: "대체 텍스트"
showToc: true
TocOpen: true
---
```

파일명 규칙: `YYYY-MM-DD-slug.md` (slug는 영문 소문자 + 하이픈)

## 주요 설정

- `hugo.yaml`: Hugo 전체 설정 (baseURL, 메뉴, 프로필, 검색, 분석 등)
- `scripts/feeds.json`: RSS 피드 소스 목록 및 카테고리/태그 매핑
- `scripts/.seen_articles.json`: 처리된 기사 캐시 (자동 관리, 수동 편집 불필요)

## 주의사항

- `themes/PaperMod/`는 서브모듈이므로 직접 수정하지 말고 `layouts/`, `assets/css/extended/`에서 오버라이드
- `public/`은 빌드 산출물이므로 직접 편집하지 말 것
- `hugo.yaml`의 `minify.disableXML: true`는 XML 축소만 비활성화 (sitemap 생성에는 영향 없음)
- `markup.goldmark.renderer.unsafe: true`로 마크다운 내 HTML 허용됨
