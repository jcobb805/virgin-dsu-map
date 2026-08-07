"""
Virgin DSU Screener - Enverus v3 pipeline.

Finds US horizontal drilling permits approved in the last N days (default 90)
whose drilling unit (approximated as a 1-mile-wide corridor around the
permitted lateral) contains NO well that has ever produced. These are
"virgin DSUs" - mineral owners in them have never received a royalty check,
and a fresh permit means first payment is coming.

Pipeline:
  1. Pull all US permits approved >= cutoff (nationwide, one query).
  2. Client-side filter: US, HORIZONTAL, ACTIVE, new-drill types, oil&gas.
  3. Build lateral corridor per permit (permitted line > SHL-BHL > N-S fallback).
  4. Per state: pull ALL wells that have ever produced (FirstProdDate not null,
     plus LastProducingMonth catch-up), test every candidate corridor for hits.
  5. Also pull in-progress wellbores (DUC/drilling/completed) for unit context.
  6. Group sibling permits (same pad / overlapping corridors) into units.
  7. Diff against history.json -> flag new-this-run permits/units.
  8. Write data.js (consumed by index.html dashboard) + update history.json.

Usage:
  python fetch.py            normal weekly run
  python fetch.py --cached   reuse today's cached API pulls (dev iteration)
  python fetch.py --days 90  change lookback window
"""
import argparse
import gzip
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from enverus_developer_api import DeveloperAPIv3

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, '_cache')
os.makedirs(CACHE, exist_ok=True)

API_KEY = os.environ.get('ENVERUS_API_KEY')
if not API_KEY:
    raise SystemExit('ENVERUS_API_KEY not set (user env var on this machine).')

MI = 1609.344            # meters per mile
HALF_WIDTH_M = 0.5 * MI  # corridor half-width beyond lateral (half-mile spacing each side)
END_PAD_M = 0.25 * MI    # padding beyond lateral ends
CELL = 0.02              # grid cell size, degrees (~1.4 mi)
NEAR_SCAN_DEG = 0.25     # nearest-producer scan half-window (~17 mi)

PERMIT_FIELDS = ','.join([
    'PermitID', 'WellID', 'PadID', 'API_UWI', 'WellName', 'WellNumber',
    'ENVOperator', 'RawOperator', 'ApprovedDate', 'SubmittedDate', 'ExpiredDate',
    'PermitType', 'PermitStatus', 'Trajectory', 'WellType', 'Country',
    'StateProvince', 'County', 'District', 'ENVBasin', 'ENVPlay', 'ENVInterval',
    'Latitude', 'Longitude', 'Latitude_BH', 'Longitude_BH', 'GeomPermitted_Line',
    'PermittedLateralLength_FT', 'PermitDepth_FT', 'PermittedMeasuredDepth_FT',
    'PermittedTrueVerticalDepth_FT', 'Formation', 'LeaseName', 'Lease_Acres',
    'Section', 'Township', 'Range', 'Abstract', 'Block', 'Survey', 'ENVWellStatus',
])

PROD_WELL_FIELDS = ','.join([
    'API_UWI', 'WellName', 'ENVOperator', 'Latitude', 'Longitude',
    'Latitude_BH', 'Longitude_BH', 'FirstProdDate', 'ENVWellStatus', 'Trajectory',
])

ACTIVITY_FIELDS = ','.join([
    'API_UWI', 'WellName', 'ENVOperator', 'Latitude', 'Longitude',
    'Latitude_BH', 'Longitude_BH', 'ENVWellStatus', 'Trajectory',
    'SpudDate', 'CompletionDate',
])
ACTIVITY_STATUSES = ['DUC', 'DRILLING', 'COMPLETED']

EXCLUDE_WELLTYPE_KEYWORDS = ('INJECT', 'DISPOSAL', 'SWD', 'SERVICE', 'STORAGE', 'SUPPLY')
NEW_DRILL_TYPES = {'NEW DRILL', 'AMENDED', None, ''}


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- geometry
def parse_wkt_line(wkt):
    """Return [(lon, lat), ...] from LINESTRING/MULTILINESTRING WKT."""
    if not wkt:
        return []
    nums = re.findall(r'(-?\d+\.?\d*)\s+(-?\d+\.?\d*)', wkt)
    return [(float(a), float(b)) for a, b in nums]


