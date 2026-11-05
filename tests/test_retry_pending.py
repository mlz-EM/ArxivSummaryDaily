"""Exercise real controllers through delayed failures without real waits/calls."""

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import requests

from src import chronicle_cli, insidehighered_cli, retry_pending, summary_report
from src.llm_client import LLMModelClient, LLMQuotaExhausted, LLMRequestBudgetExhausted, LLMServiceUnavailable
from src.madgex_client import MadgexStore


def response(status, payload, headers=None):
    value = mock.Mock(status_code=status, headers=headers or {})
    value.json.return_value = payload
    return value


def completion(accepted=(), rejected=()):
    text = json.dumps({"accepted": list(accepted), "rejected": list(rejected)})
    return response(200, {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}]})


def accepted(number):
    return {"url": f"https://jobs.chronicle.com/job/{number}", "fitScore": 2,
            "description": "Materials characterization faculty opportunity.", "keywords": ["materials"],
            "location": "Example University at Boston, MA, US"}


def queue(directory, prefix, numbers):
    store = MadgexStore(Path(directory) / (prefix + "JobsPending.json"), Path(directory) / (prefix + "ScanState.json"))
    for number in numbers:
        store.add_job({"id": number, "title": "Assistant Professor of Materials Science",
                       "institution": "Example University", "location": "Boston, MA, US",
                       "postedDate": "2026-11-05", "description": "Materials characterization research and engineering teaching."})
    store.save()


