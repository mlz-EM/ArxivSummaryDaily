"""Behavior tests for the workflow's bounded git-push retry helper."""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts/push_with_retry.sh"


class TestPushWithRetry(unittest.TestCase):
    def _run_with_fake_commands(self, git_script: str):
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory)
            counter = bin_dir / "attempts"
            git = bin_dir / "git"
            git.write_text(git_script, encoding="utf-8")
            git.chmod(git.stat().st_mode | stat.S_IXUSR)
            sleep = bin_dir / "sleep"
            sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            sleep.chmod(sleep.stat().st_mode | stat.S_IXUSR)
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "ATTEMPT_FILE": str(counter),
                "PUSH_MAX_ATTEMPTS": "3",
                "PUSH_RETRY_DELAY_SECONDS": "1",
            }
            result = subprocess.run(
                ["bash", str(SCRIPT), "upstream", "main"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            attempts = int(counter.read_text(encoding="utf-8"))
            return result, attempts

    def test_retries_transient_failure_then_succeeds(self):
        result, attempts = self._run_with_fake_commands(
            """#!/usr/bin/env bash
attempts=0
[[ -f "$ATTEMPT_FILE" ]] && attempts=$(cat "$ATTEMPT_FILE")
attempts=$((attempts + 1))
echo "$attempts" > "$ATTEMPT_FILE"
[[ "$1 $2 $3" == "push upstream main" ]] || exit 9
[[ "$attempts" -ge 2 ]]
"""
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(attempts, 2)
        self.assertIn("retrying in 1s", result.stdout)

    def test_stops_after_configured_attempt_limit(self):
        result, attempts = self._run_with_fake_commands(
            """#!/usr/bin/env bash
attempts=0
[[ -f "$ATTEMPT_FILE" ]] && attempts=$(cat "$ATTEMPT_FILE")
attempts=$((attempts + 1))
echo "$attempts" > "$ATTEMPT_FILE"
exit 1
"""
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(attempts, 3)
        self.assertIn("failed after 3 attempts", result.stdout)


if __name__ == "__main__":
    unittest.main()
