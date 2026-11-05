"""
ArXiv API configuration.
"""

import os

# arXiv API search settings
SEARCH_CONFIG = {
    'max_total_results': 50,         # Max total number of papers to fetch
    'sort_by': 'SubmittedDate',       # Sort by: Relevance, LastUpdatedDate, SubmittedDate
    'sort_order': 'Descending',       # Sort order: Ascending, Descending
    'include_cross_listed': True,     # Include cross-listed papers
    'abstracts': True,                # Include abstracts
    'id_list': None,                  # Search specific papers by ID
    'title_only': False,              # Search only in title
    'author_only': False,             # Search only by author
    'abstract_only': False,           # Search only in abstract
    'search_mode': 'all'             # Search mode: 'all' (any keyword), 'any' (all keywords)
}

# Fixed search categories
CATEGORIES = [
    "cond-mat.supr-con",  # Superconductivity
    "cond-mat.str-el",    # Strongly correlated electrons
    "cond-mat.mtrl-sci",  # Materials science
    "cond-mat.mes-hall",  # Quantum Hall effect
    "physics.app-ph",
    "physics.comp-ph",
    "physics.ins-det",
    "physics.optics"
]

# Search query config. Use OR or AND between keywords; empty is allowed.
# QUERY = "nickelate OR cuprate"   # Papers in CATEGORIES containing nickelate or cuprate
# QUERY = "nickelate AND cuprate"  # Papers in CATEGORIES containing both nickelate and cuprate
QUERY = '(all:"ptychography" OR all:"electron microscopy")'     # Papers in CATEGORIES

# LLM API settings
LLM_CONFIG = {
    'api_key': os.getenv("LLM_API_KEY", "YOUR_API_HERE"),                  # API key from the environment
    'model': os.getenv("LLM_MODEL", "gemini-3.8-flash"),                  # Try 3.8 first, then independent free-tier fallbacks
    'fallback_models': [name.strip() for name in os.getenv("LLM_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash-lite").split(",") if name.strip()],
    'api_url': "https://generativelanguage.googleapis.com/v1beta/models",   # API base URL
    'temperature': 0.5,                                                     # Temperature
    'max_output_tokens': 65536,                                             # Supported by both configured models
    'job_batch_size': 500,                                                  # Ceiling; token budgets reduce long batches
    'job_batch_input_tokens': 100000,                                       # Fit many jobs without half-million-token requests
    'job_output_tokens_per_item': 256,                                      # Budget for an all-accepted response
    'top_p': 0.8,                                                           # Top P
    'top_k': 40,                                                            # Top K
    'retry_count': 2,                                                       # Short rate-limit retries; 5xx uses cooldowns instead
    'retry_delay': 5,                                                       # Retry delay (seconds)
    'service_retry_delays': [300, 900, 1800],                                # Recovery passes after about 5, 15, then 30 minutes
    'retry_jitter_seconds': 30,                                             # Avoid synchronized retries
    'max_requests_per_model': 18,                                           # Count all API attempts across sources in one run
    'max_service_failures_per_model': 4,                                    # Initial attempt plus three delayed attempts
    'max_run_seconds': 5400,                                                # Shared 90-minute LLM budget, including cooldowns
    'timeout': 180,                                                         # Bound each attempt before trying the fallback
}

# Output settings
OUTPUT_DIR = "data"
LAST_RUN_FILE = "last_run.json"  # Persist the newest fetched arXiv entry for incremental runs


# Job query
JOB_CONFIG = {
    'site_name': ["indeed", "linkedin", "google", ],
    'search_term': "professor tenure -adjunct -dean -chair -lecturer -temporary -medical -clinical -visiting",
    'google_search_term': "tenure tracked professor in the north america",
    'location': "USA",
    'results_wanted': 100,
    'hours_old': 30*24,
    'country_indeed': 'USA',
    'linkedin_fetch_description': True  # Gets more info such as description, direct job URL (slower)
    # proxies=["208.195.175.46:65095", "208.195.175.45:65095", "localhost"],
}


# Interfolio public-position discovery. The historical bootstrap is deliberately
# run locally once; scheduled runs use the committed raw/state JSON afterward.
INTERFOLIO_CONFIG = {
    'minimum_posted_date': os.getenv("INTERFOLIO_MINIMUM_POSTED_DATE", "2026-06-01"),
    'start_id': int(os.getenv("INTERFOLIO_START_ID", "180000")),
    'lookahead': int(os.getenv("INTERFOLIO_LOOKAHEAD", "400")),
    'recent_days': int(os.getenv("INTERFOLIO_RECENT_DAYS", "7")),
    'request_delay': float(os.getenv("INTERFOLIO_REQUEST_DELAY", "0.5")),
    'timeout': float(os.getenv("INTERFOLIO_TIMEOUT", "30")),
    'retry_count': int(os.getenv("INTERFOLIO_RETRY_COUNT", "4")),
    'retry_delay': float(os.getenv("INTERFOLIO_RETRY_DELAY", "2")),
    'checkpoint_every': int(os.getenv("INTERFOLIO_CHECKPOINT_EVERY", "50")),
    'workers': int(os.getenv("INTERFOLIO_WORKERS", "8")),
    'summary_batch_size': int(os.getenv("INTERFOLIO_SUMMARY_BATCH_SIZE", "500")),
}


# Chronicle job discovery uses the site's public current-job sitemap. Sixteen
# workers completed the controlled 60-page probe without errors or throttling;
# shared request spacing and exponential backoff reduce pressure if that changes.
CHRONICLE_CONFIG = {
    'minimum_posted_date': os.getenv("CHRONICLE_MINIMUM_POSTED_DATE", "2026-06-01"),
    'request_delay': float(os.getenv("CHRONICLE_REQUEST_DELAY", "0.02")),
    'timeout': float(os.getenv("CHRONICLE_TIMEOUT", "30")),
    'retry_count': int(os.getenv("CHRONICLE_RETRY_COUNT", "4")),
    'retry_delay': float(os.getenv("CHRONICLE_RETRY_DELAY", "2")),
    'checkpoint_every': int(os.getenv("CHRONICLE_CHECKPOINT_EVERY", "200")),
    'workers': int(os.getenv("CHRONICLE_WORKERS", "16")),
    'summary_batch_size': int(os.getenv("CHRONICLE_SUMMARY_BATCH_SIZE", "500")),
}


# Inside Higher Ed uses the same public Madgex sitemap/HTML format as
# Chronicle. The bootstrap inspects current sitemap pages but queues only jobs
# posted on or after June 1, 2026.
INSIDE_HIGHER_ED_CONFIG = {
    'minimum_posted_date': os.getenv("INSIDE_HIGHER_ED_MINIMUM_POSTED_DATE", "2026-06-01"),
    'request_delay': float(os.getenv("INSIDE_HIGHER_ED_REQUEST_DELAY", "0.02")),
    'timeout': float(os.getenv("INSIDE_HIGHER_ED_TIMEOUT", "30")),
    'retry_count': int(os.getenv("INSIDE_HIGHER_ED_RETRY_COUNT", "4")),
    'retry_delay': float(os.getenv("INSIDE_HIGHER_ED_RETRY_DELAY", "2")),
    'checkpoint_every': int(os.getenv("INSIDE_HIGHER_ED_CHECKPOINT_EVERY", "200")),
    'workers': int(os.getenv("INSIDE_HIGHER_ED_WORKERS", "16")),
    'summary_batch_size': int(os.getenv("INSIDE_HIGHER_ED_SUMMARY_BATCH_SIZE", "500")),
}
