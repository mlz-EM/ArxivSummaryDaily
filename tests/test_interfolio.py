"""Unit tests for resumable Interfolio job discovery."""

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from src.interfolio_cli import main as interfolio_main
from src.interfolio_client import (
    FetchResult,
    InterfolioClient,
    InterfolioScanner,
    InterfolioStore,
    build_tenant_directory,
    canonical_school_name,
    html_to_text,
    is_summary_candidate,
    load_processed_ids,
    normalize_position,
    raw_job_to_summary_input,
    save_tenant_directory,
    save_processed_ids,
)
from src.job_summarizer import JobSummarizer


def position(position_id, *, tenant_id=10128, closed=False, posted="Aug 08, 2026"):
    return {
        "position_id": position_id,
        "tenant_id": tenant_id,
        "landing_page_url": f"https://apply.interfolio.com/{position_id}",
        "position_name": f"Faculty Position {position_id}",
        "institution": "Example University: School: Department",
        "institution_condensed": "Example University: Department",
        "location": "Massachusetts, United States",
        "start_date": posted,
        "end_date": "Sep 15, 2026",
        "is_open": not closed,
        "is_closed": closed,
        "active_status": "Closed" if closed else "Open",
        "landing_page_description": "<p>Studies <strong>advanced materials</strong>.</p>",
        "qualifications": "<p>Ph.D. required.</p>",
        "application_instructions": "<p>Upload a CV.<br>Upload references.</p>",
        "salary": "",
    }


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class FakeDiscoveryClient:
    def __init__(self, positions=None):
        self.positions = positions or {}
        self.position_calls = []

    def fetch_position(self, position_id):
        self.position_calls.append(position_id)
        payload = self.positions.get(position_id)
        if payload:
            return FetchResult("position", 200, payload)
        return FetchResult("not_found", 404)


