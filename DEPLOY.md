# Getting this actually running

You said you'll run this yourself with Claude Code on your own server. Here's
the concrete path — about 30 minutes of setup, then it runs itself.

## 1. Get a server

Any small always-on Linux box works. Cheapest options: a $6/mo DigitalOcean
droplet, a Linode Nanode, or an AWS Lightsail instance. Pick "Ubuntu 24.04",
smallest size — this pipeline does almost no compute, it just polls a few
web endpoints once a day.

Why a server and not your laptop: it needs to be on and connected every day,
including when your laptop is closed.

## 2. Put Claude Code on that server

Once the server is up, SSH into it and install Claude Code there (same tool
you're using right now, just running on the server instead of your laptop).
Anthropic's install docs: `docs.claude.com` → Claude Code → install.

## 3. Get this project onto the server

Upload `rei-leads.zip` to the server (drag-and-drop if your SSH client
supports it, or `scp rei-leads.zip you@yourserver:~/`), then:

```bash
unzip rei-leads.zip && cd rei-leads
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
cp .env.example .env
```

The `playwright install` step downloads a real (headless) Chromium build --
needed for the Douglas County GA probate source, which drives a Telerik
UI that doesn't respond correctly to plain HTTP requests. It's a ~200MB
download the first time; skip it and everything else still works, but
GA probate leads won't.

## 4. Fill in `.env`

Two values, both from your GoHighLevel/REI Reply account:

- `REIREPLY_TOKEN` — Settings → Private Integrations → create one with
  Contacts read/write scope. Copy the token it gives you.
- `REIREPLY_LOCATION_ID` — visible in your account URL or Settings → Company.

Leave `DRY_RUN=1` for now — that's step 6.

## 5. One-time setup for Franklin County (Ohio)

Franklin doesn't publish a live foreclosure flag like Cuyahoga and Hamilton
do, so it starts from a watchlist instead of a daily diff:

```bash
python -m reileads.cli seed-franklin
```

Run this once. It's fine to re-run it later if you want to refresh the list.

## 6. Watch it for a week before it touches your CRM

```bash
python -m reileads.cli run
```

With `DRY_RUN=1`, this pulls today's leads, scores them, and writes them to
`out/leads-<date>.csv` — nothing goes to REI Reply yet. Open that CSV each
day. You're checking two things: are the leads real (open one address in
Google Maps, does it look right), and is anything showing up that you
wouldn't have wanted (an owner-occupant who just bought, a government
parcel). If the classifier is wrong about something, that's the moment to
catch it — not after 500 leads have already hit your CRM.

## 7. Turn it on for real

Once a week of CSVs look right, open `.env` and set:

```
DRY_RUN=0
```

From here, every run pushes new leads straight into REI Reply, tagged by
state, county, and which signal triggered it (`signal-foreclosure-sale`,
`signal-ga-notice`, etc.) — set up your GoHighLevel workflows to trigger off
those tags.

## 8. Make it run every day without you

```bash
crontab -e
```

Add one line (runs at 6am server time — adjust the hour if you want):

```
0 6 * * * cd /home/youruser/rei-leads && .venv/bin/python -m reileads.cli run >> logs/run.log 2>&1
```

Make the log folder first: `mkdir -p logs`. From then on, check
`logs/run.log` occasionally, or run `python -m reileads.cli status` any time
to see counts by county and how many leads are sitting unpushed.

## What to check periodically, not just once

- `logs/run.log` — silent failures (a county changing its API without
  warning) show up here first, not as an obvious symptom.
- REI Reply's own delivery — a lead reaching REI Reply isn't the same as a
  workflow actually firing on it; confirm your tag-triggered automations
  are catching what you expect.
- The gaps documented in `README.md`'s "What's not verified yet" section —
  those get resolved by watching real output for a week or two after
  deploy, from a network that can actually reach the county/newspaper
  sites (this sandbox can't, which is exactly why this step has to happen
  on your server, not here).
