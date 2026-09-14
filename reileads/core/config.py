import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

REIREPLY_TOKEN = os.getenv("REIREPLY_TOKEN", "")
REIREPLY_LOCATION_ID = os.getenv("REIREPLY_LOCATION_ID", "")
REIREPLY_BASE = "https://services.leadconnectorhq.com"
REIREPLY_VERSION = "2021-07-28"

DRY_RUN = _bool("DRY_RUN", True)
MAX_PUSH_PER_RUN = int(os.getenv("MAX_PUSH_PER_RUN", "400"))

DB_PATH = Path(os.getenv("DB_PATH", "./ohfc.sqlite3"))
OUT_DIR = Path(os.getenv("OUT_DIR", "./out"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Be a good citizen. These are public open-data services, not a target.
USER_AGENT = os.getenv(
    "OHFC_USER_AGENT",
    "DI-Holdings-lead-research/0.1 (+contact: sharon.i@diholdings.org)",
)
REQUEST_TIMEOUT = 90
PAGE_PAUSE_SECONDS = 0.4

# GA legal-ad sites run on a shared CDN that 429s after a handful of rapid
# requests (confirmed 2026-09-10, both in testing and on the first real
# deploy run). One pause between article fetches, not just between pages.
ARTICLE_FETCH_PAUSE_SECONDS = 3.0
