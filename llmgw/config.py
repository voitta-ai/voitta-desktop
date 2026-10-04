"""Where the LLM accounts live, and the timings that keep them signed in."""

import paths

DATA_DIR = paths.ROOT / "llm"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"

# Refresh OAuth access tokens this long before they expire.
REFRESH_MARGIN_S = 5 * 60
# Background sweep: how often, and how far ahead it looks.
REFRESH_SWEEP_EVERY_S = 10 * 60
REFRESH_SWEEP_AHEAD_S = 30 * 60

REQUEST_LOG_SIZE = 200
