"""Retry unresolved queues after provider cooldowns, within shared run budgets."""

import argparse
import importlib
import json
import os
import random
import tempfile
import time
import uuid
from pathlib import Path

from config.settings import LLM_CONFIG
from .feed_utils import eastern_generated_at
from .llm_client import LLMModelClient, current_run_id


# Start with the sources that otherwise follow Interfolio in the initial pass.
# Rotate the first source on each recovery pass so one source cannot always lead.
SOURCES = (
    ("Chronicle", "chronicleJobsPending.json", "jobs", "src.chronicle_cli"),
    ("Inside Higher Ed", "insideHigherEdJobsPending.json", "jobs", "src.insidehighered_cli"),
    ("Interfolio", "interfolioJobsPending.json", "jobs", "src.interfolio_cli"),
    ("JobSpy", "jobSpyJobsPending.json", "jobs", "src.jobcli"),
    ("arXiv", "arXivPending.json", "papers", "src.cli"),
)


def pending_counts(directory):
    counts = {}
    for name, filename, field, _ in SOURCES:
        path = Path(directory) / filename
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        records = payload.get(field, [])
        if not isinstance(records, list):
            raise ValueError(f"Invalid {field} queue: {path}")
        counts[name] = len(records)
    return counts


def summarize_source(source, directory):
    module = importlib.import_module(source[3])
    return module.main(["--output-dir", str(directory), "summarize"])


def retry_pending(directory):
    directory = Path(directory)
    client = LLMModelClient(LLM_CONFIG.get("api_key", ""))
    rounds = []
    stop_reason = ""
    delays = LLM_CONFIG.get("service_retry_delays", [300, 900, 1800])
    for index, minimum_delay in enumerate(delays):
        before = pending_counts(directory)
        if not any(before.values()):
            break
        provider_delay = client.next_retry_delay()
        if provider_delay is None:
            stop_reason = "All models are blocked by daily quota or local request/time/retry budgets."
            break
        delay = max(minimum_delay, provider_delay) + random.uniform(0, float(LLM_CONFIG.get("retry_jitter_seconds", 30)))
        if delay >= client.remaining_run_seconds():
            stop_reason = "The next cooldown exceeds the remaining LLM time budget."
            break
        print(f"Recovery pass {index + 1}: {sum(before.values())} unfinished records; cooling down for {delay:.0f}s.", flush=True)
        # Short sleeps keep cancellation responsive during a long cooldown.
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            time.sleep(min(30, max(0, deadline - time.monotonic())))
        outcomes = []
        order = SOURCES[index % len(SOURCES):] + SOURCES[:index % len(SOURCES)]
        for source in order:
            name = source[0]
            if not pending_counts(directory)[name]:
                continue
            if client.remaining_run_seconds() <= 0:
                stop_reason = "LLM elapsed-time budget exhausted."
                break
            try:
                code = summarize_source(source, directory)
            except (Exception, SystemExit) as exc:
                # Each controller owns its durable queue; isolate its failure so
                # the other sources still get a chance during this pass.
                print(f"::warning::{name} recovery failed; its queue is retained: {exc}")
                code = 1
            outcomes.append({"source": name, "exitCode": code})
        rounds.append({"pass": index + 1, "waitSeconds": round(delay, 1), "before": before, "after": pending_counts(directory), "outcomes": outcomes})

    remaining = pending_counts(directory)
    complete = not any(remaining.values())
    if not complete and not stop_reason:
        stop_reason = "Delayed recovery passes exhausted; remaining records will be retried next run."
    report = {
        "generatedAt": eastern_generated_at(), "runId": current_run_id(),
        "complete": complete, "remainingQueued": remaining,
        "stopReason": stop_reason, "rounds": rounds,
        "runtime": client.runtime_snapshot(),
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "summaryRetryReport.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    if not complete:
        print(f"::warning::{sum(remaining.values())} records remain unfinished. Details: {path}")
    return 0 if complete else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="data")
    args = parser.parse_args(argv)
    # CLI processes share one marker in Actions. Provide the same isolation for
    # a local invocation without retaining credentials or stale request budgets.
    if os.getenv("LLM_QUOTA_STATE_FILE"):
        return retry_pending(args.output_dir)
    with tempfile.TemporaryDirectory(prefix="arxivsummary-retry-") as directory:
        previous_id = os.environ.get("LLM_RUN_ID")
        os.environ["LLM_RUN_ID"] = uuid.uuid4().hex
        os.environ["LLM_QUOTA_STATE_FILE"] = str(Path(directory) / "llm-state.json")
        try:
            return retry_pending(args.output_dir)
        finally:
            os.environ.pop("LLM_QUOTA_STATE_FILE", None)
            if previous_id is None:
                os.environ.pop("LLM_RUN_ID", None)
            else:
                os.environ["LLM_RUN_ID"] = previous_id


if __name__ == "__main__":
    raise SystemExit(main())
