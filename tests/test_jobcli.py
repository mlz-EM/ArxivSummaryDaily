"""Tests for the JobSpy command-line boundary."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import jobcli


class TestJobCli(unittest.TestCase):
    def test_provider_failure_exits_successfully_without_touching_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            feed = output_dir / "jobsDaily.json"
            original = '{"header": {"generatedAt": "existing"}, "items": []}\n'
            feed.write_text(original, encoding="utf-8")
            args = SimpleNamespace(output_dir=directory)

            with mock.patch(
                "argparse.ArgumentParser.parse_args", return_value=args
            ), mock.patch.object(
                jobcli,
                "scrape_jobs",
                side_effect=RuntimeError("provider returned HTTP 429"),
            ), mock.patch.object(jobcli, "JobSummarizer") as summarizer_cls, mock.patch(
                "builtins.print"
            ) as print_mock:
                result = jobcli.main()

            self.assertEqual(result, 0)
            self.assertEqual(feed.read_text(encoding="utf-8"), original)
            summarizer_cls.assert_not_called()
            self.assertTrue(
                any(
                    "Job provider fetch failed" in str(call)
                    and "HTTP 429" in str(call)
                    for call in print_mock.call_args_list
                )
            )


if __name__ == "__main__":
    unittest.main()
