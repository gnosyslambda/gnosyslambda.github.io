#!/usr/bin/env python3
"""Validate Seoul Picks n8n workflow export."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / "local_drafts" / "n8n_workflows" / "seoul_picks_automation.json"


class SeoulPicksWorkflowTests(unittest.TestCase):
    def test_workflow_runs_hourly_during_publish_window_and_is_active(self):
        workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))

        self.assertTrue(workflow["active"])
        schedule = next(node for node in workflow["nodes"] if node["id"] == "seoul-publish-hourly")
        self.assertEqual(schedule["typeVersion"], 1.3)
        interval = schedule["parameters"]["rule"]["interval"][0]
        self.assertEqual(interval["field"], "cronExpression")
        self.assertEqual(interval["expression"], "0 0 9-22 * * *")

    def test_http_request_publishes_live_category_rotation(self):
        workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))

        http = next(node for node in workflow["nodes"] if node["id"] == "run-seoul-picks")

        self.assertEqual(http["parameters"]["method"], "POST")
        self.assertEqual(http["parameters"]["url"], "http://host.docker.internal:8771/run")
        body = http["parameters"]["jsonBody"]
        self.assertIn(
            "categories = ['Skincare','Makeup','K-Food','Lifestyle','K-Tech','K-Entertainment','Korea Travel','K-Culture']",
            body,
        )
        self.assertIn("variants: 1", body)
        self.assertIn("judges: 1", body)
        self.assertIn("minScore: 95", body)
        self.assertIn("reviewMinScore: 90", body)
        self.assertIn("dryRun: false", body)
        self.assertIn("publish: true", body)
        self.assertIn("sendToBlogger: true", body)
        self.assertIn("isDraft: false", body)
        self.assertIn("category: categories[index]", body)

    def test_alert_gate_suppresses_telegram_during_quiet_hours(self):
        workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        alert = next(node for node in workflow["nodes"] if node["id"] == "build-alert")
        code = alert["parameters"]["jsCode"]

        self.assertIn("const quietStartHour = 23;", code)
        self.assertIn("const quietEndHour = 9;", code)
        self.assertIn("const shouldNotify = !quietHours", code)


if __name__ == "__main__":
    unittest.main()
