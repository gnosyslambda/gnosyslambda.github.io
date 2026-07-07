---
date: "2026-07-07T12:12:09+09:00"
draft: false
title: "AI 랜섬웨어 첫 사례, 인간 개입 논란"
tags:
  - "백엔드"
  - "아키텍처"
  - "개발"
  - "AI-랜섬웨어"
  - "AI-에이전트-사이버-공격"
  - "랜섬웨어-인간-개입"
  - "JadePuffer-사례"
  - "startup"
categories:
  - "스타트업 · 비즈니스"
description: "첫 agentic ransomware 사례로 불린 JadePuffer가 왜 논란이 됐는지 정리합니다. AI 실행과 인간의 피해자 선정·인프라 준비를 구분합니다."
source:
  name: "TechCrunch"
  url: "https://techcrunch.com/2026/07/06/the-first-ai-run-ransomware-attack-still-needed-a-human/"
  title: "The ‘first’ AI-run ransomware attack still needed a human"
cover:
  image: "https://techcrunch.com/wp-content/uploads/2026/07/Ransomware-attack.png?resize=1200,686"
  alt: "Cover image"
  relative: false
showToc: true
TocOpen: true
---

AI 랜섬웨어라는 말이 먼저 달렸다. Sysdig가 공개한 JadePuffer 사례는 실제 침해에서 AI 에이전트가 기술 실행을 맡은 첫 알려진 사례로 소개됐다. 중요한 차이는 따로 있다. 인간이 키보드에서 물러난 것과 인간이 공격에서 사라진 것은 같은 말이 아니다.

## AI가 공격한 것은 맞다, 공격을 고른 것은 사람이었다

확인된 사실부터 보자. 2026년 7월 6일 TechCrunch 보도에 따르면 Sysdig는 JadePuffer라는 공격에서 AI 에이전트가 취약 서버 침투, 자격 증명 탈취, 내부 이동, 파일 암호화, 랜섬 노트 작성까지 수행했다고 설명했다.

진입점은 Langflow의 알려진 취약점이었다. Langflow는 LLM 앱을 만드는 오픈소스 도구다. 에이전트는 이후 프로덕션 MySQL 서버로 이동했고, 또 다른 알려진 결함을 이용해 관리자 권한을 얻었다. 1,300개가 넘는 설정 레코드를 암호화했고, 비트코인 주소가 담긴 랜섬 노트도 남겼다.

여기까지는 AI 실행이 맞다.

다만 Sysdig의 Michael Clark은 CyberScoop 인터뷰에서 사람이 여전히 피해자를 골랐고, 명령제어 서버와 스테이징 서버 같은 인프라를 마련했으며, 공격에 쓰인 데이터베이스 자격 증명도 별도 선행 침해로 확보된 뒤 제공됐다고 정정했다. TechCrunch가 추가 확인한 내용도 같은 방향이다. 공격에서 발견된 OpenAI, Anthropic, DeepSeek, Gemini 키는 여러 모델이 공격을 나눠 수행했다는 증거가 아니라, 에이전트가 훔친 전리품에 가까웠다.

아직 확인되지 않은 부분도 있다. Sysdig는 JadePuffer를 실제로 구동한 모델을 특정하지 못했다. 시스템 프롬프트와 설정도 확인하지 못했다. Microsoft 연구자 Geoff McDonald는 안전장치가 제거된 오픈웨이트 모델 가능성을 제기했지만, 관찰에 기반한 추정이지 확인된 결론은 아니다.

## 커뮤니티가 불편해한 지점은 자동화가 아니라 책임의 빈칸이다

이 사건이 보안 커뮤니티에서 말이 붙는 이유는 첫 AI 랜섬웨어라는 간판 때문만이 아니다. 더 찝찝한 부분은 역할 분리가 너무 깔끔해졌다는 데 있다.

사람은 목표를 정한다. AI는 손을 움직인다.

