"""Shared core for the ICOS ancillary viewer (Dash app + static export).

Resolves a station code OR an archive PID to that station's L2 ancillary data,
parses the BADM BIF long format into per-group tables, and builds interactive
Plotly figures (curated soil/chem profiles/texture/foliar + generic fallback).
Data-driven: only the variable groups actually reported for the station appear,
so forest / grassland / cropland / wetland stations all work unchanged.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import bif_parser as bp

HERE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("ANCILLARY_CACHE", HERE / "cache"))
CACHE.mkdir(parents=True, exist_ok=True)
ETC_ARCHIVE = "http://meta.icos-cp.eu/resources/cpmeta/etcArchiveProduct"
CYAN, INK = "#00abc9", "#222"

# dictionary is station-independent; bundled with the app (BE-Bra copy as fallback)
_DICT_PATH = next((p for p in (HERE / "BIF_Ancillary_Variables.csv",
                               HERE / "BE-Bra_BIF_Ancillary_Variables.csv") if p.exists()), None)
DIC = bp.load_dictionary(_DICT_PATH).set_index("VARIABLE") if _DICT_PATH else pd.DataFrame()

TITLES = {
    "GRP_SPP_O": "Overstory species", "GRP_SPP": "Species composition",
    "GRP_BIOMASS": "Biomass", "GRP_BASAL_AREA": "Basal area",
    "GRP_DBH": "Diameter at breast height", "GRP_HEIGHTC": "Canopy height",
    "GRP_TREES_NUM": "Tree density",
    "GRP_LAI": "Leaf area index", "GRP_LITTER": "Litterfall",
    "GRP_VEG_CHEM": "Foliar / vegetation chemistry", "GRP_SOIL_CHEM": "Soil chemistry",
    "GRP_SOIL_STOCK": "Soil C/N stock", "GRP_SOIL_TEX": "Soil texture",
    "GRP_SOIL_DEPTH": "Soil depth", "GRP_SOIL_CLASSIFICATION": "Soil classification",
    "GRP_SOIL_WRB_GROUP": "Soil WRB reference group",
}
# forest-style groups first, then shared vegetation, then soil; unknowns appended.
ORDER = ["GRP_SPP_O", "GRP_SPP", "GRP_BIOMASS", "GRP_BASAL_AREA", "GRP_DBH", "GRP_HEIGHTC",
         "GRP_TREES_NUM", "GRP_LAI", "GRP_LITTER", "GRP_VEG_CHEM",
         "GRP_SOIL_CHEM", "GRP_SOIL_STOCK", "GRP_SOIL_TEX", "GRP_SOIL_DEPTH",
         "GRP_SOIL_CLASSIFICATION", "GRP_SOIL_WRB_GROUP"]
SPECS = {
    "GRP_SPP":        {"kind": "cover", "species": "SPP_O", "value": "SPP_O_PERC"},
    "GRP_SPP_O":      {"kind": "cover", "species": "SPP_O", "value": "SPP_O_PERC"},
    "GRP_SOIL_CHEM":  {"kind": "profile", "measures": ["C_ORG", "N_TOT", "CN_RATIO", "BD"]},
    "GRP_SOIL_STOCK": {"kind": "profile", "measures": ["C_ORG", "N_TOT"]},
    "GRP_SOIL_TEX":   {"kind": "texture", "measures": ["CLAY", "SILT", "SAND", "ROCK"]},
    "GRP_VEG_CHEM":   {"kind": "elements",
                       "measures": ["CONC_C", "CONC_N", "CONC_P", "CONC_K", "CONC_CA", "CONC_MG"],
                       "by": "SPP"},
}
TEX_COLORS = {"CLAY": "#1f9e89", "SILT": "#fdc23e", "SAND": "#e76f51", "ROCK": "#9aa0a6"}


# ---------------- helpers ----------------

def num(s):
    return pd.to_numeric(s, errors="coerce")


def title_of(g):
    return TITLES.get(g, g.replace("GRP_", "").replace("_", " ").capitalize())


def unit_of(var):
    try:
        u = DIC.loc[var, "UNIT"]
    except (KeyError, TypeError):
        return ""
    if pd.isna(u) or str(u).startswith("LIST") or u in ("free text",):
        return ""
    return str(u)


def desc_of(var):
    try:
        return str(DIC.loc[var, "DESCRIPTION"])
    except (KeyError, TypeError):
        return ""


def mean_rows(d, var):
    stat = f"{var}_STATISTIC"
    s = d.copy()
    s[var] = num(s[var])
    if stat in s.columns:
        s = s[(s[stat] == "Mean") | s[stat].isna()]
    return s.dropna(subset=[var])


# ---------------- data access (station OR pid) ----------------

def _cookie():
    from icoscp_core.icos import auth
    return auth.get_token().cookie_value


def available_stations():
    """Sorted list of station codes that have an ETC L2 archive (cached to disk)."""
    import json
    cache = CACHE / "stations.json"
    if cache.exists():
        return json.loads(cache.read_text())
    from icoscp_core.icos import meta
    objs = meta.list_data_objects(datatype=ETC_ARCHIVE, limit=2000)
    sites = sorted({o.filename.split("_")[1] for o in objs
                    if o.filename and o.filename.startswith("ICOSETC_")})
    cache.write_text(json.dumps(sites))
    return sites


def resolve(station=None, pid=None):
    """Return (station_code, archive_hash). Either input may be given."""
    from icoscp_core.icos import meta
    if pid:
        uri = pid if str(pid).startswith("http") else f"https://meta.icos-cp.eu/objects/{pid}"
        fn = meta.get_dobj_meta(uri).fileName          # ICOSETC_<station>_ARCHIVE_L2.zip
        return fn.split("_")[1], uri.rsplit("/", 1)[-1]
    objs = meta.list_data_objects(datatype=ETC_ARCHIVE, limit=2000)
    hit = next((o for o in objs if f"ICOSETC_{station}_" in (o.filename or "")), None)
    if not hit:
        raise ValueError(f"no ETC L2 archive found for station {station!r}")
    return station, hit.uri.rsplit("/", 1)[-1]


def fetch_ancillary(station=None, pid=None):
    """Return (station_code, raw BIF DataFrame). Serves from the cache when present
    (no network/auth needed); otherwise extracts the ancillary CSV from the L2 archive."""
    if station:                                  # cache hit by station code -> offline-friendly
        cached = CACHE / f"ICOSETC_{station}_ANCILLARY_L2.csv"
        if cached.exists():
            return station, bp.load_bif(cached)
    code, h = resolve(station, pid)
    cache = CACHE / f"ICOSETC_{code}_ANCILLARY_L2.csv"
    if not cache.exists():
        import requests
        url = f"https://data.icos-cp.eu/zip/{h}/extractFile/ICOSETC_{code}_ANCILLARY_L2.csv"
        # on ICOS's own servers this needs no auth; elsewhere a session cookie is used
        headers = {}
        try:
            headers["Cookie"] = _cookie()
        except Exception:
            pass
        r = requests.get(url, headers=headers, timeout=180)
        r.raise_for_status()
        cache.write_bytes(r.content)
    return code, bp.load_bif(cache)


def groups_present(bif):
    present = sorted(bif.VARIABLE_GROUP.unique())
    return [g for g in ORDER if g in present] + [g for g in present if g not in ORDER]


# ---------------- Plotly figures ----------------

def _style(fig, height):
    fig.update_layout(template="plotly_white", height=height,
                      margin=dict(l=10, r=10, t=46, b=10),
                      title_font_color=INK, font_family="Segoe UI, Arial")
    return fig


def _profile(w, group, spec):
    pfx = group.replace("GRP_", "")
    dmin, dmax = f"{pfx}_PROFILE_MIN", f"{pfx}_PROFILE_MAX"
    if dmin not in w.columns:
        return None
    panels = []
    for m in spec["measures"]:
        var = f"{pfx}_{m}"
        if var not in w.columns:
            continue
        s = mean_rows(w, var)
        if s.empty:
            continue
        s = s.assign(_d=(num(s[dmin]) + num(s[dmax])) / 2).dropna(subset=["_d"]).sort_values("_d")
        if not s.empty:
            panels.append((m, var, s))
    if not panels:
        return None
    fig = make_subplots(rows=1, cols=len(panels), shared_yaxes=True,
                        subplot_titles=[f"{m.replace('_', ' ')} [{unit_of(v)}]" for m, v, _ in panels])
    for i, (m, var, s) in enumerate(panels, 1):
        fig.add_trace(go.Scatter(x=s[var], y=s["_d"], mode="lines+markers",
                                 line=dict(color=CYAN), name=m), row=1, col=i)
    fig.update_yaxes(autorange="reversed", row=1, col=1, title_text="depth [cm] (0 = surface)")
    _style(fig, 430).update_layout(showlegend=False, title=f"{title_of(group)} — depth profile")
    recs = [(f"{num(pd.Series([r[dmin]]))[0]:g} to {num(pd.Series([r[dmax]]))[0]:g}",
             m.replace("_", " "), r[var]) for m, var, s in panels for _, r in s.iterrows()]
    tab = (pd.DataFrame(recs, columns=["depth [cm]", "measure", "mean"])
             .pivot_table(index="depth [cm]", columns="measure", values="mean", aggfunc="mean"))
    return fig, tab, "Mean by soil depth layer."


def _texture(w, group, spec):
    pfx = group.replace("GRP_", "")
    dmin, dmax = f"{pfx}_PROFILE_MIN", f"{pfx}_PROFILE_MAX"
    rows = {}
    for f in spec["measures"]:
        var = f"{pfx}_{f}"
        if var not in w.columns:
            continue
        s = mean_rows(w, var)
        if s.empty:
            continue
        rows[f.title()] = (s.assign(_lo=num(s[dmin]), _hi=num(s[dmax]))
                            .groupby(["_lo", "_hi"])[var].mean())
    if not rows:
        return None
    mat = pd.DataFrame(rows).sort_index()
    labels = [f"{lo:g}–{hi:g}" for lo, hi in mat.index]
    fig = go.Figure()
    for f in mat.columns:
        fig.add_bar(y=labels, x=mat[f].fillna(0), orientation="h", name=f,
                    marker_color=TEX_COLORS.get(f.upper(), "#888"))
    fig.update_layout(barmode="stack", xaxis_title="% by mass",
                      yaxis=dict(autorange="reversed", title="depth layer [cm]"),
                      title=f"{title_of(group)} fractions by depth")
    _style(fig, max(260, 60 * len(mat) + 120))
    tab = mat.copy()
    tab.index = labels
    tab.index.name = "depth [cm]"
    return fig, tab, "Particle-size fractions per soil depth layer."


def _elements(w, group, spec):
    pfx = group.replace("GRP_", "")
    by = f"{pfx}_{spec['by']}" if f"{pfx}_{spec['by']}" in w.columns else None
    ms = [(m, f"{pfx}_{m}") for m in spec["measures"]
          if f"{pfx}_{m}" in w.columns and not mean_rows(w, f"{pfx}_{m}").empty]
    if not ms:
        return None
    n = len(ms)
    ncol = min(3, n)
    nrow = math.ceil(n / ncol)
    fig = make_subplots(rows=nrow, cols=ncol,
                        subplot_titles=[f"{m.replace('CONC_', '')} [{unit_of(v)}]" for m, v in ms])
    tab = {}
    for idx, (m, var) in enumerate(ms):
        r, c = idx // ncol + 1, idx % ncol + 1
        s = mean_rows(w, var)
        agg = (s.groupby(by)[var].mean().sort_values() if by and s[by].notna().any()
               else pd.Series({"all": s[var].mean()}))
        fig.add_trace(go.Bar(y=agg.index.astype(str), x=agg.values, orientation="h",
                             marker_color=CYAN), row=r, col=c)
        tab[m.replace("CONC_", "")] = agg
    _style(fig, 240 * nrow + 60).update_layout(
        showlegend=False, title=f"{title_of(group)} — mean concentration"
        + (f" by {spec['by'].lower()}" if by else ""))
    return fig, pd.DataFrame(tab), "Mean elemental concentrations."


def _cover(w, group, spec):
    # schema varies by site: cover value is SPP_O_PERC (most sites) or a numeric SPP_O (BE-Bra);
    # species name is the text column SPP_O, SPP_O_SPP, or any *_SPP.
    val = next((c for c in ("SPP_O_PERC", "SPP_PERC") if c in w.columns and num(w[c]).notna().any()), None)
    if val is None and "SPP_O" in w.columns and num(w["SPP_O"]).notna().any():
        val = "SPP_O"
    if val is None:
        return None
    sp = next((c for c in ("SPP_O_SPP", "SPP_O", "SPP_SPP", "SPP")
               if c in w.columns and c != val and w[c].notna().any() and num(w[c]).isna().all()), None)
    if sp is None:
        sp = next((c for c in w.columns if c.endswith("_SPP") and c != val
                   and w[c].notna().any() and num(w[c]).isna().all()), None)
    if sp is None:
        return None
    d = w.copy()
    d[val] = num(d[val])
    stat = f"{val}_STATISTIC"
    if stat in d.columns:                       # keep representative stats only
        d = d[d[stat].isin(["Mean", "Single observation"]) | d[stat].isna()]
    d = d.dropna(subset=[val, sp])
    if d.empty:
        return None
    agg = d.groupby(sp)[val].mean().sort_values().tail(20)
    if agg.empty:
        return None
    unit = ""
    if "SPP_PERC_UNIT" in w.columns and w["SPP_PERC_UNIT"].notna().any():
        unit = str(w["SPP_PERC_UNIT"].dropna().mode().iat[0])
    unit = unit or "%"
    fig = go.Figure(go.Bar(y=agg.index.astype(str), x=agg.values, orientation="h",
                           marker_color=CYAN))
    fig.update_layout(xaxis_title=f"cover [{unit}]",
                      title=f"{title_of(group)} — mean cover by species")
    _style(fig, max(260, 26 * len(agg) + 120))
    tab = agg.sort_values(ascending=False).to_frame(f"cover [{unit}]")
    tab.index.name = "species"
    return fig, tab, desc_of(val) or "Species / vegetation type percent cover."


def _primary_var(w, group):
    base = group.replace("GRP_", "")
    skip = ("_STATISTIC_NUMBER", "_DATE", "_DATE_START", "_DATE_END", "_UNC", "_LAT", "_LONG")
    for cand in (base, base + "_PERC"):
        if cand in w.columns and num(w[cand]).notna().any():
            return cand
    best, best_n = None, 0
    for c in w.columns:
        if c in ("SITE_ID", "GROUP_ID", "YEAR") or any(c.endswith(s) for s in skip):
            continue
        n = num(w[c]).notna().sum()
        if n > best_n:
            best, best_n = c, n
    return best


def _category(w):
    for suff in ("_SPP", "_ORGAN", "_LIFESTAGE", "_TYPE"):
        hit = next((c for c in w.columns if c.endswith(suff)), None)
        if hit and w[hit].notna().any() and w[hit].nunique() > 1:
            return hit
    return "YEAR" if "YEAR" in w.columns else None


def _generic(w, group):
    pv = _primary_var(w, group)
    if pv is None:
        return None
    w = w.copy()
    w[pv] = num(w[pv])
    stat = next((c for c in w.columns if c.endswith("_STATISTIC")), None)
    d = w[w[stat] == "Mean"].copy() if stat else w.copy()
    if d[pv].notna().sum() == 0:
        d = w.copy()
    cat = _category(d)
    unit = unit_of(pv)
    if not unit or "specified in" in unit.lower():
        ucol = pv + "_UNIT"
        if ucol in d.columns and d[ucol].notna().any():
            unit = str(d[ucol].dropna().mode().iat[0])
    if not (cat and d[cat].notna().any()):
        return None
    agg = d.dropna(subset=[pv]).groupby(cat)[pv].mean().sort_values().tail(14)
    fig = go.Figure(go.Bar(y=agg.index.astype(str), x=agg.values, orientation="h",
                           marker_color=CYAN))
    fig.update_layout(xaxis_title=f"{pv}" + (f" [{unit}]" if unit else ""),
                      title=f"Mean {title_of(group).lower()} by {cat.lower().replace('_', ' ')}")
    _style(fig, max(260, 32 * len(agg) + 120))
    tab = (d.dropna(subset=[pv]).groupby(cat)[pv].mean().sort_values(ascending=False)
             .to_frame(pv + (f" [{unit}]" if unit else "")))
    return fig, tab, desc_of(pv)


def figure_for(group, w):
    spec = SPECS.get(group)
    out = None
    if spec:
        out = {"profile": _profile, "texture": _texture, "elements": _elements,
               "cover": _cover}[spec["kind"]](w, group, spec)
    return out or _generic(w, group)


# metadata/qualifier suffixes to hide from info cards
_META = ("_STATISTIC", "_STATISTIC_METHOD", "_STATISTIC_NUMBER", "_STATISTIC_TYPE",
         "_DATE", "_DATE_START", "_DATE_END", "_DATE_UNC", "_APPROACH", "_COMMENT",
         "_METHOD", "_UNC")


def _info(w, group):
    """For chart-less groups (e.g. soil classification): a variable -> value table."""
    recs = []
    for c in w.columns:
        if c in ("SITE_ID", "GROUP_ID", "YEAR") or c.endswith(_META):
            continue
        vals = w[c].dropna().astype(str).unique()
        if len(vals):
            recs.append((c, ", ".join(vals[:8])))
    if not recs:
        return None
    tab = pd.DataFrame(recs, columns=["variable", "value"]).set_index("variable")
    return None, tab, "Reported values."


def section_for(group, w):
    """(fig | None, table, desc). Falls back to an info table when no chart applies."""
    return figure_for(group, w) or _info(w, group)
