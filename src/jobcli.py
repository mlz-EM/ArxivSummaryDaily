"""Command-line workflow for isolated JobSpy scans and summarization."""

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List

from jobspy import scrape_jobs

from config.settings import JOB_CONFIG, LLM_CONFIG, OUTPUT_DIR
from .job_summarizer import JobSummarizer


PENDING_FILENAME = "jobSpyJobsPending.json"
PENDING_SCHEMA_VERSION = 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JobSpy job summary generator")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="output directory")
    subparsers = parser.add_subparsers(dest="command")

    scan_parser = subparsers.add_parser("scan", help="scrape one isolated provider group")
    scan_parser.add_argument(
        "--source",
        choices=("google", "other"),
        required=True,
        help="Google alone, or every configured non-Google provider",
    )
    subparsers.add_parser("summarize", help="summarize all queued JobSpy records")
    return parser


def _pending_path(output_dir: str) -> Path:
    return Path(output_dir) / PENDING_FILENAME


def _load_pending_jobs(path: Path) -> List[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return []

    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise ValueError(f"JobSpy pending queue must contain a jobs array: {path}")
    return [job for job in jobs if isinstance(job, dict)]


def _job_key(job: Dict[str, Any]) -> str:
    for field in ("job_url", "id"):
        value = job.get(field)
        if value is not None and str(value).strip():
            return f"{field}:{value}"
    return json.dumps(job, sort_keys=True, ensure_ascii=False)


def _write_pending_jobs(path: Path, jobs: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    payload = {"schemaVersion": PENDING_SCHEMA_VERSION, "jobs": jobs}
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary_path, path)


def _merge_pending_jobs(
    existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    merged = {_job_key(job): job for job in existing}
    for job in incoming:
        merged[_job_key(job)] = job
    return list(merged.values())


def _config_for_source(source: str) -> Dict[str, Any]:
    config = dict(JOB_CONFIG)
    configured_sites = config.get("site_name", [])
    if isinstance(configured_sites, str):
        configured_sites = [configured_sites]

    if source == "google":
        selected_sites = [site for site in configured_sites if str(site).lower() == "google"]
    else:
        selected_sites = [site for site in configured_sites if str(site).lower() != "google"]
        config.pop("google_search_term", None)

    if not selected_sites:
        raise ValueError(f"No JobSpy providers are configured for source group: {source}")
    config["site_name"] = selected_sites
    return config


def _records_from_dataframe(jobs: Any) -> List[Dict[str, Any]]:
    if jobs is None or jobs.empty:
        return []
    ordered = jobs.sort_values(by="date_posted", ascending=False)
    return json.loads(ordered.to_json(orient="records", date_format="iso"))


def scan(output_dir: str, source: str) -> int:
    """Scrape one provider group and append its records to the shared queue."""
    try:
        jobs = scrape_jobs(**_config_for_source(source))
        incoming = _records_from_dataframe(jobs)
        path = _pending_path(output_dir)
        pending = _merge_pending_jobs(_load_pending_jobs(path), incoming)
        _write_pending_jobs(path, pending)
    except Exception as exc:
        print(f"::warning::JobSpy {source} scan failed; its existing queue was retained: {exc}")
        return 0

    print(
        f"JobSpy {source} scan fetched {len(incoming)} jobs; "
        f"{len(pending)} total records are queued."
    )
    return 0


def summarize(output_dir: str) -> int:
    """Summarize the shared Google and non-Google pending queue."""
    path = _pending_path(output_dir)
    if not path.exists():
        print("No JobSpy pending queue was produced; leaving the existing feed unchanged.")
        return 0

    try:
        jobs = _load_pending_jobs(path)
        output_file = os.path.join(output_dir, "jobsDaily.json")
        job_summarizer = JobSummarizer(LLM_CONFIG["api_key"], LLM_CONFIG.get("model"))
        success = job_summarizer.summarize_jobs(jobs, output_file)
    except Exception as exc:
        print(f"Error generating JobSpy summary: {exc}")
        return 1

    if not success:
        print("JobSpy summary incomplete; pending records were retained for retry.")
        return 1

    path.unlink()
    print(f"JSON feed generated and saved to: {output_file}")
    print("JobSpy summary generated successfully; pending queue cleared.")
    return 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    command = getattr(args, "command", None)
    if command == "scan":
        return scan(args.output_dir, args.source)
    if command == "summarize":
        return summarize(args.output_dir)

    # Preserve the original one-command experience while isolating Google from
    # the other providers internally.
    scan(args.output_dir, "other")
    scan(args.output_dir, "google")
    return summarize(args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
