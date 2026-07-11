#!/usr/bin/env python3
"""Regression checks for the trend writer's KST daily output cap."""

import sys
import tempfile
import types
from pathlib import Path

# The cap helpers are pure filesystem/date logic. Stub optional runtime-only
# RSS dependencies so this regression check runs with macOS system Python too.
sys.modules.setdefault("feedparser", types.ModuleType("feedparser"))
sys.modules.setdefault("requests", types.ModuleType("requests"))
bs4 = sys.modules.setdefault("bs4", types.ModuleType("bs4"))
if not hasattr(bs4, "BeautifulSoup"):
    setattr(bs4, "BeautifulSoup", object)
dateutil = sys.modules.setdefault("dateutil", types.ModuleType("dateutil"))
parser = sys.modules.setdefault("dateutil.parser", types.ModuleType("dateutil.parser"))
if not hasattr(parser, "parse"):
    setattr(parser, "parse", lambda value: value)
setattr(dateutil, "parser", parser)

import trend_writer as writer

original_posts_dir = writer.POSTS_DIR
original_daily_limit = writer.DAILY_POST_LIMIT

try:
    with tempfile.TemporaryDirectory() as tmp:
        writer.POSTS_DIR = Path(tmp)
        writer.DAILY_POST_LIMIT = 2
        today = writer.current_kst_date_prefix()

        assert writer.daily_post_count() == 0
        assert writer.daily_post_slots_remaining() == 2

        (writer.POSTS_DIR / f"{today}-first.md").write_text("---\n---\n", encoding="utf-8")
        assert writer.daily_post_count() == 1
        assert writer.daily_post_slots_remaining() == 1

        (writer.POSTS_DIR / f"{today}-second.md").write_text("---\n---\n", encoding="utf-8")
        assert writer.daily_post_count() == 2
        assert writer.daily_post_slots_remaining() == 0

        (writer.POSTS_DIR / "2020-01-01-old.md").write_text("---\n---\n", encoding="utf-8")
        assert writer.daily_post_count() == 2
finally:
    writer.POSTS_DIR = original_posts_dir
    writer.DAILY_POST_LIMIT = original_daily_limit
