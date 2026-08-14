"""Inside Higher Ed configuration for the shared Madgex crawler."""

from .madgex_client import (
    DEFAULT_WORKERS,
    FetchResult,
    MAX_DESCRIPTION_CHARS,
    MadgexClient,
    MadgexScanner,
    MadgexStore,
    is_title_candidate,
    parse_job_html,
    parse_sitemap,
)
from .madgex_client import raw_job_to_summary_input as _raw_job_to_summary_input


SITEMAP_URL = "https://careers.insidehighered.com/sitemap2-1.xml"
JOB_URL = "https://careers.insidehighered.com/job/{job_id}"
DEFAULT_MINIMUM_POSTED_DATE = "2026-06-01"


class InsideHigherEdClient(MadgexClient):
    def __init__(self, **kwargs):
        super().__init__(sitemap_url=SITEMAP_URL, source_name="Inside Higher Ed", **kwargs)


class InsideHigherEdStore(MadgexStore):
    pass


class InsideHigherEdScanner(MadgexScanner):
    def __init__(self, client, store, *, minimum_posted_date=DEFAULT_MINIMUM_POSTED_DATE, **kwargs):
        super().__init__(
            client,
            store,
            job_url_template=JOB_URL,
            source_name="Inside Higher Ed",
            minimum_posted_date=minimum_posted_date,
            **kwargs,
        )


def raw_job_to_summary_input(job, max_description_chars=MAX_DESCRIPTION_CHARS):
    return _raw_job_to_summary_input(
        job,
        max_description_chars=max_description_chars,
        job_url_template=JOB_URL,
    )
