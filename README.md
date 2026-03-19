<div align="center">

# gnosyslambda's log

**Backend Engineering & AI — 깊이 있는 기술 인사이트**

[![Blog](https://img.shields.io/badge/Blog-gnosyslambda.github.io-FF5722?style=flat-square&logo=hugo&logoColor=white)](https://gnosyslambda.github.io/)
[![GitHub Stars](https://img.shields.io/github/stars/gnosyslambda/gnosyslambda.github.io?style=flat-square)](https://github.com/gnosyslambda/gnosyslambda.github.io)
[![Posts](https://img.shields.io/badge/Posts-15+-blue?style=flat-square)](#categories)
[![Hugo](https://img.shields.io/badge/Hugo-0.157.0-FF4088?style=flat-square&logo=hugo)](https://gohugo.io/)

[**블로그 바로가기**](https://gnosyslambda.github.io/) · [**About**](https://gnosyslambda.github.io/about/) · [**Archives**](https://gnosyslambda.github.io/archives/) · [**RSS**](https://gnosyslambda.github.io/index.xml)

</div>

---

## What is this?

대규모 시스템 설계, AI 엔지니어링, 오픈소스 생태계에 대한 **심층 기술 분석 블로그**입니다.

해외 빅테크(Google, Meta, Netflix, Uber, Cloudflare, OpenAI)의 엔지니어링 블로그와 최신 기술 동향을 분석하고, **한국 실무 환경에서의 적용 가능성과 트레이드오프**를 비판적으로 다룹니다.

> *트렌드를 좇기보다, "왜 그 기술이 필요한지"를 먼저 묻습니다.*

---

## Categories

| Category | Description | Posts |
|:---------|:------------|:-----:|
| **글로벌 테크 인사이트** | 빅테크 엔지니어링 블로그 심층 분석 및 한국 실무 적용 | 7 |
| **시스템 설계 Deep Dive** | 대규모 분산 시스템 아키텍처 패턴 | 1 |
| **개발자 도구 & DX** | 개발 생산성, 플랫폼 엔지니어링, AI 코딩 도구 | 2 |
| **보안 & 신뢰성 엔지니어링** | 공급망 보안, SRE, 컴플라이언스 | 1 |
| **AI 엔지니어링 실전** | LLM 인프라, RAG/Fine-tuning, MLOps | 1 |
| **오픈소스 해부학** | 주요 오픈소스 프로젝트 내부 구조 분석 | 2 |

---

## Featured Posts

- [**OpenAI는 어떻게 PostgreSQL 하나로 8억 사용자를 감당하는가**](https://gnosyslambda.github.io/posts/2026-01-28-openai-scaling-postgresql-chatgpt/) — 단일 프라이머리 + 50대 읽기 복제본 아키텍처
- [**Cloudflare가 인터넷의 20%를 Rust로 다시 쓴 이유**](https://gnosyslambda.github.io/posts/2025-10-08-cloudflare-rust-internet-infrastructure/) — NGINX → Rust 마이그레이션의 교훈
- [**Uber는 어떻게 수십억 건의 결제를 "딱 한 번만" 처리하는가**](https://gnosyslambda.github.io/posts/2025-10-30-uber-exactly-once-payment-processing/) — 멱등성, Outbox 패턴, Temporal
- [**Claude Code 플러그인 생태계 해부**](https://gnosyslambda.github.io/posts/2025-10-18-claude-code-plugins-skills-ecosystem/) — Skills, MCP, Hooks, Subagents 확장성 분석
- [**72시간 만에 GitHub 6만 스타, OpenClaw의 모든 것**](https://gnosyslambda.github.io/posts/2026-02-28-openclaw-open-source-ai-agent/) — 오픈소스 AI 에이전트의 부상과 지속 가능성

---

## Writing Style

각 포스트는 동일한 구조를 따릅니다:

```
## 왜 지금 이게 문제인가    → 기술적 배경과 필요성
## 어떻게 동작하는가        → 아키텍처, 코드 예시, 다이어그램
## 실제로 써먹을 수 있는가   → 도입 판단 기준, 트레이드오프, 운영 리스크
## 한 줄로 남기는 생각      → 핵심 인사이트
```

**원칙:**
- 기술을 소개하는 데 그치지 않고, **한국 실무 환경에서의 적용 가능성**을 냉정하게 분석합니다.
- 코드 예시, Mermaid 다이어그램, 비교 테이블을 포함하여 **엔지니어가 바로 판단할 수 있는 수준**의 깊이를 목표로 합니다.
- 모든 분석에는 **도입 권장 / 비권장 상황**과 **운영 리스크**를 반드시 포함합니다.

---

## Tech Stack

| Layer | Technology |
|:------|:-----------|
| **Static Site Generator** | [Hugo](https://gohugo.io/) v0.157.0 (Extended) |
| **Theme** | [PaperMod](https://github.com/adityatelange/hugo-PaperMod) (Dark mode default) |
| **Hosting** | GitHub Pages |
| **CI/CD** | GitHub Actions — push 시 자동 빌드 & 배포 |
| **Analytics** | Google Analytics 4 |
| **Search** | Fuse.js 기반 클라이언트 사이드 검색 |

---

## Local Development

```bash
# 저장소 클론 (서브모듈 포함)
git clone --recursive https://github.com/gnosyslambda/gnosyslambda.github.io.git
cd gnosyslambda.github.io

# 개발 서버 실행
hugo server -D

# 프로덕션 빌드
hugo --gc --minify
```

---

## License

콘텐츠(블로그 포스트)는 [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) 라이선스를 따릅니다.
코드는 MIT 라이선스를 따릅니다.
