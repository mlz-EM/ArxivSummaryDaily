[Daily Arxiv Paper Summary](https://mlz-em.github.io/personal-site/arxiv)

Forked from [https://github.com/dong-zehao/ArxivSummaryDaily](https://github.com/dong-zehao/ArxivSummaryDaily) and pipelined to [personal site](https://mlz-em.github.io/personal-site/arxiv)

Daily job processing uses `gemini-3.8-flash`, which Google lists as free-tier
eligible as of November 2, 2026. See [pricing](https://ai.google.dev/gemini-api/docs/pricing)
and [model limits](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash).

Job batches grow according to input and output budgets, with a ceiling of 500
jobs and a default output budget that permits about 240 short records. Long
descriptions reduce the batch size. Context/output-limit errors split a batch.
`--batch-size` can lower the ceiling for an individual source.

Each model response explicitly lists accepted and rejected URLs. Both decisions
are saved after every batch; omitted or failed jobs stay pending. Daily quota
exhaustion stops the remaining generation calls across workflow steps. Other
transient errors have bounded retries. `*SummaryReport.json` files (and
`jobsDailyReport.json`) list unresolved URLs and reasons; the Actions job summary
also shows the remaining queue counts.

The local profile filter excludes only explicit unrelated title fields and
appointment types. Unknown titles and potential engineering/physical-science
fits continue to the LLM. Materials evidence preserves interdisciplinary
searches, including chemical/biological engineering, biomaterials and soft matter.