class TestInterfolioClient(unittest.TestCase):
    def test_html_and_position_normalization(self):
        self.assertEqual(html_to_text("<p>Hello&nbsp;<b>world</b></p><p>Next</p>"), "Hello world\nNext")

        record = normalize_position(position(190677), observed_at="2026-08-14T12:00:00Z")

        self.assertEqual(record["id"], 190677)
        self.assertEqual(record["postedDate"], "2026-08-08")
        self.assertEqual(record["deadline"], "2026-09-15")
        self.assertEqual(record["description"], "Studies advanced materials.")
        self.assertEqual(record["firstSeenAt"], record["lastSeenAt"])
        self.assertRegex(record["contentHash"], r"^[0-9a-f]{64}$")
        for removed_key in (
            "source",
            "url",
            "institutionFull",
            "qualifications",
            "applicationInstructions",
            "salary",
            "jobRequestNumber",
            "externalUrl",
            "positionStatus",
            "isPrivate",
            "isOnlineApplication",
            "isByCommittee",
            "equalOpportunityStatement",
            "logo",
        ):
            self.assertNotIn(removed_key, record)

    def test_tenant_directory_is_unique_canonical_and_idempotent(self):
        jobs = [
            {"tenantId": 10128, "institution": "Brown University: Engineering"},
            {"tenantId": 10128, "institution": "Brown University: Physics"},
            {"tenantId": 10338, "institution": "Carnegie Mellon University: MSE"},
            {"tenantId": None, "institution": "Unknown University"},
        ]
        self.assertEqual(canonical_school_name(jobs[0]["institution"]), "Brown University")
        self.assertEqual(
            build_tenant_directory(jobs),
            {
                "10128": "Brown University",
                "10338": "Carnegie Mellon University",
            },
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "tenants.json"
            self.assertTrue(save_tenant_directory(output, jobs))
            self.assertFalse(save_tenant_directory(output, jobs))
            self.assertTrue(
                save_tenant_directory(
                    output,
                    [{"tenantId": 20000, "institution": "New University: Science"}],
                )
            )
            with open(output, "r", encoding="utf-8") as handle:
                self.assertEqual(
                    json.load(handle)["tenants"],
                    {
                        **build_tenant_directory(jobs),
                        "20000": "New University",
                    },
                )

    def test_position_response_classification(self):
        session = FakeSession(
            [
                FakeResponse(200, position(190677)),
                FakeResponse(200, {}),
                FakeResponse(404, {"errors": []}),
                FakeResponse(403, {}),
                FakeResponse(400, {}),
            ]
        )
        client = InterfolioClient(session=session, request_delay=0, sleep=lambda _: None)

        self.assertEqual(client.fetch_position(190677).kind, "position")
        self.assertEqual(client.fetch_position(190678).kind, "empty")
        self.assertEqual(client.fetch_position(192000).kind, "not_found")
        self.assertEqual(client.fetch_position(192001).kind, "private")
        self.assertEqual(client.fetch_position(192002).kind, "unavailable")

    def test_rate_limit_response_is_retried(self):
        waits = []
        session = FakeSession(
            [
                FakeResponse(429, {}, {"Retry-After": "0"}),
                FakeResponse(200, position(190677)),
            ]
        )
        client = InterfolioClient(
            session=session,
            request_delay=0,
            retry_count=2,
            sleep=waits.append,
        )

        result = client.fetch_position(190677)

        self.assertEqual(result.kind, "position")
        self.assertEqual(len(session.calls), 2)

class TestInterfolioPersistence(unittest.TestCase):
    def test_existing_raw_job_is_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InterfolioStore(Path(directory) / "raw.json", Path(directory) / "state.json")
            self.assertTrue(store.add_position(position(190677, closed=False)))
            store.save()
            store = InterfolioStore(Path(directory) / "raw.json", Path(directory) / "state.json")
            self.assertFalse(store.add_position(position(190677, closed=True)))

            stored = store.jobs[0]
            self.assertFalse(stored["isClosed"])
            self.assertEqual(stored["activeStatus"], "Open")

    def test_bootstrap_starts_at_180000_and_extends_lookahead(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InterfolioStore(Path(directory) / "raw.json", Path(directory) / "state.json")
            client = FakeDiscoveryClient(
                positions={
                    180000: position(180000),
                    180002: position(180002),
                }
            )
            scanner = InterfolioScanner(
                client,
                store,
                start_id=180000,
                lookahead=2,
                checkpoint_every=2,
            )

            report = scanner.bootstrap()

            self.assertEqual(client.position_calls, [180000, 180001, 180002, 180003, 180004])
            self.assertEqual(report["newJobs"], 2)
            self.assertEqual(store.state["highestDiscoveredId"], 180002)
            self.assertEqual(store.state["scanUpperBound"], 180004)
            self.assertTrue(store.state["bootstrapComplete"])
            self.assertEqual(set(store.state["missing"]), {"180001"})
            self.assertEqual(store.state["missing"]["180001"]["nextLargerId"], 180002)

            reloaded = InterfolioStore(Path(directory) / "raw.json", Path(directory) / "state.json")
            self.assertEqual([job["id"] for job in reloaded.jobs], [180000, 180002])

    def test_daily_frontier_discovers_jobs_without_refetching_known_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InterfolioStore(Path(directory) / "raw.json", Path(directory) / "state.json")
            store.add_position(position(180000))
            store.state["bootstrapComplete"] = True
            store.save()

            client = FakeDiscoveryClient(
                positions={
                    180001: position(180001),
                    180003: position(180003),
                },
            )
            scanner = InterfolioScanner(client, store, start_id=180000, lookahead=2)
            report = scanner.daily()

            self.assertNotIn(180000, client.position_calls)
            self.assertIn(180001, client.position_calls)
            self.assertIn(180003, client.position_calls)
            self.assertEqual(report["newJobs"], 2)
            self.assertEqual({job["id"] for job in store.jobs}, {180000, 180001, 180003})

    def test_daily_requires_completed_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InterfolioStore(Path(directory) / "raw.json", Path(directory) / "state.json")
            scanner = InterfolioScanner(FakeDiscoveryClient(), store, start_id=180000, lookahead=1)
            with self.assertRaisesRegex(RuntimeError, "bootstrap is incomplete"):
                scanner.daily()

    def test_daily_retries_recent_missing_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InterfolioStore(Path(directory) / "raw.json", Path(directory) / "state.json")
            posted = datetime.now().strftime("%b %d, %Y")
            store.add_position(position(180000, posted=posted))
            store.add_position(position(180002, posted=posted))
            store.record_missing(
                180001,
                "empty",
                next_larger_id=180002,
                next_larger_posted_date=posted,
            )
            store.state["bootstrapComplete"] = True
            store.save()

            client = FakeDiscoveryClient(positions={180001: position(180001)})
            scanner = InterfolioScanner(
                client,
                store,
                start_id=180000,
                lookahead=1,
                checkpoint_every=2,
            )
            scanner.daily()

            self.assertIn(180001, client.position_calls)
            self.assertIn(180001, store.known_ids)
            self.assertNotIn("180001", store.state["missing"])

    def test_old_missing_id_is_completed_instead_of_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InterfolioStore(Path(directory) / "raw.json", Path(directory) / "state.json")
            store.record_missing(
                180001,
                "not_found",
                next_larger_id=180002,
                next_larger_posted_date="2026-08-01",
            )

            retryable = store.retryable_missing_ids(7, today=date(2026, 8, 15))

            self.assertEqual(retryable, [])
            self.assertNotIn("180001", store.state["missing"])
            self.assertIn(180001, store.completed_missing_ids)

    def test_future_missing_id_is_deferred(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InterfolioStore(Path(directory) / "raw.json", Path(directory) / "state.json")
            store.record_missing(
                180001,
                "not_found",
                next_larger_id=180002,
                next_larger_posted_date="2026-08-17",
            )

            retryable = store.retryable_missing_ids(7, today=date(2026, 8, 15))

            self.assertEqual(retryable, [])
            self.assertEqual(store.state["missing"]["180001"]["status"], "deferred")

    def test_frontier_does_not_persist_ids_above_high_water_mark(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InterfolioStore(Path(directory) / "raw.json", Path(directory) / "state.json")
            store.add_position(position(180000))
            store.state["bootstrapComplete"] = True
            client = FakeDiscoveryClient()

            InterfolioScanner(client, store, start_id=180000, lookahead=2).daily()

            self.assertEqual(client.position_calls, [180001, 180002])
            self.assertEqual(store.state["missing"], {})
            self.assertEqual(store.state["highestDiscoveredId"], 180000)

    def test_frontier_gap_uses_nearest_larger_discovered_position(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InterfolioStore(Path(directory) / "raw.json", Path(directory) / "state.json")
            store.add_position(position(180000))
            store.state["bootstrapComplete"] = True
            client = FakeDiscoveryClient(
                positions={180003: position(180003, posted="Aug 15, 2026")}
            )

            InterfolioScanner(
                client,
                store,
                start_id=180000,
                lookahead=3,
                checkpoint_every=2,
            ).daily()

            for position_id in (180001, 180002):
                observation = store.state["missing"][str(position_id)]
                self.assertEqual(observation["nextLargerId"], 180003)
                self.assertEqual(observation["nextLargerPostedDate"], "2026-08-15")
            self.assertNotIn("180004", store.state["missing"])
            self.assertNotIn("180005", store.state["missing"])
            self.assertNotIn("180006", store.state["missing"])

    def test_summary_state_and_input_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "summary-state.json"
            save_processed_ids(state_path, [190677, 190678, 190677])
            self.assertEqual(load_processed_ids(state_path), {190677, 190678})

        normalized = raw_job_to_summary_input(normalize_position(position(190677)))
        self.assertEqual(normalized["title"], "Faculty Position 190677")
        self.assertEqual(normalized["company"], "Example University: Department")
        self.assertEqual(normalized["description"], "Studies advanced materials.")
        self.assertEqual(normalized["job_url"], "https://apply.interfolio.com/190677")

        faculty = normalize_position(position(190677))
        self.assertTrue(is_summary_candidate(faculty))
        faculty["isOpen"] = False
        self.assertTrue(is_summary_candidate(faculty))
        faculty["title"] = "Postdoctoral Research Associate"
        self.assertFalse(is_summary_candidate(faculty))
        faculty["title"] = "Research Scientist"
        self.assertFalse(is_summary_candidate(faculty))
        faculty["title"] = "Open-Rank Faculty Position"
        self.assertTrue(is_summary_candidate(faculty))
        faculty["title"] = "Tenure Track Position in Materials Science"
        self.assertTrue(is_summary_candidate(faculty))
        faculty["title"] = "Tenure-Track Position in Materials Science"
        self.assertTrue(is_summary_candidate(faculty))
        faculty["title"] = "Non-Tenure-Track Position in Materials Science"
        self.assertFalse(is_summary_candidate(faculty))
        faculty["isClosed"] = True
        self.assertFalse(is_summary_candidate(faculty))
        faculty["isClosed"] = False
        faculty["activeStatus"] = "Closed"
        self.assertFalse(is_summary_candidate(faculty))


class TestInterfolioSummary(unittest.TestCase):
    def test_summary_sends_only_candidates_and_marks_all_records_processed(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            open_job = normalize_position(position(190677, closed=False))
            closed_job = normalize_position(position(190678, closed=True))
            InterfolioStore._atomic_write(
                output_dir / "interfolioJobsPending.json",
                {"schemaVersion": 1, "jobs": [open_job, closed_job]},
            )

            fake_summarizer = mock.Mock()
            fake_summarizer.client.model = "test-model"

            def write_summary(jobs, output_file):
                fake_summarizer.received_jobs = jobs
                InterfolioStore._atomic_write(Path(output_file), {"header": {}, "items": []})
                return True

            fake_summarizer.summarize_jobs.side_effect = write_summary
            with mock.patch.dict(
                "src.interfolio_cli.settings.LLM_CONFIG",
                {"api_key": "test-key", "model": "test-model"},
            ), mock.patch("src.interfolio_cli.JobSummarizer", return_value=fake_summarizer):
                result = interfolio_main(["--output-dir", directory, "summarize"])

            self.assertEqual(result, 0)
            self.assertEqual(len(fake_summarizer.received_jobs), 1)
            self.assertEqual(fake_summarizer.received_jobs[0]["source_id"], "190677")
            self.assertEqual(
                load_processed_ids(output_dir / "interfolioSummaryState.json"),
                {190677, 190678},
            )
            self.assertFalse((output_dir / "interfolioJobsPending.json").exists())

    def test_pre_cutoff_jobs_are_marked_processed_without_an_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            old_job = normalize_position(position(190677, posted="May 31, 2026"))
            InterfolioStore._atomic_write(
                output_dir / "interfolioJobsPending.json",
                {"schemaVersion": 1, "jobs": [old_job]},
            )

            fake_summarizer = mock.Mock()
            fake_summarizer.client.model = "test-model"

            def write_summary(jobs, output_file):
                fake_summarizer.received_jobs = jobs
                InterfolioStore._atomic_write(Path(output_file), {"header": {}, "items": []})
                return True

            fake_summarizer.summarize_jobs.side_effect = write_summary
            with mock.patch.dict(
                "src.interfolio_cli.settings.LLM_CONFIG",
                {"api_key": "YOUR_API_HERE", "model": "test-model"},
            ), mock.patch("src.interfolio_cli.JobSummarizer", return_value=fake_summarizer):
                result = interfolio_main(["--output-dir", directory, "summarize"])

            self.assertEqual(result, 0)
            self.assertEqual(fake_summarizer.received_jobs, [])
            self.assertEqual(
                load_processed_ids(output_dir / "interfolioSummaryState.json"),
                {190677},
            )
            self.assertFalse((output_dir / "interfolioJobsPending.json").exists())

    def test_failed_ai_batch_does_not_create_rejected_placeholder(self):
        job = {
            "title": "Retry Me",
            "company": "Example University",
            "location": "Boston, MA",
            "date_posted": "2026-08-08",
            "description": "Materials role",
            "job_url": "https://apply.interfolio.com/190677",
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "summary.json"
            summarizer = JobSummarizer("dummy", "test-model")
            with mock.patch.object(summarizer, "_generate_batch_summary", return_value=([], False)):
                self.assertFalse(summarizer.summarize_jobs([job], str(output)))
            with open(output, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["items"], [])

if __name__ == "__main__":
    unittest.main()
