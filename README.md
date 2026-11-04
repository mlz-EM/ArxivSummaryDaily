[Daily Arxiv Paper Summary](https://mlz-em.github.io/personal-site/arxiv)

Forked from [https://github.com/dong-zehao/ArxivSummaryDaily](https://github.com/dong-zehao/ArxivSummaryDaily) and pipelined to [personal site](https://mlz-em.github.io/personal-site/arxiv)

Daily processing uses `gemini-3.6-flash` with `gemini-3.5-flash-lite` as a
fallback. Both are free-tier eligible as of November 4, 2026. See [pricing](https://ai.google.dev/gemini-api/docs/pricing)
and [model limits](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash).
Gemini 3.7 is an alias routed to 3.8 and does not provide independent capacity.
Override the primary with `LLM_MODEL` and the comma-separated fallback list with
`LLM_FALLBACK_MODELS`; an empty fallback list disables rotation.

Job batches grow according to input and output budgets, with a ceiling of 500
jobs and a default output budget that permits about 240 short records. Long
descriptions reduce the batch size. Context/output-limit errors split a batch.
`--batch-size` can lower the ceiling for an individual source.

Each model response explicitly lists accepted and rejected URLs. Both decisions
are saved after every batch; omitted or failed jobs stay pending. Daily quota
exhaustion defers that model and tries the configured fallback. Other
transient errors have bounded retries per model. `*SummaryReport.json` files (and
`jobsDailyReport.json`) list unresolved URLs and reasons; the Actions job summary
also shows the remaining queue counts.

Repeated HTTP 5xx failures also defer the affected model, preserving its cooldown
separately from other models. In Actions, later sources skip a model already
deferred during the same run. Outside Actions, provider cooldowns expire after
five minutes. Reports record the models used and fallback reasons. If every
configured model is unavailable, unresolved work stays queued for the next run.

The local profile filter excludes only explicit unrelated title fields and
appointment types. Unknown titles and potential engineering/physical-science
fits continue to the LLM. Materials evidence preserves interdisciplinary
searches, including chemical/biological engineering, biomaterials and soft matter.
