#!/usr/bin/env python3
"""Small wiring check for the trend writer humanizer gate."""

import ast
from pathlib import Path


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


source = Path(__file__).with_name("trend_writer.py").read_text(encoding="utf-8")
tree = ast.parse(source)

assert "def humanize_post" in source
assert "def build_quality_gated_post" in source
assert "def build_quality_feedback" in source
assert "def build_revision_feedback" in source
assert "def evaluate_post" in source
assert "def parse_post_evaluation" in source
assert "def run_codex_prompt" in source
assert "QUALITY_RUBRIC" in source
assert "POST_VARIANTS" in source
assert "POST_REPAIR_VARIANTS" in source
assert "POST_POLISH_MIN_SCORE" in source
assert "POST_JUDGES" in source
assert "TOPICS_PER_RUN" in source
assert "POST_MIN_SCORE" in source
assert "POST_REVIEW_MIN_SCORE" in source
assert "POST_MAX_ROUNDS" in source
assert "CANDIDATE_TIMEOUT_SECONDS" in source
assert "JUDGE_TIMEOUT_SECONDS" in source
assert "QUALITY_HISTORY_PATH" in source
assert "def terminate_process_group" in source
assert "def build_quality_prompt_memory" in source
assert "def choose_quality_strategy" in source
assert "def build_quality_record" in source
assert "def select_articles_for_run" in source
assert "def create_post_for_article" in source
assert "def save_review_candidate" in source
assert "def save_review_candidates" in source
assert "local_drafts" in source
assert "start_new_session=True" in source
assert "candidate_hash" in source
assert "saved_post_relpath" in source
assert "ThreadPoolExecutor" in source
assert "is_polish_round" in source
assert "variant_count = POST_VARIANTS if round_index == 1 else (1 if is_polish_round else POST_REPAIR_VARIANTS)" in source
assert "feedback = build_revision_feedback(best)" in source
assert '"publishable"' in source
assert '"blockers"' in source
assert '"top_3_fixes"' in source
assert "품질 게이트" in source
assert "def parse_args" in source
assert "def configure_track" in source
assert "def filter_feeds_for_track" in source
assert "def post_structure" in source
assert "def fetch_github_trending_articles" in source
assert "GitHub Trending" in source
assert "articles.extend(fetch_github_trending_articles())" in source
assert "fetch_github_issue_articles" not in source
assert "GitHub Issues" not in source
assert "api.github.com/repos" not in source
assert "judge 피드백으로 다음 라운드 재작성" in source
assert "humanizer 스킬 기준" in source
assert "frontmatter, 설명, 변경 요약은 쓰지 말 것" in source
assert "현재 트랙에 맞는 글감" in source
assert "회사 기술 블로그나 공식 문서는 주장 검증과 사례 보강에만 사용" in source
assert "issue 트랙" in source
assert "tech 트랙" in source
# Named failure taxonomy: rubric and judges must reference concrete pattern codes.
assert "실패 패턴 점검 목록" in source
assert "stale_structure" in source
assert "weak_community_angle" in source
assert "thin_evidence" in source
assert "generic_conclusion" in source
assert "missing_counterpoint" in source
assert "single_source_summary" in source
assert "ai_tone" in source
assert "구조 다양성 원칙" in source
# Flexible functional-beat structure replaced the fixed H2 skeleton.
assert "기능적 비트" in source
assert "## 왜 지금 이슈인가" not in source
assert "## 커뮤니티에서 갈리는 지점" not in source
assert "## 아키텍처 관점에서 볼 점" not in source
assert "## 실무에서 볼 점" not in source
assert "## 무슨 일이 있었나" not in source
# AI-tell guards exist both at generation time and in the humanizer gate.
assert source.count("상투적 전환어") >= 2
assert source.count("수사 의문문") >= 2
assert "귀추가 주목된다" in source
assert "새 사실, 새 수치, 새 경험담을 추가하지 말 것" in source
assert "대표 원문은 중심축" not in source
assert "GEMINI" not in source
assert "google.genai" not in source

post_creator = next(
    node for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "create_post_for_article"
)
creator_calls = [
    _call_name(node.func)
    for node in ast.walk(post_creator)
    if isinstance(node, ast.Call)
]

assert creator_calls.index("build_quality_gated_post") < creator_calls.index("save_post")

candidate = next(
    node for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "generate_post_candidate"
)
candidate_calls = [
    _call_name(node.func)
    for node in ast.walk(candidate)
    if isinstance(node, ast.Call)
]

assert candidate_calls.index("generate_post") < candidate_calls.index("humanize_post")

runner_source = Path(__file__).with_name("n8n_codex_blog_runner.py").read_text(encoding="utf-8")
collector_source = Path(__file__).with_name("collect_topics.py").read_text(encoding="utf-8")

assert "collectOnly" in runner_source
assert "scripts/collect_topics.py" in runner_source
assert "candidateCount" in runner_source
assert "COLLECTED_FILE" in collector_source
assert "CANDIDATE_COUNT" in collector_source
