"""Keyless HTTP smoke checks for a running local Compose prototype."""

import hashlib
import json
from pathlib import Path
import re
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE = "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]


def get_json(path: str) -> dict:
    with urlopen(BASE + path, timeout=10) as response:
        return json.load(response)


class WebSmokeTest(unittest.TestCase):
    def test_demo_source_and_downloads_match_saved_materials(self):
        config = get_json("/api/config")
        self.assertFalse(config["configured"], "Run this smoke check without a key.")
        self.assertTrue(config["demoReplay"])

        bundle = get_json("/api/demo/report")
        expected_json = (ROOT / "reports/submission.json").read_bytes()
        expected_md = (ROOT / "reports/submission.md").read_bytes()
        pdf = (ROOT / "web-demo/requirements.pdf").read_bytes()
        source = bundle["source"]
        self.assertEqual(bundle["report"], json.loads(expected_json))
        self.assertEqual(source["sha256"], hashlib.sha256(pdf).hexdigest())
        self.assertEqual(source["sha256"], bundle["report"]["document"]["sha256"])
        self.assertEqual(source["lines"][28]["number"], 29)
        self.assertEqual(source["lines"][28]["page"], 1)

        for kind, expected in (("md", expected_md), ("json", expected_json)):
            with self.subTest(kind=kind):
                with urlopen(f"{BASE}/api/demo/download/{kind}", timeout=10) as response:
                    self.assertIn("attachment", response.headers["Content-Disposition"])
                    self.assertEqual(response.read(), expected)

    def test_replay_has_original_timing_and_passport(self):
        replay = get_json("/api/demo/replay")
        original = (ROOT / "web-demo/progress.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(replay["reviewedAt"], "2026-11-27T01:43:54+05:00")
        self.assertEqual(len(replay["events"]), len(original))
        for event, line in zip(replay["events"], original, strict=True):
            with self.subTest(line=line):
                match = re.fullmatch(r"\[\s*(\d+(?:\.\d+)?)\s+с\]\s+(.+)", line)
                self.assertIsNotNone(match)
                self.assertEqual(event["elapsed"], float(match[1]))
                self.assertEqual(event["text"], match[2])

    def test_cannot_start_analysis_without_key(self):
        params = urlencode({"filename": "sample.txt", "concurrency": 1, "consent": "yes"})
        request = Request(
            f"{BASE}/api/runs?{params}",
            data="Тестовое требование".encode(),
            headers={"Content-Type": "application/octet-stream"},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=10)
        with caught.exception as response:
            self.assertEqual(response.code, 503)
            self.assertTrue(json.load(response)["error"])


if __name__ == "__main__":
    unittest.main()
