# Virgin DSU Screener

Finds US **horizontal permits approved in the last 90 days** whose drilling unit has
**no well that has ever produced** — mineral owners in these units have never received
a royalty check, and the fresh permit means a first check is coming. Prospecting tool
for buying minerals ahead of first production.

## Files
- `fetch.py` — Enverus v3 pipeline. Pulls nationwide permits, screens each permit's
  unit corridor against every well that has ever produced in that state, groups sibling
  permits into units, diffs against `history.json`, writes `data.js`.
- `index.html` — dashboard (Leaflet map + grid). Open directly or via `serve.bat`.
- `data.js` — generated dataset consumed by the dashboard.
- `history.json` — permit first-seen dates + run log (drives the "new this week" alert).
- `refresh.ps1` — weekly refresh + Windows toast alert. Wired to scheduled task
  **"Virgin DSU Weekly Refresh"** (Mondays 7:00 AM; runs late if the machine was off).
- `_cache/` — same-day API pull cache (`python fetch.py --cached` to reuse).

## Method
1. Permits: `ApprovedDate >= today-90d`, US, `Trajectory=HORIZONTAL`, `PermitStatus=ACTIVE`,
   new-drill types, injection/disposal/service excluded. Deduped per well (latest permit kept).
2. Unit corridor: permitted lateral line (fallback SHL→BHL, fallback N-S assumption),
   buffered a **half-mile each side** + quarter-mile beyond the ends — approximates the
   1-mile-wide DSU governing the lateral. Not a surveyed unit boundary.
3. Virgin test: state-wide pull of every well with any production history
   (`FirstProdDate` set, plus `LastProducingMonth` catch-up). Producing laterals are
   sampled every ~500 m; any sample point inside the corridor disqualifies the unit.
4. Context: in-progress wellbores (DUC / DRILLING / COMPLETED) inside the unit,
   nearest producer distance (step-out gauge), sibling permits grouped per unit/pad.
5. Weekly diff: permit IDs vs `history.json` → NEW badges, alert banner, toast.
6. Ownership tier (`ownership.py`): FEE / MIXED / GOV per unit. Federal O&G mineral
   % by area via BLM mineral estate GIS (WY; NM-office layer covering NM/TX/OK/KS; UT),
   name markers (FED/STATE/SL/COM/tribal) everywhere else. GOV = N/A for buying;
   dashboard defaults to Fee + Mixed.

## Maintenance
- Manual run: `python fetch.py` (~15–30 min, mostly TX/NM well pulls).
- Change window: `python fetch.py --days 120`.
- Remove schedule: `Unregister-ScheduledTask -TaskName "Virgin DSU Weekly Refresh"`.
- API key: `ENVERUS_API_KEY` user environment variable (required; never committed).
