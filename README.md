# gnosyslambda's log 🚀

개발자 + AI 테크 블로그입니다. 코드, 아키텍처, AI 도구에 대한 실전 기록을 남깁니다.

🔗 **블로그 바로가기:** [https://gnosyslambda.github.io/](https://gnosyslambda.github.io/)

## 특징 (Features)
- **정적 웹사이트 엔진**: [Hugo](https://gohugo.io/)를 기반으로 제작되었습니다.
- **테마**: 가볍고 빠른 [PaperMod](https://github.com/adityatelange/hugo-PaperMod) 테마를 사용합니다.
- **AI 기반 자동 기술 블로그 발행 (Trend Writer)**
  - 매시간 해외 유명 테크 블로그들의 RSS 피드를 수집합니다.
  - **Google Gemini** 모델을 활용해, 수집된 기사 중 가장 가치 있는 글을 선별하고 다른 기술 블로그 레퍼런스까지 함께 엮어 한국어 분석 포스트를 자동 생성합니다.
  - GitHub Actions 워크플로우(`.github/workflows/trend_writer.yml`)를 통해 이 모든 과정이 매시간 자동으로 실행되고, 이미 다룬 기사만 남아 있으면 중복 발행을 건너뜁니다.

## 디렉토리 구조 (Structure)
- `content/`: 블로그 마크다운 포스트 및 정적 페이지 (`posts`, `about` 등)
- `scripts/`: 자동화 스크립트 (`trend_writer.py`, `feeds.json` 등)
- `.github/workflows/`: GitHub Actions 배포(Deploy) 및 포스트 자동화(Trend Writer) 파이프라인
- `hugo.yaml`: Hugo 프로젝트 통합 설정 파일

## 로컬 실행 (Local Development)
Hugo가 설치되어 있다면 다음 명령어로 로컬 환경에서 블로그를 확인할 수 있습니다.

```bash
# 서브모듈(테마) 초기화 및 업데이트
git submodule update --init --recursive

# 개발 서버 실행
hugo server -D
```
