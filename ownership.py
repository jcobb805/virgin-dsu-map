"""
Mineral ownership enrichment for virgin DSU units.

Assigns each unit a tier for high-grading by private (fee) mineral ownership:
  FEE   - no federal/state signal; private mineral owners -> actionable
  MIXED - partial federal/state (fed com units, partial overlay) -> some fee owners
  GOV   - federal/state minerals dominate -> N/A for mineral buying

Two evidence layers:
  1. BLM federal mineral estate GIS overlay (area % of unit where the US owns
     the oil & gas). Services: WY, and NM-office layer covering NM/TX/OK/KS, UT.
  2. Well/lease name markers (FED, STATE, SL, USA, tribal, COM) - operators bake
     mineral ownership into well names; used everywhere, decisive where no GIS.

Standalone: `python ownership.py` re-enriches data.js in place.
From fetch.py: `ownership.enrich_units(units)` before writing data.js.
"""
import json
import math
import os
import re

import requests

BASE = os.path.dirname(os.path.abspath(__file__))

FED_SERVICES = {
    # state -> (query url, commodity field, {codes meaning federal O&G})
    'WY': ('https://gis.blm.gov/wyarcgis/rest/services/Lands/BLM_WY_FederalMineralEstate/FeatureServer/0/query',
           'FEDMIN'),
    'NM': ('https://gis.blm.gov/nmarcgis/rest/services/Lands/BLM_NM_Lands_Mineral_Estate/FeatureServer/0/query',
           'minown'),
    'TX': ('https://gis.blm.gov/nmarcgis/rest/services/Lands/BLM_NM_Lands_Mineral_Estate/FeatureServer/0/query',
           'minown'),
    'OK': ('https://gis.blm.gov/nmarcgis/rest/services/Lands/BLM_NM_Lands_Mineral_Estate/FeatureServer/0/query',
           'minown'),
    'KS': ('https://gis.blm.gov/nmarcgis/rest/services/Lands/BLM_NM_Lands_Mineral_Estate/FeatureServer/0/query',
           'minown'),
    'UT': ('https://gis.blm.gov/utarcgis/rest/services/Lands/BLM_UT_Federal_Minerals/FeatureServer/0/query',
           None),  # commodity field auto-detected; default = all federal
}

# Commodity codes meaning the US owns the OIL & GAS (coal-only 'C' and
# other-minerals 'T' leave the O&G in private/state hands).
FED_OG_CODES = {'A', 'O', 'G'}


# ---------------------------------------------------------------- name markers
FED_RE = re.compile(r'\bFED(ERAL)?\b|\bUSA\b|\bBLM\b')
STATE_RE = re.compile(r'\bSTATE\b|\bSTATE COM\b|^SL \d')
TRIBAL_RE = re.compile(r'\bTRIBAL\b|\bALLOT+E?E?\b|\bNAVAJO\b|\bUTE\b|\bINDIAN\b')
COM_RE = re.compile(r'\bCOM\b|\bCOMM\b|\bUNIT\b')


def name_flags(unit):
    text = ' '.join(
        [unit.get('name') or ''] +
        [p.get('name') or '' for p in unit['permits']] +
        [p.get('lease') or '' for p in unit['permits']]
    ).upper()
    return {
        'fed': bool(FED_RE.search(text)),
        'state': bool(STATE_RE.search(text)),
        'tribal': bool(TRIBAL_RE.search(text)),
        'com': bool(COM_RE.search(text)),
    }


# ---------------------------------------------------------------- geometry
def point_in_rings(lon, lat, rings):
    """Even-odd rule across all rings (handles holes)."""
    inside = False
    for ring in rings:
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > lat) != (yj > lat) and \
               lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def sample_grid(ring, nu=36, nv=12):
    """Sample points across the unit rectangle (ring = 4 [lat,lon] corners)."""
    c0, c1, c2, c3 = ring[0], ring[1], ring[2], ring[3]
    pts = []
    for i in range(nu):
        s = (i + 0.5) / nu
        # edge c0->c1 and edge c3->c2 (opposite sides)
        ax = c0[1] + (c1[1] - c0[1]) * s
        ay = c0[0] + (c1[0] - c0[0]) * s
        bx = c3[1] + (c2[1] - c3[1]) * s
        by = c3[0] + (c2[0] - c3[0]) * s
        for k in range(nv):
            t = (k + 0.5) / nv
            pts.append((ax + (bx - ax) * t, ay + (by - ay) * t))  # (lon, lat)
    return pts


