"""
ArXiv API configuration.
"""

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
    'api_key': "YOUR_API_HERE",                                             # Set your API key here
    'model': 'gemini-3-flash-preview',                                      # Model name
    'api_url': "https://generativelanguage.googleapis.com/v1beta/models",   # API base URL
    'temperature': 0.5,                                                     # Temperature
    'max_output_tokens': 32648,                                             # Max output tokens
    'top_p': 0.8,                                                           # Top P
    'top_k': 40,                                                            # Top K
    'retry_count': 5,                                                       # Retry attempts on failure
    'retry_delay': 5,                                                       # Retry delay (seconds)
    'timeout': 600,                                                         # Request timeout (seconds)
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
