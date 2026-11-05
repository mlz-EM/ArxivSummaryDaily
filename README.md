[Daily Arxiv Paper Summary](https://mlz-em.github.io/personal-site/arxiv)

Forked from [https://github.com/dong-zehao/ArxivSummaryDaily](https://github.com/dong-zehao/ArxivSummaryDaily) and pipelined to [personal site](https://mlz-em.github.io/personal-site/arxiv)

Daily processing starts with `gemini-3.8-flash`, then falls back to
`gemini-3.6-flash` and `gemini-3.5-flash-lite`. See [pricing](https://ai.google.dev/gemini-api/docs/pricing)
and [model limits](https://aistudio.google.com/docs/models/gemini-3.8-flash).
Gemini 3.7 is an alias routed to 3.8 and does not provide independent capacity.
Override the primary with `LLM_MODEL` and the comma-separated fallback list with
`LLM_FALLBACK_MODELS`; an empty fallback list disables rotation.

Job batches grow according to input and output budgets, with a ceiling of 500
jobs and a default output budget that permits about 240 short records. Long
descriptions reduce the batch size. Context/output-limit errors split a batch.
The default input budget is 100,000 estimated tokens, so short postings can still
share large batches without requiring half-million-token requests.
`--batch-size` can lower the ceiling for an individual source.

Each model response explicitly lists accepted and rejected URLs. Both decisions
are saved after every batch; omitted or failed jobs stay pending. Daily quota
exhaustion defers that model and tries the configured fallback. Other
transient errors have bounded retries per model. `*SummaryReport.json` files (and
`jobsDailyReport.json`) list unresolved URLs and reasons; the Actions job summary
also shows the remaining queue counts.

HTTP 5xx and network failures cool down the affected model and immediately try
the next configured model. Cooldowns expire within the same Actions run. After
the initial source steps, `python -m src.retry_pending` retries only unresolved
queues after about 5, 15, then 30 minutes, with up to 30 seconds of random delay.
Recovery passes rotate source order. Completed decisions and their `isNew` flags
are preserved across passes, and the source reports accumulate the run totals.

All sources share a 90-minute LLM budget, at most 18 API attempts per model, and
at most four service failures per model. These limits include failed attempts,
which may consume request quota even when no tokens are billed. Daily quota
exhaustion stops that model until the next Pacific-midnight reset; a long
per-minute cooldown is not treated as daily exhaustion. Actual provider limits
still apply and may be lower than the local attempt budget.

`data/summaryRetryReport.json` records recovery passes, remaining queues, request
counts and the stop reason. Any pending jobs or papers make the final Actions
status fail, after the database and partial feeds have been committed and pushed.
The daily schedule is 4:17 AM in `America/New_York` (including daylight saving),
after the daily quota reset and away from the start of the hour. GitHub's
scheduler can still start late, and no scheduled time guarantees Gemini capacity.

The local profile filter excludes only explicit unrelated title fields and
appointment types. Unknown titles and potential engineering/physical-science
fits continue to the LLM. Materials evidence preserves interdisciplinary
searches, including chemical/biological engineering, biomaterials and soft matter.
