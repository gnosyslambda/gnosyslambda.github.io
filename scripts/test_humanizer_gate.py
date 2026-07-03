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
assert "def run_codex_prompt" in source
assert "humanizer 스킬 기준" in source
assert "frontmatter, 설명, 변경 요약은 쓰지 말 것" in source
assert "커뮤니티에서 이슈화된 기술 주제" in source
assert "회사 기술 블로그나 공식 문서는 주장 검증과 사례 보강에만 사용" in source
assert "대표 원문은 글의 중심축" not in source
assert "GEMINI" not in source
assert "google.genai" not in source

main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
calls = [
    _call_name(node.value.func)
    for node in ast.walk(main)
    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
]

assert calls.index("generate_post") < calls.index("humanize_post") < calls.index("save_post")