class TestDelayedQueueRecovery(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.now = datetime(2026, 11, 5, 14, tzinfo=timezone.utc).timestamp()
        clock = self

        class SimulatedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(clock.now, tz)

        self.stack.enter_context(mock.patch.dict(os.environ, {
            "LLM_QUOTA_STATE_FILE": str(Path(self.directory) / "llm-state.json"), "GITHUB_RUN_ID": "retry-test",
        }))
        self.stack.enter_context(mock.patch.dict("src.llm_client.LLM_CONFIG", {
            "api_key": "dummy", "model": "gemini-3.8-flash", "fallback_models": [],
            "retry_count": 2, "service_retry_delays": [300, 900, 1800], "retry_jitter_seconds": 0,
            "max_requests_per_model": 18, "max_service_failures_per_model": 4, "max_run_seconds": 5400,
        }))
        self.stack.enter_context(mock.patch("src.llm_client.datetime", SimulatedDatetime))
        self.stack.enter_context(mock.patch("src.llm_client.time.time", side_effect=lambda: self.now))
        self.stack.enter_context(mock.patch("src.retry_pending.time.monotonic", side_effect=lambda: self.now))
        self.stack.enter_context(mock.patch("src.retry_pending.time.sleep", side_effect=self.advance))

    def advance(self, seconds):
        self.now += seconds

    def test_completed_acceptance_and_rejection_survive_outage_then_only_pending_jobs_retry(self):
        queue(self.directory, "chronicle", [1, 2, 3])
        queue(self.directory, "insideHigherEd", [4])
        responses = [
            completion(accepted=[accepted(1)]),
            completion(rejected=["https://jobs.chronicle.com/job/2"]),
            response(503, {"error": {"message": "high demand"}}),
            completion(accepted=[accepted(3)]),
            completion(rejected=["https://careers.insidehighered.com/job/4"]),
        ]
        with mock.patch("src.llm_client.requests.post", side_effect=responses) as post:
            self.assertEqual(chronicle_cli.main(["--output-dir", self.directory, "summarize", "--batch-size", "1"]), 1)
            self.assertEqual(insidehighered_cli.main(["--output-dir", self.directory, "summarize"]), 1)
            self.assertEqual(post.call_count, 3)  # The second source respects the active cooldown.
            self.assertEqual(retry_pending.main(["--output-dir", self.directory]), 0)
        self.assertEqual(post.call_count, 5)
        retry_prompts = [call.kwargs["json"]["contents"][0]["parts"][0]["text"] for call in post.call_args_list[3:]]
        self.assertTrue(all("https://jobs.chronicle.com/job/1" not in text and "https://jobs.chronicle.com/job/2" not in text for text in retry_prompts))
        report = json.loads(Path(self.directory, "chronicleJobsSummaryReport.json").read_text())
        self.assertEqual((report["accepted"], report["rejected"], report["inputJobs"], report["unsummarized"]), (2, 1, 3, 0))
        self.assertEqual(report["summaryPasses"], 2)
        items = json.loads(Path(self.directory, "chronicleJobsSummary.json").read_text())["items"]
        self.assertTrue(all(item["isNew"] for item in items))
        self.assertFalse(any(retry_pending.pending_counts(self.directory).values()))
        retry = json.loads(Path(self.directory, "summaryRetryReport.json").read_text())
        self.assertEqual(len(retry["rounds"]), 1)
        self.assertGreaterEqual(retry["rounds"][0]["waitSeconds"], 300)
        self.assertEqual(summary_report.main(["--output-dir", self.directory, "--require-complete"]), 0)

    def test_persistent_503_has_three_delayed_passes_and_leaves_work_pending(self):
        queue(self.directory, "chronicle", [1])
        with mock.patch.dict("src.llm_client.LLM_CONFIG", {"fallback_models": ["gemini-3.6-flash", "gemini-3.5-flash-lite"]}), mock.patch(
            "src.llm_client.requests.post", return_value=response(503, {"error": {"message": "high demand"}})
        ) as post:
            self.assertEqual(chronicle_cli.main(["--output-dir", self.directory, "summarize"]), 1)
            self.assertEqual(retry_pending.main(["--output-dir", self.directory]), 1)
            self.assertEqual(post.call_count, 12)  # Four attempts per model, across all sources/passes.
            self.assertIsNone(LLMModelClient("dummy").next_retry_delay())
        report = json.loads(Path(self.directory, "summaryRetryReport.json").read_text())
        self.assertEqual([r["waitSeconds"] for r in report["rounds"]], [300, 900, 1800])
        self.assertEqual(report["remainingQueued"]["Chronicle"], 1)
        self.assertEqual(json.loads(Path(self.directory, "chronicleJobsSummary.json").read_text())["items"], [])
        self.assertEqual(summary_report.main(["--output-dir", self.directory, "--require-complete"]), 1)

    def test_request_budget_is_shared_across_successful_source_clients(self):
        with mock.patch.dict("src.llm_client.LLM_CONFIG", {"max_requests_per_model": 2}), mock.patch(
            "src.llm_client.requests.post", return_value=completion()
        ) as post:
            for _ in range(2):
                LLMModelClient("dummy").chat_completion([{"content": "source"}])
            with self.assertRaises(LLMRequestBudgetExhausted):
                LLMModelClient("dummy").chat_completion([{"content": "another source"}])
        self.assertEqual(post.call_count, 2)

    def test_primary_timeout_uses_fallback_and_counts_the_failed_attempt(self):
        with mock.patch.dict("src.llm_client.LLM_CONFIG", {"fallback_models": ["gemini-3.6-flash"]}), mock.patch(
            "src.llm_client.requests.post", side_effect=[requests.Timeout(), completion()]
        ) as post:
            client = LLMModelClient("dummy")
            client.chat_completion([{"content": "real payload"}])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(client.models_used, ["gemini-3.6-flash"])
        self.assertEqual(client.runtime_snapshot()["requests"], {"gemini-3.8-flash": 1, "gemini-3.6-flash": 1})
        self.assertEqual(client.stop_reason, "")

    def test_cooldown_larger_than_remaining_budget_is_not_waited_out(self):
        queue(self.directory, "chronicle", [1])
        with mock.patch.dict("src.llm_client.LLM_CONFIG", {"max_run_seconds": 100}), mock.patch(
            "src.llm_client.requests.post", return_value=response(503, {"error": {"message": "high demand"}})
        ) as post:
            self.assertEqual(chronicle_cli.main(["--output-dir", self.directory, "summarize"]), 1)
            started = self.now
            self.assertEqual(retry_pending.main(["--output-dir", self.directory]), 1)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.now, started)
        report = json.loads(Path(self.directory, "summaryRetryReport.json").read_text())
        self.assertEqual(report["rounds"], [])

    def test_elapsed_budget_prevents_later_source_requests(self):
        client = LLMModelClient("dummy")
        self.assertIsNotNone(client.next_retry_delay())  # Starts the shared elapsed-time budget.
        self.advance(5401)
        with mock.patch("src.llm_client.requests.post") as post:
            with self.assertRaises(LLMRequestBudgetExhausted):
                LLMModelClient("dummy").chat_completion([{"content": "too late"}])
        post.assert_not_called()
        self.assertIsNone(client.next_retry_delay())

    def test_long_minute_limit_is_retryable_and_not_daily_quota(self):
        with mock.patch("src.llm_client.requests.post", side_effect=[
            response(429, {"error": {"message": "Requests per minute exceeded"}}, {"Retry-After": "120"}), completion(),
        ]) as post:
            client = LLMModelClient("dummy")
            with self.assertRaises(LLMQuotaExhausted):
                client.chat_completion([{"content": "first"}])
            self.assertEqual(client.next_retry_delay(), 120)
            self.advance(120)
            client.chat_completion([{"content": "retry"}])
        self.assertEqual(post.call_count, 2)

    def test_daily_quota_does_not_schedule_another_provider_attempt(self):
        queue(self.directory, "chronicle", [1])
        with mock.patch("src.llm_client.requests.post", return_value=response(429, {"error": {
            "message": "Requests per day exhausted", "details": [{"retryDelay": "1s"}],
        }})) as post:
            self.assertEqual(chronicle_cli.main(["--output-dir", self.directory, "summarize"]), 1)
            self.assertEqual(retry_pending.main(["--output-dir", self.directory]), 1)
        self.assertEqual(post.call_count, 1)
        report = json.loads(Path(self.directory, "summaryRetryReport.json").read_text())
        self.assertEqual(report["rounds"], [])
        self.assertIn("daily quota", report["stopReason"])


if __name__ == "__main__":
    unittest.main()
