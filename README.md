[Daily Arxiv Paper Summary](https://mlz-em.github.io/personal-site/arxiv)

Forked from [https://github.com/dong-zehao/ArxivSummaryDaily](https://github.com/dong-zehao/ArxivSummaryDaily) and pipelined to [personal site](https://mlz-em.github.io/personal-site/arxiv)

## Interfolio job discovery

The Interfolio pipeline stores unsummarized records temporarily in
`data/interfolioJobsPending.json`, crawl checkpoints and missing-ID observations in
`data/interfolioScanState.json`, AI results in
`data/interfolioJobsSummary.json`, and summarized-ID state in
`data/interfolioSummaryState.json`. The unique tenant-ID-to-school mapping is
stored in `data/interfolioTenantDirectory.json`.

Pending records intentionally keep only the numeric IDs needed to reconstruct
`https://apply.interfolio.com/{id}`, compact institution/location/date/status
metadata, the main description, first/last-seen bookkeeping, and a content
hash. Boilerplate qualifications, application instructions, salary-through-logo
fields, duplicate institution text, and explicit source URLs are omitted.

Only records awaiting their first summary remain in the temporary queue. Records explicitly marked
closed, or whose titles contain adjunct, chair, dean, fellowship, instructor,
lecturer, postdoc, temporary, or visiting terms, are skipped. Remaining titles
must contain `professor`, `faculty`, `open rank`/`open-rank`, or
`tenure track`/`tenure-track`. The prompt performs tenure-track,
field-relevance, and research-fit filtering. Interfolio, regular job, and arXiv
summaries use at most 20 input records per API request.
Titles explicitly saying `non-tenure-track` do not qualify through the
tenure-track term.

The initial feed includes only positions posted on or after June 1, 2026.
Earlier discovered positions are added to `interfolioSummaryState.json` without
being summarized, so neither local nor scheduled runs can submit them later.

Known jobs are represented by the scan high-water mark and summary state. Daily
discovery does not refresh open/closed status or descriptions. Empty and missing IDs are kept only
in the scan-state file and retried for 14 days when they are within 100 IDs of a
recent posting.

Live access is opt-in because it must be used only when authorized under the
source site's current terms. After copying `config/settings.example.py` to
`config/settings.py`, run the one-time historical bootstrap locally:

```bash
INTERFOLIO_ENABLE_LIVE=true interfoliojobs --output-dir data scan \
  --mode bootstrap --start-id 180000
```

The scan uses eight bounded workers by default, spaces request starts, checkpoints
every 50 IDs, and resumes from the saved state. It continues until 500 IDs
beyond the highest public position found. Generate the initial summaries
afterward:

```bash
LLM_API_KEY=... interfoliojobs --output-dir data summarize
```

Commit the Interfolio state, tenant directory, and summary after the bootstrap. Scheduled Actions will
then retry recent holes, scan the numeric frontier, and summarize only IDs not recorded in
`interfolioSummaryState.json`. The independent `interfolioJobsSummary.json`
feed is copied to the personal-site data directory and is never merged into
`jobsDaily.json`. The pending file is removed whenever the queue becomes empty.

After each discovery run, the workflow merges newly observed schools into the
tenant directory. School labels use the text before Interfolio's department
separator (`School: Department`), and the file is rewritten only when a tenant
or canonical school name changes.

## Chronicle job discovery

Chronicle discovery reads the site's public current-job sitemap instead of
probing numeric IDs. The one-time bootstrap and daily scan use 16 bounded
workers by default, checkpoint every 200 completed pages, and share retry and
rate-limit backoff. Run the initial scrape locally:

```bash
CHRONICLE_ENABLE_LIVE=true chroniclejobs --output-dir data scan --mode bootstrap
```

`data/chronicleScanState.json` stores compact seen IDs and retry metadata.
`data/chronicleJobsPending.json` temporarily contains only the title, institution,
location, posting date, and description needed to make the LLM decision.
Titles must pass the same shared faculty-like inclusion and exclusion rules;
listings posted before June 1, 2026 are marked seen but never stored. After a successful summary
run, processed records are removed and an empty pending file is deleted:

```bash
LLM_API_KEY=... chroniclejobs --output-dir data summarize
```

The durable AI feed is `data/chronicleJobsSummary.json`. It remains separate
from `jobsDaily.json` and `interfolioJobsSummary.json`, and the scheduled
workflow copies it to the personal-site data directory. Daily scans download
the sitemap, skip every seen ID, fetch only newly listed jobs, and then send
the queued jobs to the LLM in batches of at most 20.

Chronicle's crawler implementation is shared with Inside Higher Ed in
`src/madgex_client.py`; source-specific URLs, date policy, filenames, and feed
headers remain in their respective wrappers.

## Inside Higher Ed job discovery

Inside Higher Ed publishes a public Madgex job sitemap and exposes the posting
date in each job page's structured data. Its one-time bootstrap inspects the
current sitemap but queues only jobs posted on or after June 1, 2026:

```bash
INSIDE_HIGHER_ED_ENABLE_LIVE=true insidehigheredjobs --output-dir data scan \
  --mode bootstrap --minimum-posted-date 2026-06-01
LLM_API_KEY=... insidehigheredjobs --output-dir data summarize
```

Compact seen-ID state is stored in `data/insideHigherEdScanState.json`, the
temporary LLM queue in `data/insideHigherEdJobsPending.json`, and the durable
independent site feed in `data/insideHigherEdJobsSummary.json`. The read-only
job detail pages do not require reCAPTCHA; the reCAPTCHA markup is attached to
the external application/redirect form, which this workflow never invokes.
The pending file is removed after every successfully completed queue.

## HigherEdJobs access

HigherEdJobs currently returns an Imperva/Incapsula JavaScript verification
shell for its job pages, robots file, and sitemap candidates. No crawler is
implemented because the normal HTTP path does not expose a supported discovery
document, and this project does not bypass human-verification controls.
