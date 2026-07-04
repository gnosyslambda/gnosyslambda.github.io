#!/usr/bin/env python3
"""Collect topic candidates without generating or publishing posts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import trend_writer


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SNAPSHOT_DIR = REPO_ROOT / "local_drafts" / "topic_snapshots"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect blog topic candidates only.")
    parser.add_argument("--track", choices=sorted(trend_writer.TRACKS), default=trend_writer.DEFAULT_TRACK)
    parser.add_argument("--limit", type=int, default=30)
    return parser.parse_args()


def article_uid(article: dict[str, Any]) -> str:
    if hasattr(trend_writer, "article_uid"):
        return trend_writer.article_uid(article)
    return str(article.get("uid") or article.get("link") or article.get("title") or "")


def compact_article(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": article_uid(article),
        "source": article.get("source", ""),
        "title": article.get("title", ""),
        "link": article.get("link", ""),
        "published": article.get("published", ""),
        "summary": article.get("summary", "")[:500],
    }


def main() -> None:
    args = parse_args()
    track = args.track
    limit = max(1, args.limit)

    trend_writer.configure_track(track)
    feeds = trend_writer.filter_feeds_for_track(trend_writer.load_feeds())
    articles = trend_writer.fetch_recent_articles(feeds)
    if track == "issue" and hasattr(trend_writer, "fetch_github_trending_articles"):
        articles.extend(trend_writer.fetch_github_trending_articles())

    seen = trend_writer.load_seen()
    fresh = [article for article in articles if article_uid(article) not in seen]
    candidates = fresh[:limit]

    now = datetime.now(timezone.utc)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    relpath = Path("local_drafts") / "topic_snapshots" / f"{now.strftime('%Y%m%d-%H%M%S')}-{track}.json"
    outpath = REPO_ROOT / relpath
    outpath.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "track": track,
                "collectedAt": now.isoformat(),
                "feedCount": len(feeds),
                "totalCount": len(articles),
                "freshCount": len(fresh),
                "candidates": [compact_article(article) for article in candidates],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"COLLECTED_FILE={relpath.as_posix()}")
    print(f"CANDIDATE_COUNT={len(candidates)}")
    print(f"FRESH_COUNT={len(fresh)}")
    for index, article in enumerate(candidates[:5], start=1):
        print(f"CANDIDATE_{index}={article.get('source', '')} | {article.get('title', '')}")


if __name__ == "__main__":
    main()
