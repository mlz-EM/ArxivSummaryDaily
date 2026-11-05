"""Regressions for lost rejections, partial results, quota stops and recall."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.job_filter import profile_exclusion_reason
from src import chronicle_cli, insidehighered_cli
from src.madgex_client import MadgexStore
from src.interfolio_client import is_summary_candidate
from src.job_summarizer import JobSummarizer
from src.llm_client import LLMContextLimit, LLMModelClient, LLMQuotaExhausted, LLMServiceUnavailable


def job(number, title="Assistant Professor of Materials Science"):
    return {"title": title, "job_url": f"https://example.org/job/{number}",
            "description": "Research materials and teach engineering.",
            "date_posted": "2026-10-01", "company": "Example University", "location": "Boston, MA, US"}


def completion(accepted=(), rejected=()):
    return {"choices": [{"message": {"content": json.dumps({"accepted": list(accepted), "rejected": list(rejected)})}}]}


class TestJobRecovery(unittest.TestCase):
    @mock.patch("src.job_summarizer.time.sleep")
    def test_partial_progress_compacts_both_board_queues(self, sleep):
        for cli, prefix, host in (
            (chronicle_cli, "chronicle", "jobs.chronicle.com"),
            (insidehighered_cli, "insideHigherEd", "careers.insidehighered.com"),
        ):
            with self.subTest(source=prefix), tempfile.TemporaryDirectory() as directory:
                pending = Path(directory) / (prefix + "JobsPending.json")
                state = Path(directory) / (prefix + "ScanState.json")
                store = MadgexStore(pending, state)
                for number in (1, 2):
                    store.add_job({"id": number, "title": "Materials Faculty", "institution": "Example University",
                                   "location": "Boston, MA, US", "postedDate": "2026-10-01", "description": "Materials research."})
                store.save()
                summarizer = JobSummarizer("dummy", "test-model")
                summarizer.max_papers_per_batch = 1

                def generate(messages):
                    if f"https://{host}/job/1" in messages[0]["content"]:
                        return completion(rejected=[f"https://{host}/job/1"])
                    summarizer.client.stop_reason = "daily quota exhausted"
                    raise LLMQuotaExhausted("daily quota exhausted")

                with mock.patch.object(cli, "JobSummarizer", return_value=summarizer), mock.patch.dict(
                    cli.settings.LLM_CONFIG, {"api_key": "dummy", "model": "test-model"}
                ), mock.patch.object(summarizer.client, "chat_completion", side_effect=generate):
                    self.assertEqual(cli.main(["--output-dir", directory, "summarize"]), 1)
                self.assertEqual([value["id"] for value in json.loads(pending.read_text())["jobs"]], [2])
                self.assertEqual(json.loads(state.read_text())["seenIds"], [1, 2])

    @mock.patch("src.job_summarizer.time.sleep")
    def test_successful_rejection_is_checkpointed_before_later_quota_failure(self, sleep):
        jobs = [job(1), job(2), job(3)]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.json"
            summarizer = JobSummarizer("dummy", "test-model")
            summarizer.max_papers_per_batch = 1

            def generate(messages):
                if not output.exists() or not json.loads(output.read_text())["items"]:
                    return completion(rejected=[jobs[0]["job_url"]])
                # Rejection must already be durable before requesting batch 2.
                saved = json.loads(output.read_text())["items"]
                self.assertEqual(saved[0]["fitScore"], 0)
                summarizer.client.stop_reason = "daily quota exhausted"
                raise LLMQuotaExhausted("daily quota exhausted")

            with mock.patch.object(summarizer.client, "chat_completion", side_effect=generate) as request:
                self.assertFalse(summarizer.summarize_jobs(jobs, str(output)))
            self.assertEqual(request.call_count, 2)
            self.assertEqual(summarizer.completed_urls, {jobs[0]["job_url"]})
            report = json.loads((Path(directory) / "feedReport.json").read_text())
            self.assertEqual(report["rejected"], 1)
            self.assertEqual(report["unsummarized"], 2)

            second = JobSummarizer("dummy", "test-model")
            with mock.patch.object(second.client, "chat_completion", return_value=completion(
                rejected=[jobs[1]["job_url"], jobs[2]["job_url"]]
            )) as request:
                self.assertTrue(second.summarize_jobs(jobs, str(output)))
            self.assertNotIn(jobs[0]["job_url"], request.call_args.args[0][0]["content"])
            self.assertEqual(second.last_report["alreadyProcessed"], 1)

    def test_omitted_job_is_pending_rather_than_silently_rejected(self):
        jobs = [job(1), job(2)]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.json"
            summarizer = JobSummarizer("dummy", "test-model")
            with mock.patch.object(summarizer.client, "chat_completion", return_value=completion(rejected=[jobs[0]["job_url"]])):
                self.assertFalse(summarizer.summarize_jobs(jobs, str(output)))
            items = json.loads(output.read_text())["items"]
            self.assertEqual([item["url"] for item in items], [jobs[0]["job_url"]])
            self.assertEqual(summarizer.last_report["unsummarized"], 1)

    @mock.patch("src.job_summarizer.time.sleep")
    def test_provider_context_limit_splits_batch_without_losing_jobs(self, sleep):
        jobs = [job(1), job(2)]
        with tempfile.TemporaryDirectory() as directory:
            summarizer = JobSummarizer("dummy", "test-model")
            with mock.patch.object(summarizer.client, "chat_completion", side_effect=[
                LLMContextLimit("output too long"),
                completion(rejected=[jobs[0]["job_url"]]),
                completion(rejected=[jobs[1]["job_url"]]),
            ]) as request:
                self.assertTrue(summarizer.summarize_jobs(jobs, str(Path(directory) / "feed.json")))
            self.assertEqual(request.call_count, 3)
            self.assertEqual(len(summarizer.completed_urls), 2)

    def test_batches_expand_beyond_fifty_but_obey_input_and_output_budgets(self):
        summarizer = JobSummarizer("dummy", "test-model")
        with mock.patch.dict("src.job_summarizer.LLM_CONFIG", {
            "max_output_tokens": 65536, "job_output_tokens_per_item": 256, "job_batch_input_tokens": 750000,
        }):
            batches = list(summarizer._iter_batches([job(i) for i in range(600)]))
            self.assertEqual([len(batch) for batch in batches], [240, 240, 120])
        with mock.patch.dict("src.job_summarizer.LLM_CONFIG", {"job_batch_input_tokens": 2500}):
            long_jobs = [{**job(i), "description": "long description " * 300} for i in range(3)]
            self.assertEqual([len(batch) for batch in summarizer._iter_batches(long_jobs)], [1, 1, 1])


class TestQuotaStop(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("src.llm_client.LLM_CONFIG", {"fallback_models": []})
        patcher.start()
        self.addCleanup(patcher.stop)

    @mock.patch("src.llm_client.time.sleep")
    def test_provider_outage_stops_other_sources_after_bounded_retries(self, sleep):
        response = mock.Mock(status_code=503, headers={})
        response.json.return_value = {"error": {"message": "This model is experiencing high demand"}}
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            "LLM_QUOTA_STATE_FILE": str(Path(directory) / "quota.json"),
        }), mock.patch.dict("src.llm_client.LLM_CONFIG", {"retry_count": 2}), mock.patch(
            "src.llm_client.requests.post", return_value=response
        ) as post:
            with self.assertRaises(LLMServiceUnavailable):
                LLMModelClient("dummy", "gemini-3.8-flash").chat_completion([{"content": "first source"}])
            with self.assertRaises(LLMServiceUnavailable):
                LLMModelClient("dummy", "gemini-3.8-flash").chat_completion([{"content": "second source"}])
            self.assertEqual(post.call_count, 1)
            sleep.assert_not_called()

    @mock.patch("src.llm_client.time.sleep")
    def test_daily_quota_is_not_retried_and_stops_next_process(self, sleep):
        response = mock.Mock(status_code=429, headers={})
        response.json.return_value = {"error": {"message": "Requests per day exhausted", "details": [
            {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
            {"retryDelay": "7200s"},
        ]}}
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            "LLM_QUOTA_STATE_FILE": str(Path(directory) / "quota.json"),
        }), mock.patch("src.llm_client.requests.post", return_value=response) as post:
            with self.assertRaises(LLMQuotaExhausted):
                LLMModelClient("secret", "gemini-3.8-flash").chat_completion([{"content": "hello"}])
            with self.assertRaises(LLMQuotaExhausted):
                LLMModelClient("secret", "gemini-3.8-flash").chat_completion([{"content": "other source"}])
            self.assertEqual(post.call_count, 1)
            self.assertNotIn("secret", post.call_args.args[0])
            sleep.assert_not_called()

    @mock.patch("src.llm_client.time.sleep")
    def test_minute_rate_limit_retries_then_uses_non_thought_text(self, sleep):
        rate = mock.Mock(status_code=429, headers={"Retry-After": "1"})
        rate.json.return_value = {"error": {"message": "Requests per minute exceeded"}}
        okay = mock.Mock(status_code=200)
        okay.json.return_value = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"thought": True, "text": "private thought"}, {"text": "[]"},
        ]}}]}
        with mock.patch.dict(os.environ, {"LLM_QUOTA_STATE_FILE": ""}), mock.patch(
            "src.llm_client.requests.post", side_effect=[rate, okay]
        ) as post:
            result = LLMModelClient("dummy", "gemini-3.8-flash").chat_completion([{"content": "hello"}])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result["choices"][0]["message"]["content"], "[]")
        sleep.assert_called_once()


class TestConservativeFilter(unittest.TestCase):
    def test_clear_unrelated_fields_are_excluded(self):
        for title in ("Assistant Professor of Nursing", "Professor of Accounting", "Assistant Professor of English", "Professor of Neuroscience", "CHOP Division of Hematology", "Assistant Professor in Anglophone Poetry Studies"):
            with self.subTest(title=title):
                self.assertTrue(profile_exclusion_reason(job(1, title)))

    def test_potential_good_fits_and_unknown_titles_are_preserved(self):
        for title in (
            "Assistant Professor", "Faculty Position", "Assistant Professor of Chemical and Biological Engineering",
            "Professor of Biomedical Engineering", "Endowed Chair in Materials Science",
            "Assistant Professor of Physics", "Professor of Chemistry", "Faculty in Artificial Intelligence",
            "Professor of Mechanical Engineering", "Professor of Geophysics",
        ):
            with self.subTest(title=title):
                self.assertFalse(profile_exclusion_reason(job(1, title)))
        interdisciplinary = job(1, "Faculty in Design")
        interdisciplinary["description"] = "Research programmable materials and soft matter."
        self.assertFalse(profile_exclusion_reason(interdisciplinary))
        self.assertTrue(is_summary_candidate(job(1, "Endowed Chair in Materials Science")))

    def test_incidental_biological_microscopy_does_not_override_primary_field(self):
        biological = job(1, "Professor of Biology")
        biological["description"] = "Study proteins with cryo-electron microscopy and imaging."
        self.assertEqual(profile_exclusion_reason(biological), "life_sciences")


if __name__ == "__main__":
    unittest.main()