def fed_overlay_pct(unit):
    """% of unit area where the US owns the oil & gas, via BLM GIS. None if no service."""
    svc = FED_SERVICES.get(unit['state'])
    if not svc:
        return None
    url, field = svc
    ring_ll = [[p[1], p[0]] for p in unit['ring']]  # -> [lon, lat]
    ring_ll.append(ring_ll[0])
    params = {
        'f': 'json',
        'geometry': json.dumps({'rings': [ring_ll], 'spatialReference': {'wkid': 4326}}),
        'geometryType': 'esriGeometryPolygon',
        'inSR': 4326,
        'spatialRel': 'esriSpatialRelIntersects',
        'outFields': field or '*',
        'returnGeometry': 'true',
        'outSR': 4326,
    }
    r = requests.post(url, data=params, timeout=60)
    r.raise_for_status()
    d = r.json()
    if 'error' in d:
        raise RuntimeError(d['error'].get('message', 'ArcGIS error'))
    feats = d.get('features', [])
    fed_polys = []
    for ft in feats:
        attrs = ft.get('attributes') or {}
        if field:
            code = str(attrs.get(field) or '').strip().upper()[:1]
            if code and code not in FED_OG_CODES:
                continue  # coal-only / other-minerals: O&G not federal
        rings = (ft.get('geometry') or {}).get('rings')
        if rings:
            fed_polys.append(rings)
    if not fed_polys:
        return 0
    pts = sample_grid(unit['ring'])
    hits = 0
    for lon, lat in pts:
        for rings in fed_polys:
            if point_in_rings(lon, lat, rings):
                hits += 1
                break
    return round(100.0 * hits / len(pts))


# ---------------------------------------------------------------- tiers
def assign_tier(unit, fed_pct, flags):
    """Returns (tier, basis, note)."""
    pure_state = flags['state'] and not flags['com']
    if fed_pct is not None:
        basis = 'BLM overlay'
        if fed_pct >= 75:
            return 'GOV', basis, f'{fed_pct}% federal O&G minerals'
        if fed_pct >= 15:
            return 'MIXED', basis, f'{fed_pct}% federal O&G minerals - fee tracts in remainder'
        # <15% federal by area
        if flags['tribal']:
            return 'MIXED', basis + ' + name', 'tribal interest named - verify'
        if pure_state:
            return 'MIXED', basis + ' + name', 'state minerals named - verify state vs fee split'
        if flags['state']:
            return 'MIXED', basis + ' + name', f'{fed_pct}% federal; STATE in name - com unit with state tracts'
        return 'FEE', basis, f'{fed_pct}% federal O&G minerals'
    # no GIS service - names decide
    basis = 'name markers'
    if flags['tribal']:
        return 'MIXED', basis, 'tribal interest named - verify'
    if flags['fed']:
        if flags['com']:
            return 'MIXED', basis, 'FED COM - communitized, some fee tracts likely'
        return 'GOV', basis, 'FED-named unit'
    if flags['state']:
        if flags['com']:
            return 'MIXED', basis, 'STATE com unit - some fee tracts likely'
        return 'GOV', basis, 'state-named unit'
    return 'FEE', basis, 'no fed/state markers'


def enrich_units(units, log=print):
    n_gis = n_err = 0
    for u in units:
        flags = name_flags(u)
        fed_pct = None
        try:
            fed_pct = fed_overlay_pct(u)
            if fed_pct is not None:
                n_gis += 1
        except Exception as e:
            n_err += 1
            log(f'  overlay failed for {u["id"]} ({u["state"]}): {e}')
        tier, basis, note = assign_tier(u, fed_pct, flags)
        u['tier'] = tier
        u['fedPct'] = fed_pct
        u['tierBasis'] = basis
        u['tierNote'] = note
    counts = {}
    for u in units:
        counts[u['tier']] = counts.get(u['tier'], 0) + 1
    log(f'  ownership tiers: {counts} ({n_gis} via BLM overlay, {n_err} overlay errors)')
    return units


# ---------------------------------------------------------------- standalone
if __name__ == '__main__':
    fp = os.path.join(BASE, 'data.js')
    raw = open(fp, encoding='utf-8').read()
    d = json.loads(raw[raw.index('=') + 1:].rstrip().rstrip(';'))
    enrich_units(d['units'])
    d['meta']['tierCounts'] = {t: sum(1 for u in d['units'] if u['tier'] == t)
                               for t in ('FEE', 'MIXED', 'GOV')}
    with open(fp, 'w', encoding='utf-8') as f:
        f.write('window.VIRGIN_DATA = ')
        json.dump(d, f, separators=(',', ':'))
        f.write(';\n')
    print('data.js re-enriched.')
    for u in sorted(d['units'], key=lambda x: (x['tier'], (x['nearestProd'] or {}).get('distMi', 99))):
        np_ = u['nearestProd']
        print(f"{u['tier']:5} {'fed=' + str(u['fedPct']) + '%' if u['fedPct'] is not None else 'name-based':>10}  "
              f"{u['state']} {u['county'][:14]:14} {', '.join(u['ops'])[:30]:30} {u['name'][:22]:22} "
              f"near={(str(np_['distMi']) + 'mi') if np_ else '>15mi':>7}  {u['tierNote']}")
