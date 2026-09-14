# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Daily real estate distress lead detection across Ohio and Georgia counties,
feeding one shared classifier and one shared REI Reply (GoHighLevel/
LeadConnector) push. No git repo, no build step — it's a Python package run
directly on a small always-on server via cron.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium   # only needed for GA Douglas probate
cp .env.example .env                      # REIREPLY_TOKEN, REIREPLY_LOCATION_ID

python -m reileads.cli run                              # both states, default counties
python -m reileads.cli run --state oh --county cuyahoga  # one county
python -m reileads.cli run --state ga --days-back 7      # GA probate/notice lookback window
python -m reileads.cli status                            # counts by county, recent events, unpushed total
python -m reileads.cli seed-franklin                     # one-time watchlist seed, see sources/oh/franklin.py

python test_classify.py     # classifier rules, no network
python test_ga_e2e.py       # GA: mocked source data -> scored lead -> HighLevel payload
python test_oh_e2e.py       # OH: mocked ArcGIS response -> diff -> HighLevel payload
```

All three test files run against mocked data — this sandbox's egress policy
blocks county/newspaper/court hosts, so live-endpoint verification only
happens from wherever this is actually deployed. `DRY_RUN=1` by default:
every run writes `out/leads-*.csv` and nothing reaches REI Reply until a
week of CSVs have been eyeballed and `DRY_RUN=0` is set in `.env`.

No linter or formatter is configured. There's no `pytest` — the three test
files are plain scripts (`python test_x.py`), not a discoverable suite.

## Architecture

**One `Store` (`reileads/core/store.py`, SQLite), two very differently
shaped pipelines feeding it, one shared push.** `parcels` holds latest
observed state per `(county, parcel)`; `events` is the append-only table
every lead actually comes from; `pushes` makes a rerun idempotent;
`runs` is a log. `cli.py` is the only entry point — it wires
`--state`/`--county`/`--days-back` into `pipeline_oh.py` and
`pipeline_ga.py`, then always calls `core.push.push()` at the end
regardless of which `--state` ran, since unpushed events accumulate
across runs.

**Ohio (`pipeline_oh.py`, `sources/oh/*.py`) — snapshot-and-diff.** Most
Ohio counties publish a live foreclosure flag in open data, so each
source's `fetch()` yields a full daily snapshot (`{parcel, in_foreclosure,
delq_balance, payload}`), and `Store.apply_snapshot()` does the diffing —
a lead exists only when a parcel *enters* foreclosure (or its delinquent
balance jumps >5%), never merely because it's already in that state
(otherwise day one dumps the entire county into the CRM). `REGISTRY` in
`pipeline_oh.py` is the source of truth for which counties are wired up.
Franklin is the exception: no foreclosure flag exists in its schema, so it
runs off a manually seeded watchlist instead (`seed-franklin`) polled
against the Treasurer's site — see `sources/oh/franklin.py`'s docstring
before touching it. Every ArcGIS-backed source shares `core/arcgis.py`'s
`ArcGISLayer` (paged FeatureServer client); Mahoning's layer 400s unless
`outFields=*` is used, not a specific field list — documented in
`sources/oh/mahoning.py`.

**Georgia (`pipeline_ga.py`, `sources/ga/*.py`) — no diffing, re-derived
fresh every run.** Georgia foreclosures are non-judicial and never
recorded before the sale, so there's no snapshot to diff against; `run()`
inserts straight into the shared `events` table (same table Ohio uses, on
purpose — `core/push.py` doesn't know or care which pipeline created a
row), guarding against duplicate pushes via a `SELECT` before insert
rather than `apply_snapshot()`. Two independent GA sub-pipelines exist:
- `run()`/`collect()`: the legal-ad/parcel-enrichment path. **Currently
  disabled at the pipeline level** (`_no_lawful_ga_source()` stands in for
  `sources/ga/legal_ads.py`) because every wired newspaper site's Terms of
  Use bars automated access — see the module docstring in `pipeline_ga.py`
  for the exact clause and evidence. The code is fixed and ready to resume
  but must not be re-enabled without written permission (a GSCCCA letter is
  in flight; see `GSCCCA_permission_request.md`). `sources/ga/fulton_parcel.py`
  (a county ArcGIS layer, different legal basis entirely) is unaffected but
  currently has nothing to enrich since the notice source feeding it is off.
- `run_probate()`/`collect_probate()`: **live**, Douglas County probate via
  `sources/ga/douglas_probate.py`. Different mechanism from everything else
  in this repo — the site's Telerik UI doesn't respond to plain HTTP, so
  this one drives real headless Chromium via Playwright. Read that file's
  module docstring before changing it: date-range search is required (a
  county-only query returns zero rows), dates must be zero-padded
  `MM/DD/YYYY`, and **do not set a custom `User-Agent`** on the Playwright
  page — doing so silently breaks Telerik's client-side rendering and the
  search returns zero rows with no error.

Both GA sub-pipelines write different `event` values (`ga_notice` vs.
`ga_probate`) into the same table specifically so they can never collide
even if their id spaces ever overlap.

**`core/classify.py` is the part to read first when leads look wrong.**
Two deliberately separate jobs: `exclude()` is a hard filter for what
Sharon explicitly doesn't want (recent owner-occupant purchases,
government/land-bank owners) — it takes three conditions together
(recent + financed + homestead), not any one alone, since a recent cash
purchase with no homestead is an investor. `score()` requires at least one
PRIMARY signal (an actual event: foreclosure sale, estate opened, tax
lien) to produce a lead at all; AMPLIFIERS (absentee, free-and-clear,
long tenure) raise score but never create a lead by themselves.
`derive()` fills booleans from raw per-source fields and treats missing
data as unknown (`None`), never as a confirmed negative — a missing
`homestead_exemption` must not silently count as "no homestead" and
inflate a score. `test_classify.py`'s 11 cases each encode a real rule
from a conversation with Sharon; a failing case means the pipeline is
about to send (or withhold) a lead she said she didn't want.

**`core/push.py` + `core/reireply.py` are fully state-agnostic** — they
read `payload["state"]`/`payload["county"]` rather than being told which
pipeline produced a row, so both states get identical CSV output, tagging,
and DRY_RUN/MAX_PUSH_PER_RUN safety rails. `to_contact()` in
`reireply.py` deliberately puts the property address in custom fields,
never in `address1` (which is the owner's *mailing* address) — conflating
the two is the classic way to mail a vacant house.

**`core/normalize.py`** has the shared name/address parsing every county
source calls into (`split_owner`, `is_entity`, `strip_parcel`). Note
`absentee()` here is Ohio-hardcoded (checks against `"OH"`/`"OHIO"`) —
`douglas_probate.py` and `classify.py`'s own `derive()` compute absentee
inline instead rather than calling it, precisely to stay state-correct
for Georgia.

## Known gaps (check `README.md` before assuming a source works)

- GA legal-ad scraping (Fulton/Cobb/Cherokee/Douglas newspapers) and
  georgiapublicnotice.com are both explicitly ruled out by ToS — don't
  re-enable or replicate either without written permission on file.
  GSCCCA (statewide deed/lien index) likewise — draft letter only.
- Cobb, Cherokee, Douglas parcel enrichment (owner name/mailing address
  for the disabled legal-ad path) is a real dead end for each, not just
  unbuilt — see README for the specific finding per county.
- Montgomery County (OH) has no address source wired up; not in
  `pipeline_oh.REGISTRY`.
- Douglas probate can't distinguish a renter's last address from an owned
  property, and has no reliable case-type distinction (Year's Support vs.
  plain probate) — treat both as documented, open caveats, not bugs to fix
  blindly.