def haversine_mi(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3958.7613 * math.asin(math.sqrt(a))


class Corridor:
    """Oriented rectangle around a lateral, in a local tangent plane."""

    def __init__(self, pts_ll, approx=False, axis=None):
        # pts_ll: [(lon, lat), ...] at least 2 points; axis: optional (ax, ay) override
        self.approx = approx
        self.lat0 = pts_ll[0][1]
        self.lon0 = pts_ll[0][0]
        self.kx = 111320.0 * math.cos(math.radians(self.lat0))
        self.ky = 110540.0
        xy = [self._xy(lon, lat) for lon, lat in pts_ll]
        if axis:
            ax, ay = axis
        else:
            ax, ay = xy[-1][0] - xy[0][0], xy[-1][1] - xy[0][1]
        n = math.hypot(ax, ay)
        if n < 50 and not axis:          # degenerate: default N-S axis
            ax, ay = 0.0, 1.0
        else:
            ax, ay = ax / n, ay / n
        self.ax, self.ay = ax, ay        # along-lateral unit vector
        px, py = -ay, ax                 # perpendicular unit vector
        ts = [x * ax + y * ay for x, y in xy]
        ss = [x * px + y * py for x, y in xy]
        tc, sc = (min(ts) + max(ts)) / 2, (min(ss) + max(ss)) / 2
        self.half_len = (max(ts) - min(ts)) / 2 + END_PAD_M
        self.half_wid = (max(ss) - min(ss)) / 2 + HALF_WIDTH_M
        self.cx = ax * tc + px * sc
        self.cy = ay * tc + py * sc
        self.line_len_m = max(ts) - min(ts)

    def _xy(self, lon, lat):
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def contains(self, lat, lon):
        x, y = self._xy(lon, lat)
        dx, dy = x - self.cx, y - self.cy
        t = dx * self.ax + dy * self.ay
        s = -dx * self.ay + dy * self.ax
        return abs(t) <= self.half_len and abs(s) <= self.half_wid

    def _ll(self, x, y):
        return (self.lat0 + y / self.ky, self.lon0 + x / self.kx)

    def ring(self):
        """Corner ring [[lat, lon] x4] for Leaflet."""
        ax, ay = self.ax, self.ay
        px, py = -ay, ax
        pts = []
        for st, ss in ((1, 1), (1, -1), (-1, -1), (-1, 1)):
            x = self.cx + ax * self.half_len * st + px * self.half_wid * ss
            y = self.cy + ay * self.half_len * st + py * self.half_wid * ss
            lat, lon = self._ll(x, y)
            pts.append([round(lat, 6), round(lon, 6)])
        return pts

    def centroid(self):
        return self._ll(self.cx, self.cy)

    def bbox(self):
        ring = self.ring()
        lats = [p[0] for p in ring]
        lons = [p[1] for p in ring]
        return (min(lats), min(lons), max(lats), max(lons))

    def azimuth(self):
        return math.degrees(math.atan2(self.ax, self.ay)) % 180.0


def build_corridor(p):
    """Corridor from permitted line > SHL-BHL > synthetic N-S box."""
    pts = parse_wkt_line(p.get('GeomPermitted_Line'))
    if len(pts) >= 2:
        return Corridor(pts)
    slat, slon = p.get('Latitude'), p.get('Longitude')
    blat, blon = p.get('Latitude_BH'), p.get('Longitude_BH')
    if blat and blon and (abs(blat - slat) > 1e-4 or abs(blon - slon) > 1e-4):
        return Corridor([(slon, slat), (blon, blat)])
    # No lateral geometry: assume N-S lateral centered on SHL
    lat_ft = p.get('PermittedLateralLength_FT') or 10000
    half_deg = (lat_ft * 0.3048 / 2) / 110540.0
    return Corridor([(slon, slat - half_deg), (slon, slat + half_deg)], approx=True)


# ---------------------------------------------------------------- cache
def cache_path(name):
    return os.path.join(CACHE, f'{name}_{date.today().isoformat()}.json.gz')


def cache_load(name):
    fp = cache_path(name)
    if os.path.exists(fp):
        with gzip.open(fp, 'rt', encoding='utf-8') as f:
            return json.load(f)
    return None


def cache_save(name, obj):
    with gzip.open(cache_path(name), 'wt', encoding='utf-8') as f:
        json.dump(obj, f, default=str)


# ---------------------------------------------------------------- fetch
def fetch_permits(v3, cutoff, use_cache):
    if use_cache:
        c = cache_load('permits')
        if c is not None:
            log(f'  [cache] permits: {len(c)}')
            return c
    rows = []
    for row in v3.query('permits', ApprovedDate=f'ge({cutoff})', deleteddate='null',
                        fields=PERMIT_FIELDS, pagesize=10000):
        rows.append(row)
    cache_save('permits', rows)
    return rows


def fetch_state_wells(v3, st, use_cache):
    """All wells in state that have ever produced -> compact tuples."""
    if use_cache:
        c = cache_load(f'wells_{st}')
        if c is not None:
            log(f'  [cache] wells {st}: {len(c)}')
            return c
    wells = []
    seen = set()

    def take(row):
        api = row.get('API_UWI')
        if api in seen or not (row.get('Latitude') and row.get('Longitude')):
            return
        seen.add(api)
        wells.append([
            row['Latitude'], row['Longitude'],
            row.get('Latitude_BH'), row.get('Longitude_BH'),
            row.get('WellName') or '', row.get('ENVOperator') or '',
            str(row.get('FirstProdDate') or '')[:10],
            row.get('ENVWellStatus') or '',
            (row.get('Trajectory') or '')[:1],   # H/V/D/U
        ])

    for row in v3.query('wells', StateProvince=st, FirstProdDate='ge(1800-01-01)',
                        deleteddate='null', fields=PROD_WELL_FIELDS, pagesize=10000):
        take(row)
    n1 = len(wells)
    # catch-up: produced volume but null FirstProdDate
    try:
        for row in v3.query('wells', StateProvince=st, FirstProdDate='null',
                            LastProducingMonth='ge(1800-01-01)', deleteddate='null',
                            fields=PROD_WELL_FIELDS, pagesize=10000):
            take(row)
    except Exception as e:
        log(f'    catch-up query failed for {st}: {e}')
    log(f'  {st}: {n1} FirstProdDate + {len(wells) - n1} catch-up = {len(wells)} produced wells')
    cache_save(f'wells_{st}', wells)
    return wells


def fetch_state_activity(v3, st, use_cache):
    """Non-producing wellbores in progress (DUC / drilling / completed)."""
    if use_cache:
        c = cache_load(f'act_{st}')
        if c is not None:
            log(f'  [cache] activity {st}: {len(c)}')
            return c
    rows = []
    seen = set()

    def take(row):
        api = row.get('API_UWI')
        if api in seen or not (row.get('Latitude') and row.get('Longitude')):
            return
        seen.add(api)
        rows.append([
            row['Latitude'], row['Longitude'],
            row.get('Latitude_BH'), row.get('Longitude_BH'),
            row.get('WellName') or '', row.get('ENVOperator') or '',
            row.get('ENVWellStatus') or '',
            str(row.get('SpudDate') or '')[:10],
            str(row.get('CompletionDate') or '')[:10],
        ])

    try:
        for row in v3.query('wells', StateProvince=st,
                            ENVWellStatus=f'in({",".join(ACTIVITY_STATUSES)})',
                            deleteddate='null', fields=ACTIVITY_FIELDS, pagesize=10000):
            take(row)
    except Exception:
        for status in ACTIVITY_STATUSES:
            try:
                for row in v3.query('wells', StateProvince=st, ENVWellStatus=status,
                                    deleteddate='null', fields=ACTIVITY_FIELDS,
                                    pagesize=10000):
                    take(row)
            except Exception as e:
                log(f'    activity status {status} failed for {st}: {e}')
    cache_save(f'act_{st}', rows)
    return rows


# ---------------------------------------------------------------- spatial index
def sample_points(slat, slon, blat, blon):
    """Points along SHL-BHL every ~500 m (both ends always included)."""
    pts = [(slat, slon)]
    if blat and blon and (abs(blat - slat) > 1e-5 or abs(blon - slon) > 1e-5):
        dist_m = haversine_mi(slat, slon, blat, blon) * MI
        steps = max(1, int(dist_m // 500))
        for i in range(1, steps):
            f = i / steps
            pts.append((slat + (blat - slat) * f, slon + (blon - slon) * f))
        pts.append((blat, blon))
    return pts


def build_grid(wells):
    """wells: compact tuples -> {cell: [(lat, lon, widx), ...]}"""
    grid = defaultdict(list)
    for i, w in enumerate(wells):
        for lat, lon in sample_points(w[0], w[1], w[2], w[3]):
            grid[(int(lat / CELL), int(lon / CELL))].append((lat, lon, i))
    return grid


def grid_query(grid, bbox):
    lat1, lon1, lat2, lon2 = bbox
    out = []
    for cy in range(int(lat1 / CELL) - 1, int(lat2 / CELL) + 2):
        for cx in range(int(lon1 / CELL) - 1, int(lon2 / CELL) + 2):
            out.extend(grid.get((cy, cx), ()))
    return out


def nearest_producer(grid, wells, clat, clon):
    best, best_i = None, None
    for cy in range(int((clat - NEAR_SCAN_DEG) / CELL), int((clat + NEAR_SCAN_DEG) / CELL) + 1):
        for cx in range(int((clon - NEAR_SCAN_DEG) / CELL), int((clon + NEAR_SCAN_DEG) / CELL) + 1):
            for lat, lon, i in grid.get((cy, cx), ()):
                d = haversine_mi(clat, clon, lat, lon)
                if best is None or d < best:
                    best, best_i = d, i
    if best is None:
        return None
    w = wells[best_i]
    return {'name': w[4], 'op': w[5], 'firstProd': w[6], 'distMi': round(best, 1)}


# ---------------------------------------------------------------- grouping
class UnionFind:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def group_units(cands):
    """cands: list of dicts with corridor, permit. Same pad or overlapping
    corridors (centroid < 1.2 mi + azimuth within 30 deg, or < 0.35 mi any)."""
    uf = UnionFind(len(cands))
    by_pad = defaultdict(list)
    for i, c in enumerate(cands):
        pad = c['permit'].get('PadID')
        if pad:
            by_pad[pad].append(i)
    for idxs in by_pad.values():
        for j in idxs[1:]:
            uf.union(idxs[0], j)
    # spatial bucketing to avoid O(n^2) over full set
    buckets = defaultdict(list)
    for i, c in enumerate(cands):
        clat, clon = c['centroid']
        buckets[(int(clat / 0.03), int(clon / 0.03))].append(i)
    for (cy, cx), idxs in buckets.items():
        neigh = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                neigh.extend(buckets.get((cy + dy, cx + dx), ()))
        for i in idxs:
            ci = cands[i]
            for j in neigh:
                if j <= i:
                    continue
                cj = cands[j]
                if ci['permit']['StateProvince'] != cj['permit']['StateProvince']:
                    continue
                d = haversine_mi(*ci['centroid'], *cj['centroid'])
                if d > 1.2:
                    continue
                daz = abs(ci['corridor'].azimuth() - cj['corridor'].azimuth())
                daz = min(daz, 180 - daz)
                if d <= 0.35 or daz <= 30:
                    uf.union(i, j)
    groups = defaultdict(list)
    for i in range(len(cands)):
        groups[uf.find(i)].append(i)
    return list(groups.values())


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--cached', action='store_true', help="reuse today's cached pulls")
    args = ap.parse_args()

    today = date.today()
    cutoff = (today - timedelta(days=args.days)).isoformat()
    v3 = DeveloperAPIv3(secret_key=API_KEY, retries=5)

    log(f'=== Virgin DSU Screener === cutoff {cutoff} (last {args.days} days)')

    # 1-2: permits
    log('[1/5] Fetching nationwide permits...')
    raw = fetch_permits(v3, cutoff, args.cached)
    log(f'  raw permits: {len(raw)}')

    cands_by_state = defaultdict(list)
    skipped = Counter()
    seen_well = set()
    # newest ApprovedDate first so dedupe keeps the latest permit per well
    raw.sort(key=lambda r: str(r.get('ApprovedDate') or ''), reverse=True)
    for p in raw:
        if (p.get('Country') or 'US') != 'US':
            skipped['non-US'] += 1
            continue
        if (p.get('Trajectory') or '').upper() != 'HORIZONTAL':
            skipped['not horizontal'] += 1
            continue
        if (p.get('PermitStatus') or '').upper() not in ('ACTIVE', ''):
            skipped['inactive status'] += 1
            continue
        if (p.get('PermitType') or '').upper() not in NEW_DRILL_TYPES:
            skipped['not new drill'] += 1
            continue
        wt = (p.get('WellType') or '').upper()
        if any(k in wt for k in EXCLUDE_WELLTYPE_KEYWORDS):
            skipped['service/injection'] += 1
            continue
        if not (p.get('Latitude') and p.get('Longitude')):
            skipped['no coords'] += 1
            continue
        key = p.get('WellID') or p.get('API_UWI') or p.get('PermitID')
        if key in seen_well:
            skipped['dupe (same well)'] += 1
            continue
        seen_well.add(key)
        cor = build_corridor(p)
        clat, clon = cor.centroid()
        cands_by_state[p['StateProvince']].append({
            'permit': p, 'corridor': cor, 'centroid': (clat, clon),
        })
    n_cand = sum(len(v) for v in cands_by_state.values())
    log(f'  candidates: {n_cand} US horizontal new-drill permits '
        f'across {len(cands_by_state)} states')
    log(f'  skipped: {dict(skipped)}')

    # 3-5: per-state production screen
    log('[2/5] Screening states for existing production...')
    virgin = []
    for st in sorted(cands_by_state, key=lambda s: -len(cands_by_state[s])):
        cl = cands_by_state[st]
        log(f'  -- {st}: {len(cl)} candidates')
        wells = fetch_state_wells(v3, st, args.cached)
        grid = build_grid(wells)
        activity = fetch_state_activity(v3, st, args.cached)
        agrid = build_grid(activity)
        st_virgin = 0
        for c in cl:
            cor = c['corridor']
            bbox = cor.bbox()
            hits = [i for lat, lon, i in grid_query(grid, bbox) if cor.contains(lat, lon)]
            if hits:
                c['producedHits'] = len(set(hits))
                continue
            c['nearest'] = nearest_producer(grid, wells, *c['centroid'])
            acts = {}
            for lat, lon, i in grid_query(agrid, bbox):
                if i not in acts and cor.contains(lat, lon):
                    acts[i] = activity[i]
            c['activity'] = [
                {'name': a[4], 'op': a[5], 'status': a[6], 'spud': a[7], 'comp': a[8]}
                for a in acts.values()
            ]
            virgin.append(c)
            st_virgin += 1
        log(f'     virgin: {st_virgin}')
        del wells, grid, activity, agrid

    log(f'[3/5] Virgin permits total: {len(virgin)}')

    # 6: group into units
    groups = group_units(virgin)
    log(f'[4/5] Grouped into {len(groups)} units')

    # 7: history diff
    hist_fp = os.path.join(BASE, 'history.json')
    hist = {'permits': {}, 'runs': []}
    if os.path.exists(hist_fp):
        with open(hist_fp, encoding='utf-8') as f:
            hist = json.load(f)
    known = hist['permits']
    today_iso = today.isoformat()
    new_pids = set()
    for c in virgin:
        pid = str(c['permit']['PermitID'])
        if pid not in known:
            known[pid] = today_iso
            new_pids.add(pid)

    # 8: build output units
    units = []
    for gi in groups:
        members = [virgin[i] for i in gi]
        members.sort(key=lambda c: str(c['permit'].get('ApprovedDate') or ''), reverse=True)
        lead = members[0]['permit']
        # merged corridor: longest member's axis, extents from all member endpoints
        all_pts = []
        longest = max(members, key=lambda c: c['corridor'].line_len_m)
        for c in members:
            cor = c['corridor']
            for st_ in (1, -1):
                x = cor.cx + cor.ax * (cor.half_len - END_PAD_M) * st_
                y = cor.cy + cor.ay * (cor.half_len - END_PAD_M) * st_
                lat, lon = cor._ll(x, y)
                all_pts.append((lon, lat))
        base = longest['corridor']
        merged = Corridor(all_pts, axis=(base.ax, base.ay),
                          approx=any(c['corridor'].approx for c in members))
        clat, clon = merged.centroid()
        max_lat_ft = max(
            (c['permit'].get('PermittedLateralLength_FT') or
             c['corridor'].line_len_m / 0.3048) for c in members)
        sections = max(1, round(max_lat_ft / 5280))
        nearest = min((c.get('nearest') for c in members if c.get('nearest')),
                      key=lambda n: n['distMi'], default=None)
        acts = {a['name']: a for c in members for a in c.get('activity', [])}
        pmts = []
        for c in members:
            p = c['permit']
            pid = str(p['PermitID'])
            pmts.append({
                'pid': pid,
                'name': p.get('WellName') or '',
                'api': p.get('API_UWI') or '',
                'op': p.get('ENVOperator') or p.get('RawOperator') or 'UNKNOWN',
                'approved': str(p.get('ApprovedDate') or '')[:10],
                'expires': str(p.get('ExpiredDate') or '')[:10],
                'latft': round(p.get('PermittedLateralLength_FT') or
                               c['corridor'].line_len_m / 0.3048),
                'formation': p.get('Formation') or '',
                'depth': p.get('PermittedTrueVerticalDepth_FT') or p.get('PermitDepth_FT'),
                'lease': p.get('LeaseName') or '',
                'ptype': p.get('PermitType') or '',
                'isNew': pid in new_pids,
                'firstSeen': known.get(pid, today_iso),
            })
        ops = sorted({m['op'] for m in pmts})
        str_parts = []
        if lead.get('Section'):
            str_parts.append(f"Sec {lead['Section']}")
        if lead.get('Township'):
            str_parts.append(f"T{lead['Township']}")
        if lead.get('Range'):
            str_parts.append(f"R{lead['Range']}")
        if lead.get('Abstract'):
            str_parts.append(f"Abs {lead['Abstract']}")
        if lead.get('Block'):
            str_parts.append(f"Blk {lead['Block']}")
        if lead.get('Survey'):
            str_parts.append(str(lead['Survey']))
        units.append({
            'id': f"{lead['StateProvince']}-{round(clat * 1000)}-{round(clon * 1000)}",
            'name': (lead.get('LeaseName') or lead.get('WellName') or 'UNIT').strip(),
            'state': lead.get('StateProvince') or '',
            'county': (lead.get('County') or '').title(),
            'basin': lead.get('ENVBasin') or '',
            'play': lead.get('ENVPlay') or '',
            'interval': lead.get('ENVInterval') or '',
            'str': ' '.join(str_parts),
            'ops': ops,
            'ring': merged.ring(),
            'centroid': [round(clat, 5), round(clon, 5)],
            'estAcres': sections * 640,
            'maxLatFt': round(max_lat_ft),
            'approxGeom': merged.approx,
            'nearestProd': nearest,
            'activity': sorted(acts.values(), key=lambda a: a['name']),
            'permits': pmts,
            'latestApproved': max(m['approved'] for m in pmts),
            'firstSeen': min(m['firstSeen'] for m in pmts),
            'isNew': all(m['isNew'] for m in pmts),
            'hasNew': any(m['isNew'] for m in pmts),
        })
    units.sort(key=lambda u: u['latestApproved'], reverse=True)

    new_units = sum(1 for u in units if u['isNew'])
    first_run = len(hist['runs']) == 0
    hist['runs'].append({
        'date': today_iso, 'cutoff': cutoff,
        'candidates': n_cand, 'virginPermits': len(virgin),
        'virginUnits': len(units), 'newPermits': len(new_pids),
        'newUnits': new_units,
    })
    hist['runs'] = hist['runs'][-104:]
    with open(hist_fp, 'w', encoding='utf-8') as f:
        json.dump(hist, f, indent=1)

    # 9: data.js
    out = {
        'meta': {
            'generated': datetime.now().isoformat(timespec='seconds'),
            'cutoff': cutoff,
            'windowDays': args.days,
            'firstRun': first_run,
            'candidates': n_cand,
            'virginPermits': len(virgin),
            'virginUnits': len(units),
            'newPermits': len(new_pids),
            'newUnits': new_units,
            'states': sorted(cands_by_state),
            'runs': hist['runs'][-12:],
        },
        'units': units,
    }
    with open(os.path.join(BASE, 'data.js'), 'w', encoding='utf-8') as f:
        f.write('window.VIRGIN_DATA = ')
        json.dump(out, f, separators=(',', ':'))
        f.write(';\n')
    log(f'[5/5] Wrote data.js: {len(units)} virgin units, {len(virgin)} permits '
        f'({len(new_pids)} new this run, {new_units} new units)')
    log('Done.')


if __name__ == '__main__':
    main()
