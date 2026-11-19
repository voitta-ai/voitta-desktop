import os
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("LLMGW_PORT", "18910"))

DATA_DIR = Path(os.environ.get("LLMGW_HOME", Path.home() / ".voitta-llm-poc"))
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
INVITES_FILE = DATA_DIR / "invites.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"

# Refresh OAuth access tokens this long before they expire.
REFRESH_MARGIN_S = 5 * 60
# Background sweep: how often, and how far ahead it looks.
REFRESH_SWEEP_EVERY_S = 10 * 60
REFRESH_SWEEP_AHEAD_S = 30 * 60

REQUEST_LOG_SIZE = 200
