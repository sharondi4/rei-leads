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

DEALMACHINE_API_KEY = os.getenv("DEALMACHINE_API_KEY", "")

# No contact reaches REI Reply without a skip trace attempt first. A
# contact with no phone can't be dialled and can't be deduplicated by
# HighLevel (it matches on phone or email), so pushing one costs a CRM
# record and buys nothing.
REQUIRE_SKIPTRACE = _bool("REQUIRE_SKIPTRACE", True)

# Ceiling on DealMachine credits per billing month. The Basic plan grants
# 10,000 and buying more is off the table, so stop short of the wall
# rather than discovering it. At ~1 credit per lead and 150 leads a day,
# six days a week, real usage lands near 3,900.
DEALMACHINE_MONTHLY_CREDIT_CAP = int(os.getenv("DEALMACHINE_MONTHLY_CREDIT_CAP", "9000"))

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
