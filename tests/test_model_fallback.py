"""Verify per-model deferral, fallback attribution and cross-process reuse."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.llm_client import LLMModelClient, LLMServiceUnavailable
from src.paper_summarizer import PaperSummarizer


def response(status, payload):
    value = mock.Mock(status_code=status, headers={})
    value.json.return_value = payload
    return value


def success():
    return response(200, {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"ok":true}'}]}}]})


class TestFreeModelFallback(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("src.llm_client.LLM_CONFIG", {
            "fallback_models": ["gemini-3.5-flash-lite"], "retry_count": 1,
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_overloaded_primary_falls_back_and_next_source_skips_it(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            "LLM_QUOTA_STATE_FILE": str(Path(directory) / "state.json"), "GITHUB_RUN_ID": "unit-run",
        }), mock.patch("src.llm_client.requests.post", side_effect=[
            response(503, {"error": {"message": "high demand"}}), success(), success(),
        ]) as post:
            first = LLMModelClient("dummy", "gemini-3.6-flash")
            first.chat_completion([{"content": "source1"}])
            second = LLMModelClient("dummy", "gemini-3.6-flash")
            second.chat_completion([{"content": "source2"}])
            self.assertEqual(post.call_count, 3)
            self.assertIn("gemini-3.6-flash", post.call_args_list[0].args[0])
            self.assertTrue(all("gemini-3.5-flash-lite" in call.args[0] for call in post.call_args_list[1:]))
            self.assertEqual(first.model, "gemini-3.5-flash-lite")
            self.assertEqual(first.models_used, ["gemini-3.5-flash-lite"])
            self.assertEqual(first.stop_reason, "")
            self.assertEqual(len(first.fallback_events), 1)

    def test_all_models_keep_independent_cooldowns_without_repeating_requests(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            "LLM_QUOTA_STATE_FILE": str(Path(directory) / "state.json"),
        }), mock.patch("src.llm_client.requests.post", return_value=response(503, {"error": {"message": "high demand"}})) as post:
            for _ in range(2):
                with self.assertRaises(LLMServiceUnavailable):
                    LLMModelClient("dummy", "gemini-3.6-flash").chat_completion([{"content": "hello"}])
            self.assertEqual(post.call_count, 2)
            state = json.loads((Path(directory) / "state.json").read_text())
            self.assertEqual(set(state["models"]), {"gemini-3.6-flash", "gemini-3.5-flash-lite"})

    def test_primary_daily_quota_uses_fallback_without_retrying_exhausted_model(self):
        quota = response(429, {"error": {"message": "Requests per day exhausted", "details": [
            {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
            {"retryDelay": "7200s"},
        ]}})
        with mock.patch.dict(os.environ, {"LLM_QUOTA_STATE_FILE": ""}), mock.patch(
            "src.llm_client.requests.post", side_effect=[quota, success()]
        ) as post:
            client = LLMModelClient("dummy", "gemini-3.6-flash")
            client.chat_completion([{"content": "hello"}])
            self.assertEqual(post.call_count, 2)
            self.assertEqual(client.model, "gemini-3.5-flash-lite")
            self.assertEqual(client.stop_reason, "")

    def test_expired_provider_cooldown_remains_blocked_within_same_action_run(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            "LLM_QUOTA_STATE_FILE": str(Path(directory) / "state.json"), "GITHUB_RUN_ID": "same-run",
        }), mock.patch("src.llm_client.requests.post", return_value=success()) as post:
            (Path(directory) / "state.json").write_text(json.dumps({"schemaVersion": 2, "models": {
                "gemini-3.6-flash": {"reason": "outage", "retryAt": "2020-01-01T00:00:00+00:00", "errorType": "LLMServiceUnavailable", "runId": "same-run"},
            }}))
            client = LLMModelClient("dummy", "gemini-3.6-flash")
            client.chat_completion([{"content": "hello"}])
            self.assertEqual(post.call_count, 1)
            self.assertIn("gemini-3.5-flash-lite", post.call_args.args[0])

    def test_legacy_single_model_marker_is_respected(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            "LLM_QUOTA_STATE_FILE": str(Path(directory) / "state.json"),
        }), mock.patch("src.llm_client.requests.post", return_value=success()) as post:
            (Path(directory) / "state.json").write_text(json.dumps({"model": "gemini-3.6-flash", "reason": "daily quota", "retryAt": "2099-01-01T00:00:00+00:00", "errorType": "LLMQuotaExhausted"}))
            LLMModelClient("dummy", "gemini-3.6-flash").chat_completion([{"content": "hello"}])
            self.assertEqual(post.call_count, 1)
            self.assertIn("gemini-3.5-flash-lite", post.call_args.args[0])

    def test_failed_paper_summary_does_not_create_a_completed_fallback_record(self):
        paper = {"title": "Microscopy", "entry_id": "https://arxiv.org/abs/2611.00001", "published": "2026-11-04", "summary": "Full source abstract", "authors": [], "categories": []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "papers.json"
            summarizer = PaperSummarizer("dummy", "test-model")
            with mock.patch.object(summarizer, "_generate_batch_summary", return_value=([], False)):
                self.assertFalse(summarizer.summarize_papers([paper], str(path)))
            self.assertEqual(json.loads(path.read_text())["items"], [])

    def test_blank_paper_response_remains_unfinished(self):
        paper = {"title": "Microscopy", "entry_id": "https://arxiv.org/abs/2611.00001", "published": "2026-11-04", "summary": "Source abstract", "authors": [], "categories": []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "papers.json"
            summarizer = PaperSummarizer("dummy", "test-model")
            with mock.patch.object(summarizer, "_generate_batch_summary", return_value=([{"url": paper["entry_id"], "summary": ""}], True)):
                self.assertFalse(summarizer.summarize_papers([paper], str(path)))
            self.assertEqual(json.loads(path.read_text())["items"], [])


if __name__ == "__main__":
    unittest.main()
