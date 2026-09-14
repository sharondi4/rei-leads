"""Minimal paged ArcGIS REST FeatureServer client.

Every Ohio county open-data parcel layer we use speaks this dialect, so one
client covers Cuyahoga and Hamilton and anything we add later.
"""
import time
import logging
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from . import config

log = logging.getLogger(__name__)


class ArcGISError(RuntimeError):
    pass


class ArcGISLayer:
    def __init__(self, url: str, page_size: int, oid_field: str = "OBJECTID"):
        self.url = url.rstrip("/")
        self.page_size = page_size
        self.oid_field = oid_field
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((requests.RequestException, ArcGISError)),
        reraise=True,
    )
    def _get(self, params: dict) -> dict:
        r = self.session.get(
            f"{self.url}/query", params=params, timeout=config.REQUEST_TIMEOUT
        )
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError as e:
            raise ArcGISError(f"non-JSON response from {self.url}: {r.text[:200]}") from e
        # ArcGIS returns HTTP 200 with an error envelope. Treat it as a failure.
        if "error" in data:
            raise ArcGISError(f"{self.url}: {data['error']}")
        return data

    def count(self, where: str) -> int:
        d = self._get({"where": where, "returnCountOnly": "true", "f": "json"})
        return int(d.get("count", 0))

    def fetch_all(self, where: str, out_fields):
        """Yield attribute dicts, paging until the server stops sending.

        Ordering by the OID field makes paging deterministic. Without it,
        ArcGIS can repeat or skip rows across pages.
        """
        fields = ",".join(out_fields)
        offset = 0
        seen = 0
        while True:
            data = self._get({
                "where": where,
                "outFields": fields,
                "returnGeometry": "false",
                "orderByFields": self.oid_field,
                "resultOffset": offset,
                "resultRecordCount": self.page_size,
                "f": "json",
            })
            feats = data.get("features", [])
            if not feats:
                break
            for f in feats:
                yield f.get("attributes", {})
            seen += len(feats)
            log.info("  %s rows fetched", f"{seen:,}")
            if len(feats) < self.page_size and not data.get("exceededTransferLimit"):
                break
            offset += self.page_size
            time.sleep(config.PAGE_PAUSE_SECONDS)
