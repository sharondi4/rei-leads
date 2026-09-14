# REI Leads

Daily real estate distress lead detection across Ohio and Georgia, feeding
one shared classifier and one shared REI Reply push.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in REIREPLY_TOKEN, REIREPLY_LOCATION_ID

python -m reileads.cli run              # both states, default counties
python -m reileads.cli run --state ga --county fulton
python -m reileads.cli status
```

`DRY_RUN=1` by default. Every lead lands in `out/leads-*.csv` and nothing
reaches REI Reply until you've read a week of output and set `DRY_RUN=0`.

---

## The two states are built differently, on purpose

**Ohio** (`pipeline_oh.py`, `sources/oh/`): Cuyahoga and Hamilton publish a
daily foreclosure flag directly in county open data. Each run is a full
snapshot diffed against yesterday — a parcel becomes a lead only when it
*enters* foreclosure, never because it's already there. Franklin has no
such flag, so it runs off a watchlist instead. Full detail in the original
`ohio-foreclosure` package README; the mechanics carried over unchanged.

**Stark (Canton), Mahoning (Youngstown), and Summit (Akron) added
2026-09-10** — confirmed live and wired into the default county list, so
the daily cron picks them up with no extra setup. Two are name+address
complete (Stark, Mahoning); Summit is address-only, since its source
layer (a purpose-built county foreclosure feed, not a general parcel
table) has no owner-name field at all -- see `sources/oh/summit.py`'s
docstring. **Montgomery (Dayton) was investigated but is not wired up**:
its delinquent list has owner name and parcel number but no address, and
the county's own iasWorld Public Access parcel-search system (confirmed
reachable, no ToS restriction found) would need the same kind of
form-mapping work GPN's search took -- not done yet, not guessed at.

**Georgia (`pipeline_ga.py`, `sources/ga/`) currently produces zero leads,
on purpose.** It's non-judicial, so a foreclosure is never recorded before
the sale — it's only published in a newspaper, four weekly insertions
ahead of the first-Tuesday sale. O.C.G.A. 44-14-162(a) forces the
property's street address, city and ZIP into **bold type** in that ad,
which is what makes it reliably parseable *in principle*. `legal_ads.py`
(scraping Fulton/Cobb/Cherokee/Douglas's newspaper sites) still exists in
this repo, and its host/URL bugs were fixed on 2026-09-10 -- but it is
**disabled** in `pipeline_ga.py`, not deleted, because every one of those
four papers' Terms of Use explicitly bars automated access. Confirmed by
reading `fultonneighbor.com/site/terms.html` directly: an addendum dated
2026-07-01 prohibits "any robot, spider, script, service, software, or
any manual or automatic device, tool, or process designed to data-mine,
scrape, crawl, or otherwise collect or extract the Content ... without
our prior express written consent" -- and that same text cross-references
`mdjonline.com` (Cobb's paper) inline, confirming it's the shared
TownNews-network template all four papers carry, not a Fulton-specific
clause. Do not re-enable `LegalAdsForeclosure` without written permission
from each paper, same bar already applied to GSCCCA and
georgiapublicnotice.com below.

Fulton's open parcel API (`sources/ga/fulton_parcel.py`) is unaffected by
this -- it's a county government ArcGIS layer, a different legal basis
entirely, confirmed distinct from both GSCCCA and the newspaper sites --
but with the notice source disabled, it currently has nothing to enrich.

**Also ruled out: georgiapublicnotice.com (GPN), the Georgia Press
Association's statewide notice-search site.** Investigated at length on
2026-09-10 as a possible replacement for the fragile per-newspaper
scraping. Its search-results page has no CAPTCHA and includes real
structured fields (county, published date), but (a) its own Terms of Use
explicitly bar "unauthorized screen scraping, database scraping, or
spidering... or use of any other automated means to collect information
from the site" -- covering the search page too, not just the full-text
detail view -- and (b) even setting that aside, the property address only
ever appears in the full notice text, which sits behind a CAPTCHA wall
confirmed unconditional (a fresh, first-ever request to a detail page hit
it, and it wasn't a rate-limit artifact). GPN's paid "Smart Search"
subscription ($75/30 days) is a legitimate alternative worth trying
first, since it's a product they built for automated daily delivery --
a different agreement than scraping the free search UI -- but nothing
from GPN is in this codebase.

**Georgia's actual path forward is the GSCCCA letter below**, or a GPN
Smart Search subscription. Those cover foreclosure/lien/estate signals
statewide. Separately, **Douglas County probate is live** (added
2026-09-11) -- see `sources/ga/douglas_probate.py`. It's a different
kind of source than everything else in this repo: the site
(georgiaprobaterecords.com) is genuinely open, no ToS restriction found
anywhere, but its Telerik-based UI doesn't respond to plain HTTP
requests the way every other source here works, so this one drives a
real (headless) Chromium browser via Playwright instead -- confirmed
that's not optional, a plain `requests` POST with every field set
correctly still returns zero rows.

Each hit gets a second pass through its case-detail page
(EstateDetails.aspx), which carries the decedent's last known address
(a real, stable element ID, not text-position guessing) and a PARTIES
table listing every heir/administrator/attorney with their own name and
mailing address. The lead's contact is whichever party is tagged
"HEIR" -- never the decedent, who obviously can't be called. Confirmed
live 2026-09-11: 6 real cases in the prior 7 days, each with a property
address and a living heir's name and mailing address. Honest caveat:
the decedent's last address is a *residence*, not a confirmed *owned*
property -- this source can't tell a renter from an owner. No reliable
case-type distinction either (Year's Support vs. plain probate) -- every
row is tagged only `probate_opened`, the one thing confirmed. Only
Douglas is wired up; Fulton/Cherokee's real probate records live on a
different, paid Tyler system, and Cobb isn't on this vendor's platform
at all.

**Not built, deliberately: automating GSCCCA.** Georgia's statewide deed
and lien index (search.gsccca.org) forbids automated access in its terms
— "any robot, spider or other automated device… will be investigated," and
screen scraping brings "termination without notice." GSCCCA is a single
authority covering all 159 counties, so a ban there costs the entire
state at once, not one county. `GSCCCA_permission_request.md` is a draft
letter asking for narrow, explicit permission instead. If that comes back
yes, book-and-page lookups (which reflect a recording same-day, unlike the
instrument-type index which lags on each county's own upload schedule)
would extend this to liens, Year's Support, and estate deeds. Until then,
those signals aren't in this codebase, and they shouldn't be added to it
without that permission in hand.

---

## The classifier is the part worth reading first

`core/classify.py` does two jobs, and keeps them separate on purpose:

`exclude()` removes what Sharon explicitly doesn't want: a family that
financed a home purchase within the last 18 months and claims the
homestead exemption there. All three conditions together — a recent cash
purchase with no homestead is an investor, and those stay in.

`score()` ranks everything else 0–100. A **primary** signal (a foreclosure
sale scheduled, an estate opened, a tax lien filed) is required for a lead
to exist at all; **amplifiers** (absentee owner, no mortgage, long tenure)
raise the score but never create a lead by themselves — a homeowner of 20
years with no distress event is not a motivated seller, just a homeowner.

Test it directly:

```bash
python test_classify.py
```

11 cases, each one encoding a real rule from a conversation with Sharon —
recent owner-occupants excluded, government/land-bank owners excluded,
heirs and absentee foreclosures ranked at the top. If a case ever fails,
the pipeline is about to send (or withhold) a lead she said she didn't
want.

---

## Testing

```bash
python test_classify.py     # classifier rules, no network
python test_ga_e2e.py       # GA: mocked newspaper + parcel data -> scored lead -> HighLevel payload
python test_oh_e2e.py       # OH: mocked ArcGIS response -> diff -> HighLevel payload
```

All three run against mocked data. Nothing here has touched a real county
or newspaper server — this sandbox's egress policy blocks those hosts, so
the live endpoints need to be run from wherever you deploy this.

---

## Before you dial anything

**Georgia's mini-TCPA is stricter than Ohio's.** O.C.G.A. 46-5-27 carries a
private right of action, up to $2,000 per knowing violation with no
aggregate cap, and a 2024 amendment that added "real property" to what
counts as a solicitation. Calling hours are 8am–9pm. Verify your script
and cadence against this before scaling Georgia calling — Ohio's
equivalent statute likely doesn't reach acquisition calls at all, but
Georgia's is a real, litigated exposure.

**No ringless voicemail**, in either state. The FCC classified it as a
robocall in 2022, and the "we're only offering to buy" defense that
protects live acquisition calls under the federal DNC rules doesn't reach
it.

**Skip tracing isn't in this codebase.** REI Reply bundles it at roughly
$0.01/lookup with DNC and litigator scrub built in. Run it inside REI
Reply after the contact lands.

---

## What's not verified yet

- **The whole newspaper-scraping approach (`legal_ads.py`) is disabled,
  not just unverified** — see the Georgia section above. Its host/URL
  bugs were fixed 2026-09-10 (Fulton's hostname was wrong, Cherokee's
  paper rebranded domains, the shared `/search/` endpoint turned out to
  be a JS app), but that work is moot until a paper grants written
  permission to automate access to its site, since all four carry a ToS
  clause barring it outright. Treat the fixes as ready-to-resume, not as
  something to build further on right now.
- Fulton's parcel address matching is a fuzzy string join (a newspaper's
  free-text address against the assessor's own free-text address field).
  It's tested against the mismatches we anticipated (a missing/extra
  post-directional), not against real-world messiness like abbreviated
  street types or transposed unit numbers. Watch the match rate in the
  first week of real output.
- Cobb, Cherokee, and Douglas have no parcel enrichment source wired up
  yet — only Fulton does. Those three counties' leads will carry an
  address and a foreclosure date but no owner name or mailing address
  until a Gwinnett-style bulk file or a similar ArcGIS layer is added
  for each. Investigated 2026-09-10, and each is a real dead end for now,
  not just "not built yet":
    - **Cobb**: no parcel/property dataset exists in the county's public
      ArcGIS open-data catalog at all (47 datasets, none of them parcels
      or tax/assessor data). Their real parcel viewer sits behind a
      separate "internal portal" app, which likely means the underlying
      service isn't public. Would need a different route entirely — a
      bulk file from the tax assessor's office, e.g.
    - **Cherokee**: `gis.cherokeega.com` (and the `.gov` alias) time out
      on both port 80 and 443 from this network — not a 403 or an error
      page, a dead TCP connect. Could be this network specifically;
      worth retrying from wherever this actually deploys.
    - **Douglas**: this one's closest — `maps.douglascountyga.gov`'s
      `MapLayers/MapServer` is reachable, layer 13 is confirmed to be
      "Parcels" with exactly the fields needed (`Owner`,
      `PropertyAddress`, `address1/2/3`, `MailingAddressCity/State/
      ZipCode`), but every query against that specific layer — even
      `OBJECTID=1`, even via POST — returns `{"code": 400, "message":
      "Failed to execute query."}`, while sibling layers on the same
      server (City Limits, etc.) query fine. That's a server-side data-
      source problem on Douglas County's end, not something fixable
      from here. Worth a re-check later; if it starts working, the field
      names above are already confirmed correct.
- Homestead exemption status isn't available from Fulton's parcel layer
  at all (it lives on a separate exemption file). Every Fulton lead
  currently carries `homestead_exemption: None` — treated by the
  classifier as *unknown*, not as *no homestead* — until that file is
  wired in.