기존 랜섬웨어도 서비스형 랜섬웨어(Ransomware-as-a-Service)로 분업화되어 있었다. 침투 브로커, 운영자, 협상 담당, 세탁 담당이 나뉘었다. JadePuffer는 이 분업에서 기술 실행자의 일부를 에이전트로 바꾼 사례다. 공격의 도덕적 책임은 흐려지지 않는데, 실행 비용과 반복 가능성은 낮아진다.

TechCrunch 보도에서 특히 눈에 띄는 장면은 에이전트가 실패한 로그인을 31초 만에 수정했다는 대목이다. 초인적인 해킹 기술이라기보다, 운영자가 밤새 붙잡고 하던 작은 시도와 수정이 기계 속도로 반복될 수 있다는 신호다. 공격 기법은 평범했지만, 반복 속도와 관찰 가능한 추론 과정이 달랐다.

과장은 경계해야 한다. McDonald가 언급한 수천, 수만 개 동시 캠페인 가능성은 공격자 예산이 주된 한계가 될 때의 시나리오다. Clark의 설명처럼 사람이 매번 피해자를 고르고, 인프라를 세팅하고, 선행 자격 증명을 넣어야 한다면 완전 자동 대량 캠페인에는 병목이 남는다. 지금의 JadePuffer는 자율 범죄의 완성판이 아니라 자동 실행이 끼어든 중간 단계다.

```mermaid
flowchart LR
    A[Human operator] -->|chooses victim| B[Attack setup]
    A -->|provisions C2 and staging| B
    A -->|supplies stolen credentials| C[AI agent]
    B --> C
    C -->|exploits Langflow bug| D[Vulnerable server]
    C -->|moves to MySQL| E[Production database]
    C -->|encrypts records| F[Ransom impact]
    C -->|writes ransom note| G[Extortion message]
```

## 같은 주에 나온 다른 사건들이 보여주는 방향

JadePuffer를 단일 사건으로 보면 신기한 데모처럼 보인다. 같은 시기 보안 뉴스와 붙여 놓으면 의미가 바뀐다. 랜섬웨어 생태계는 이미 사람 중심 범죄에서 조직화된 플랫폼 범죄로 이동했다. AI는 그 플랫폼에 붙는 실행 레이어다.

Krebs on Security가 다룬 Scattered Spider 사건은 이 맥락을 보여준다. 영국에서 Thalha Jubair와 Owen Flowers가 Transport for London 공격과 관련해 재판 첫날 혐의를 인정했다. 보도에 따르면 이들은 Scattered Spider와 연결된 핵심 인물로 지목됐고, 미국 뉴저지 기소장에는 2022년 5월부터 2025년 9월까지 47개 미국 기관을 상대로 한 120건의 침입과 최소 1억 1,500만 달러의 랜섬 지급 피해가 언급됐다.

핵심은 AI가 없던 시기에도 범죄가 이미 확장되어 있었다는 점이다. 피해 업종은 대중교통, 의료, 유통으로 퍼졌고, 공격자는 언론 인터뷰까지 활용했다는 의혹을 받았다. AI 에이전트는 이런 집단을 대체하는 것이 아니라, 이런 집단이 더 적은 인력으로 더 많은 기술 작업을 처리하게 만드는 도구가 된다.

캐나다 CSE의 연례 보고서도 같은 그림의 다른 면을 보여준다. CSE는 해외 마약 밀매자, 극단주의 조직, 랜섬웨어 서비스 운영자를 상대로 2025년에 세 차례 외국 대상 능동 사이버 작전(Active Cyber Operations)을 수행했다고 밝혔다. 국가기관은 이제 랜섬웨어를 단순 수사 대상이 아니라 직접 방해하고 약화시켜야 할 국가안보 위협으로 다룬다.

한쪽에서는 범죄자가 자동화한다. 다른 한쪽에서는 국가가 선제적으로 교란한다.

이 둘 사이에 기업 운영자가 서 있다. 방어자는 AI 모델 이름을 맞히는 게임을 할 시간이 없다. 어떤 모델이 쓰였는지보다 어떤 권한이 노출됐고, 어떤 자동 실행 경로가 열렸고, 어떤 로그가 남았는지가 먼저다.

## 방어 기준은 모델 차단이 아니라 권한 축소로 가야 한다

JadePuffer 보도에서 실무자가 가져갈 판단은 분명하다. AI 에이전트를 막는다는 목표는 너무 크고 흐리다. 먼저 막아야 할 것은 에이전트가 훔쳐 갈 수 있는 권한과 에이전트가 자동으로 밟을 수 있는 경로다.

Langflow 같은 LLM 앱 빌더는 실험 도구처럼 배포되기 쉽다. 그런데 그 안에 모델 제공자 API 키, 클라우드 자격 증명, 데이터베이스 설정, 지갑 정보가 섞이면 공격 표면이 된다. JadePuffer에서 여러 AI 제공자 키가 발견된 사실은 공격 모델의 정체보다 운영 습관을 더 세게 찌른다. 공격자는 모델 키를 의사결정 엔진으로만 보지 않았다. 팔 수 있고, 남용할 수 있고, 다음 침투에 쓸 수 있는 자산으로 봤다.

확인할 지점은 복잡하지 않다.

- Langflow 같은 LLM 워크플로 도구가 인터넷에 직접 노출되어 있는지
- 알려진 취약점 패치가 지연된 인스턴스가 있는지
- 앱 서버에 장기 API 키와 데이터베이스 관리자 권한이 같이 놓여 있는지
- 모델 제공자 키, 클라우드 키, 데이터베이스 설정이 같은 호스트에서 읽히는지
- 실패 로그인, 권한 상승, 대량 설정 변경, 암호화성 쓰기 작업이 하나의 흐름으로 탐지되는지

이 목록은 화려하지 않다. 그래서 더 현실적이다. AI 랜섬웨어라는 이름은 새롭지만, JadePuffer가 밟은 발판은 알려진 취약점, 훔친 자격 증명, 과한 권한, 노출된 설정이었다. 방어 실패의 재료는 낯설지 않았다.

이번 사례를 완전 자율 공격이라고 부르는 것은 과했다. 그렇다고 AI 보안 우려가 전부 과장이라는 뜻은 아니다. 실행 레이어가 자동화됐다는 사실은 남는다. 보안팀이 대응해야 하는 것은 마케팅 문구가 아니라 속도다. 사람이 골라 준 한 번의 목표 안에서 에이전트가 더 빠르게 시도하고, 고치고, 훔치고, 암호화한다면 탐지와 차단의 시간표가 바뀐다.

첫 AI 랜섬웨어라는 문구는 오래가지 않을 것이다. 다음 사건에서는 첫이라는 단어가 빠지고, 더 조용한 자동화만 남을 가능성이 크다. 지금 판단해야 할 것은 AI가 범죄자가 됐는지가 아니다. 사람이 내린 공격 결정이 기계 실행을 만나면, 기존 권한 관리가 얼마나 빨리 무너지는지다.

JadePuffer는 답을 줬다. 키보드 앞의 사람은 줄어들 수 있다. 그래서 서버 안의 권한은 더 작아져야 한다.

## 참고 자료

- [선정 글감] [The ‘first’ AI-run ransomware attack still needed a human](https://techcrunch.com/2026/07/06/the-first-ai-run-ransomware-attack-still-needed-a-human/) — TechCrunch
- [관련] [Canadian spy agency says it hacked drug traffickers, extremists, and a ransomware gang last year](https://techcrunch.com/2026/07/06/canadian-spy-agency-says-it-hacked-drug-traffickers-extremists-and-a-ransomware-gang-last-year/) — TechCrunch
- [관련] [Scattered Spider Hackers Plead Guilty on Day 1 of Trial](https://krebsonsecurity.com/2026/06/scattered-spider-hackers-plead-guilty-on-day-1-of-trial/) — Krebs on Security