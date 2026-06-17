"""Classic ("R report") rendering engine.

Faithfully reproduces the ETC_PROCESSING_SUITE ggplot figures from the L2 BIF
data: campaign/year/species-resolved bars and time series WITH standard-deviation
error bars and value labels — as opposed to ancillary_lib's simplified
"one mean per category" charts.

Each group renders to a list of panels (subtitle, figure, table). A group may
yield several figures (e.g. live + dead + understory biomass). Groups without a
classic renderer return None so the caller can fall back to the ICOS rendering.

Campaign handling mirrors the R `add_columns()`:
    Plot = "SP" if STATISTIC_NUMBER > 10 else "CP";  Year = first 4 of DATE;
    Plot_Year = f"{Plot}_{Year}"  (e.g. "CP_2020").
This CP/SP split is the site-specific heuristic used by the original report.
"""
from __future__ import annotations

import math
import os
from collections import namedtuple
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import ancillary_lib as alib
import bif_parser as bp
from ancillary_lib import ACCENT, CACHE, HERE, INK, num

# (subtitle | None, fig | None, table DataFrame | None, desc)
# subtitle renders as an <h3> above the figure (used for stubs / fallbacks);
# normal figures carry their R title+subtitle inside the plot, so subtitle is None.
Panel = namedtuple("Panel", "subtitle fig table desc")
# an umbrella report section (matches the R report's top-level headings)
Section = namedtuple("Section", "title panels")

# common R subtitle reused by the per-species tree-metric bars
_SPP5 = "only includes species that contribute >5% to the total tree count"

# longer categorical sequence: Accent first (as in R), then a fallback ramp for
# the long tail (R used viridis for species beyond the top 8).
_VIRIDIS_TAIL = ["#440154", "#46327e", "#365c8d", "#277f8e", "#1fa187",
                 "#4ac16d", "#a0da39", "#fde725"]
LONG_SEQ = ACCENT + _VIRIDIS_TAIL


# ---------------- shared helpers ----------------

def _layout(fig, style, height, title=None, subtitle=None):
    fig.update_layout(template=style["template"], height=height,
                      margin=dict(l=10, r=10, t=70 if subtitle else 58, b=10),
                      font_family=style["font"], title_font_color=INK,
                      bargap=0.25)
    if title:
        text = title if not subtitle else \
            f'{title}<br><sup style="color:#888;font-weight:400">{subtitle}</sup>'
        fig.update_layout(title=dict(text=text, x=0.5, font=dict(size=15)))
    return fig


def _cmap(categories, seq=ACCENT):
    cats = list(dict.fromkeys(map(str, categories)))
    return {c: seq[i % len(seq)] for i, c in enumerate(cats)}


def _campaign(w, pfx):
    """Add Plot / Year / Plot_Year columns following the R heuristic."""
    w = w.copy()
    snum = num(w[f"{pfx}_STATISTIC_NUMBER"]) if f"{pfx}_STATISTIC_NUMBER" in w else pd.Series(np.nan, index=w.index)
    w["Plot"] = np.where(snum > 10, "SP", "CP")
    date = w[f"{pfx}_DATE"] if f"{pfx}_DATE" in w else pd.Series(pd.NA, index=w.index)
    ds = w[f"{pfx}_DATE_START"] if f"{pfx}_DATE_START" in w else pd.Series(pd.NA, index=w.index)
    yr = date.where(date.notna() & (date.astype(str) != ""), ds)
    w["Year"] = yr.astype(str).str.slice(0, 4)
    w["Plot_Year"] = w["Plot"] + "_" + w["Year"]
    return w


def _map_spp(series, approach):
    """R add_columns species relabelling: DEAD -> 'Dead Standing Trees',
    TOTAL / ALL SPECIES -> '.Total'."""
    a = approach.astype(str)
    out = series.astype(str).copy()
    out = out.mask(a.str.startswith("DEAD"), "Dead Standing Trees")
    out = out.mask(a.str.upper().str.startswith(("TOTAL", "ALL SPECIES")), ".Total")
    return out


def _date2(d, pfx):
    """DATE2 = coalesce(<pfx>_DATE, <pfx>_DATE_START) -> datetime (NaT on failure)."""
    a = d[f"{pfx}_DATE"] if f"{pfx}_DATE" in d.columns else pd.Series(pd.NA, index=d.index)
    b = d[f"{pfx}_DATE_START"] if f"{pfx}_DATE_START" in d.columns else pd.Series(pd.NA, index=d.index)
    s = a.where(a.notna() & (a.astype(str) != ""), b)
    return pd.to_datetime(s, format="%Y%m%d", errors="coerce")


# tree-only groups; their presence marks a forest station (vs crop/grassland)
_FOREST_GROUPS = {"GRP_DBH", "GRP_BASAL_AREA", "GRP_TREES_NUM", "GRP_SPP_O"}


def _is_forest(bif):
    return bool(set(bif["VARIABLE_GROUP"].unique()) & _FOREST_GROUPS)


def _mean_sd(d, pfx, keys, value="_v"):
    """Collapse Mean / Standard Deviation rows into mean & sd columns keyed by `keys`."""
    stat = f"{pfx}_STATISTIC"
    if stat not in d.columns:
        return None
    dd = d[d[stat].isin(["Mean", "Standard Deviation"])]
    if dd.empty:
        return None
    g = dd.groupby(keys + [stat])[value].mean().unstack(stat)
    g = g.rename(columns={"Mean": "mean", "Standard Deviation": "sd"}).reset_index()
    g.columns.name = None
    if "mean" not in g.columns:
        return None
    if "sd" not in g.columns:
        g["sd"] = np.nan
    return g.dropna(subset=["mean"])


def _err(arr):
    return dict(type="data", array=arr, visible=True, color="black", thickness=1, width=4)


def _grouped_bar(g, *, x, color, style, title, ylab, xlab="", subtitle=None, labels=True,
                 barmode="group", seq=ACCENT, height=460):
    """Dodge/stack bar chart with per-series Accent fill, black outline, SD error
    bars (column 'sd') and optional value labels — the R tree-metric figure."""
    fig = go.Figure()
    cmap = _cmap(g[color], seq)
    for c, col in cmap.items():
        d = g[g[color].astype(str) == c]
        if d.empty:
            continue
        fig.add_bar(
            x=d[x].astype(str), y=d["mean"], name=c,
            marker=dict(color=col, line=dict(color="black", width=1)),
            error_y=_err(d["sd"]) if d["sd"].notna().any() else None,
            text=[f"{v:.1f}" for v in d["mean"]] if labels else None,
            textposition="outside", textfont=dict(size=9), cliponaxis=False,
        )
    fig.update_layout(barmode=barmode, xaxis_title=xlab, yaxis_title=ylab,
                      legend_title_text=color.replace("_", " "))
    return _layout(fig, style, height, title, subtitle)


# ---------------- per-tree biomass database (external xlsx) ----------------
# The DBH histogram and spatial biomass map are NOT derivable from the L2 BIF
# product; they need the individual-tree database. We load it from ICOS_TREE_DB
# (env) or a DATAFILE_Biomass_ICOS_*.xlsx alongside the app / in the cache dir.
# Absent -> the two plots degrade to an explanatory note.

@lru_cache(maxsize=1)
def _tree_db():
    path = os.environ.get("ICOS_TREE_DB")
    if not (path and Path(path).exists()):
        hits = [p for base in (HERE, CACHE) for p in sorted(Path(base).glob("DATAFILE_Biomass_ICOS_*.xlsx"))]
        path = str(hits[-1]) if hits else None
    if not path:
        return None
    try:
        return pd.read_excel(path, engine="openpyxl")
    except Exception:                                    # noqa: BLE001
        return None


def _station(bif):
    return bif["SITE_ID"].iloc[0] if "SITE_ID" in bif.columns and len(bif) else None


def _tree_site(bif):
    """Per-tree rows for this station, excluding dead/removed/fallen trees."""
    df = _tree_db()
    if df is None or "Site" not in df.columns:
        return None
    d = df[df["Site"] == _station(bif)].copy()
    if "TREE_STATUS" in d.columns:
        d = d[~d["TREE_STATUS"].isin(["Dead", "Removed", "Fallen"])]
    return d if not d.empty else None


# ---------------- context (cross-group dependencies) ----------------

def build_context(bif):
    """Precompute values the R report derives once and reuses across tree plots:
    `selected_species` (>5% of tree count, incl. '.Total') and `dominant_species`."""
    sel, dom = [], None
    try:
        spp = bp.group_wide(bif, "GRP_SPP_O")
        if {"SPP_O", "SPP_O_SPP", "SPP_O_STATISTIC"} <= set(spp.columns):
            d = spp[spp["SPP_O_STATISTIC"] == "Mean"].copy()
            if "SPP_O_APPROACH" in d.columns:
                d = d[~d["SPP_O_APPROACH"].astype(str).str.startswith("DEAD")]
            d["_v"] = num(d["SPP_O"])
            m = d.groupby("SPP_O_SPP")["_v"].mean()
            m = m[m.index.astype(str) != ""]
            sel = list(m[m > 5].index)
            mt = m[m.index.astype(str) != ".Total"]
            dom = mt.idxmax() if len(mt) else None
    except Exception:                                    # noqa: BLE001
        pass
    return {"selected_species": sel, "dominant_species": dom}


# ---------------- tree-metric grouped bars (biomass/basal/density/dbh/height) ----------------

def _tree_metric(bif, ctx, style, group, *, approach_re, ylab, title, subtitle,
                 scale=1.0, use_selected=True):
    pfx = group[4:]
    w = bp.group_wide(bif, group)
    base, appr, spp = pfx, f"{pfx}_APPROACH", f"{pfx}_SPP"
    if base not in w.columns or appr not in w.columns or spp not in w.columns:
        return None
    w = _campaign(w, pfx)
    w["_spp"] = _map_spp(w[spp], w[appr])
    d = w[w[appr].astype(str).str.contains(approach_re, case=False, regex=True, na=False)].copy()
    if d.empty:
        return None
    d["_v"] = num(d[base]) * scale
    g = _mean_sd(d, pfx, keys=["_spp", "Plot_Year"])
    if g is None or g.empty:
        return None
    if use_selected and ctx["selected_species"]:
        # keep the >5% species AND the per-campaign '.Total' (sorts first -> leftmost),
        # matching the R report which always shows the totals alongside the species.
        g = g[g["_spp"].isin(set(ctx["selected_species"]) | {".Total"})]
    if g.empty:
        return None
    g = g.sort_values(["_spp", "Plot_Year"])
    fig = _grouped_bar(g, x="_spp", color="Plot_Year", style=style,
                       title=f"{title} at {_station(bif)}", subtitle=subtitle,
                       ylab=ylab, xlab="Species")
    tab = (g.pivot_table(index="_spp", columns="Plot_Year", values="mean")
             .rename_axis("species"))
    return [Panel(None, fig, tab, None)]


def _biomass_live(bif, ctx, style):
    return _tree_metric(bif, ctx, style, "GRP_BIOMASS", approach_re=r"^average|^TOTAL",
                        ylab="Biomass (tons/ha)", title="Standing (dry) biomass",
                        subtitle=_SPP5, scale=10.0)


def _biomass_dead(bif, ctx, style):
    return _tree_metric(bif, ctx, style, "GRP_BIOMASS", approach_re=r"^DEAD",
                        ylab="Biomass (tons/ha)", title="Biomass standing dead trees",
                        subtitle=None, scale=10.0, use_selected=False)


def _agb_understory(bif, style):
    w = bp.group_wide(bif, "GRP_BIOMASS")
    appr = "BIOMASS_APPROACH"
    if appr not in w.columns or "BIOMASS_VEGTYPE" not in w.columns or "BIOMASS_STATISTIC" not in w.columns:
        return None
    w = _campaign(w, "BIOMASS")
    d = w[w[appr].astype(str).str.startswith("AGB understory") & (w["BIOMASS_STATISTIC"] == "Mean")].copy()
    if d.empty:
        return None
    remap = {"Herb": "Ferns", "Annual Herb": "Herb", "Non-vascular": "Moss",
             "Tree": "Sapling", "Understory": None}
    d["_veg"] = d["BIOMASS_VEGTYPE"].map(lambda v: remap.get(v, v))
    d = d[d["_veg"].notna()]
    d["_v"] = num(d["BIOMASS"])
    if d.empty:
        return None
    g = d.groupby(["Year", "_veg"])["_v"].sum().reset_index()
    fig = go.Figure()
    cmap = _cmap(g["_veg"])                               # colour fixed per vegtype...
    for c in reversed(list(cmap)):                        # ...but stack/legend order reversed
        dd = g[g["_veg"] == c]
        fig.add_bar(x=dd["Year"], y=dd["_v"], name=c,
                    marker=dict(color=cmap[c], line=dict(color="black", width=1)))
    fig.update_layout(barmode="stack", xaxis_title="Year",
                      yaxis_title="AGB understory (kg/m²)", legend_title_text="Type")
    _layout(fig, style, 420, f"Annual variation in AGB understory at {_station(bif)}",
            "(n = number of CPs)")
    tab = g.pivot_table(index="Year", columns="_veg", values="_v")
    return [Panel(None, fig, tab, None)]


def basal_area(bif, ctx, style):
    return _tree_metric(bif, ctx, style, "GRP_BASAL_AREA",
                        approach_re=r"^average|^TOTAL", ylab="Basal area (m²/ha)",
                        title="Basal area (m²/ha)", subtitle=_SPP5)


def trees_num(bif, ctx, style):
    return _tree_metric(bif, ctx, style, "GRP_TREES_NUM",
                        approach_re=r"^average|^TOTAL", ylab="Tree density (#/ha)",
                        title="Number of trees per hectare", subtitle=_SPP5)


def dbh_bars(bif, ctx, style):
    return _tree_metric(bif, ctx, style, "GRP_DBH", approach_re=r"^average",
                        ylab="mean ± SD DBH (cm)",
                        title="Average tree DBH per species and plot/campaign", subtitle=_SPP5)


_NO_TREE_DB = ("Needs the individual-tree biomass database "
               "(DATAFILE_Biomass_ICOS_*.xlsx); not part of the L2 BIF product. "
               "Place the file beside the app or set ICOS_TREE_DB to enable this plot.")


def _dbh_histogram(bif, ctx, style):
    """Per-tree DBH histogram by observation year (continuous plots, dominant
    species) with a KDE overlay — R block 5."""
    d = _tree_site(bif)
    if d is None or "TREE_DBH_CM" not in d.columns:
        return _stub("DBH frequency distribution", _NO_TREE_DB)
    if "TREE_PLOT" in d.columns:                         # continuous plots only (CP)
        d = d[d["TREE_PLOT"].astype(str).str.startswith("CP")]
    dom = ctx.get("dominant_species")
    if dom and "TREE_SPP" in d.columns and (d["TREE_SPP"] == dom).any():
        d = d[d["TREE_SPP"] == dom]
    d = d.assign(_dbh=num(d["TREE_DBH_CM"])).dropna(subset=["_dbh"])
    # restrict to the actual DBH inventory campaigns (GRP_DBH years); this drops
    # the handful of trees logged in inter-campaign years (the R 2021->2020 case).
    camp = set(_campaign(bp.group_wide(bif, "GRP_DBH"), "DBH")["Year"].dropna().astype(str))
    if camp:
        d = d[d["Year"].astype(int).astype(str).isin(camp)]
    if d.empty:
        return _stub("DBH frequency distribution", _NO_TREE_DB)
    years = sorted(d["Year"].dropna().astype(int).astype(str).unique())
    cmap = _cmap(years)
    hi = float(d["_dbh"].max())
    fig = go.Figure()
    for y in years:
        v = d[d["Year"].astype(int).astype(str) == y]["_dbh"]
        fig.add_trace(go.Histogram(x=v, xbins=dict(start=0, end=hi + 5, size=5), name=y,
                      marker=dict(color=cmap[y], line=dict(color="black", width=1)), opacity=0.75))
        if len(v) > 2 and v.nunique() > 1:               # KDE overlay (count scale)
            try:
                from scipy.stats import gaussian_kde
                xs = np.linspace(0, hi + 5, 200)
                fig.add_trace(go.Scatter(x=xs, y=gaussian_kde(v)(xs) * len(v) * 5,
                              mode="lines", line=dict(color=cmap[y], width=1.5),
                              showlegend=False, hoverinfo="skip"))
            except Exception:                            # noqa: BLE001
                pass
    fig.update_layout(barmode="group", bargap=0.1, xaxis_title="DBH (cm)", yaxis_title="Tree count")
    _layout(fig, style, 440, f"DBH distribution per observation year within CPs at {_station(bif)}",
            f"for the most common species: {dom}" if dom else None)
    return Panel(None, fig, None, None)


def _biomass_map(bif, style):
    """Spatial variation in standing biomass: per-plot biomass (tons/ha) interpolated
    across the plot network for the labelling campaign — R block 6."""
    d = _tree_site(bif)
    need = {"TREE_PLOT", "Biomass_KG", "TREE_LOCATION_CALC_LAT", "TREE_LOCATION_CALC_LONG", "Year"}
    if d is None or not need <= set(d.columns):
        return _stub("Spatial variation in biomass", _NO_TREE_DB)
    d = d.assign(_b=num(d["Biomass_KG"]), _lat=num(d["TREE_LOCATION_CALC_LAT"]),
                 _lon=num(d["TREE_LOCATION_CALC_LONG"]))
    agg = (d.groupby(["TREE_PLOT", "Year"])
             .agg(b=("_b", "sum"), lat=("_lat", "mean"), lon=("_lon", "mean")).reset_index())
    agg["area"] = np.where(agg["TREE_PLOT"].astype(str).str.startswith("CP"), 0.2, 0.07)
    agg["tph"] = (agg["b"] / 1000) / agg["area"]
    counts = agg.dropna(subset=["lat", "lon", "tph"]).groupby("Year")["TREE_PLOT"].nunique()
    counts = counts[counts >= 4]                          # labelling campaign = earliest full survey
    yr = min(counts.index) if len(counts) else agg["Year"].min()
    a = agg[agg["Year"] == yr].dropna(subset=["lat", "lon", "tph"])
    a = a[(a["lat"] != 0) & (a["lon"] != 0)]
    if len(a) < 4:
        return _stub("Spatial variation in biomass", _NO_TREE_DB)
    try:
        from scipy.interpolate import griddata
        xi = np.linspace(a["lon"].min(), a["lon"].max(), 120)
        yi = np.linspace(a["lat"].min(), a["lat"].max(), 120)
        zi = griddata((a["lon"], a["lat"]), a["tph"], tuple(np.meshgrid(xi, yi)), method="linear")
        fig = go.Figure(go.Contour(x=xi, y=yi, z=zi, colorscale="Viridis",
                        colorbar=dict(title="Biomass<br>(tons/ha)"), connectgaps=False))
    except Exception:                                    # scipy missing -> scatter only
        fig = go.Figure()
    fig.add_trace(go.Scatter(x=a["lon"], y=a["lat"], mode="markers+text", text=a["TREE_PLOT"],
                  textposition="top center", textfont=dict(size=9),
                  marker=dict(color="red", size=9, symbol="square", line=dict(color="white", width=1)),
                  name="plot", showlegend=False))
    # x/y are lon/lat in degrees; preserve the true km aspect ratio. 1° lat ≈ 110.57 km,
    # 1° lon ≈ 111.32·cos(lat) km. scaleanchor keeps km/px equal on both axes (exact at any
    # render width); the height is chosen from the km extents so there's little letterboxing.
    kmlat, kmlon = 110.574, 111.32 * math.cos(math.radians(float(a["lat"].mean())))
    dlon = (float(a["lon"].max() - a["lon"].min()) or 1e-6) * kmlon
    dlat = (float(a["lat"].max() - a["lat"].min()) or 1e-6) * kmlat
    fig.update_yaxes(scaleanchor="x", scaleratio=kmlat / kmlon)
    fig.update_layout(xaxis_title="Longitude", yaxis_title="Latitude")
    height = int(min(760, max(320, 760 * dlat / dlon)))   # ~760px plot width inside the page
    _layout(fig, style, height, f"Spatial variation in biomass (tons/ha) at {_station(bif)}",
            f"labelling campaign {yr}")
    tab = (a[["TREE_PLOT", "tph"]].set_index("TREE_PLOT")
            .rename(columns={"tph": "biomass [tons/ha]"}).sort_values("biomass [tons/ha]", ascending=False))
    return Panel(None, fig, tab, None)


def heightc(bif, ctx, style):
    return _tree_metric(bif, ctx, style, "GRP_HEIGHTC", approach_re=r"^average",
                        ylab="mean ± SD height (m)",
                        title="Average tree height per species and plot/campaign", subtitle=_SPP5)


# ---------------- species composition (stacked) ----------------

def spp_o(bif, ctx, style):
    w = bp.group_wide(bif, "GRP_SPP_O")
    if not {"SPP_O", "SPP_O_SPP", "SPP_O_STATISTIC"} <= set(w.columns):
        return None
    w = _campaign(w, "SPP_O")
    d = w[(w["SPP_O_STATISTIC"] != "Standard Deviation")
          & ~w["SPP_O_SPP"].isin(["Dead Standing Trees", ".Total"])].copy()
    d["_v"] = num(d["SPP_O"])
    d = d.dropna(subset=["_v"])
    if d.empty:
        return None
    g = d.groupby(["Plot_Year", "SPP_O_SPP"])["_v"].mean().reset_index()
    # normalise each campaign to 100% (per-species means needn't sum to exactly 100;
    # e.g. SP_2020 sums to ~122.7% -> divide that campaign's bars by 1.22675)
    totals = g.groupby("Plot_Year")["_v"].transform("sum")
    g["_v"] = np.where(totals > 0, g["_v"] / totals * 100, g["_v"])
    # stack order: largest species at the bottom
    order = g.groupby("SPP_O_SPP")["_v"].mean().sort_values(ascending=False).index
    cmap = _cmap(order, LONG_SEQ)
    fig = go.Figure()
    for sp in order:
        dd = g[g["SPP_O_SPP"] == sp]
        fig.add_bar(x=dd["Plot_Year"], y=dd["_v"], name=str(sp),
                    marker=dict(color=cmap[str(sp)], line=dict(color="black", width=0.5)),
                    text=[f"{v:.1f}" if v > 5 else "" for v in dd["_v"]],
                    textposition="inside", textfont=dict(size=9))
    fig.update_layout(barmode="stack", xaxis_title="",
                      yaxis_title="mean tree species percentage (%)",
                      legend_title_text="Species")
    _layout(fig, style, 480, f"Average tree species ratio per plot/campaign at {_station(bif)}")
    tab = g.pivot_table(index="SPP_O_SPP", columns="Plot_Year", values="_v").rename_axis("species")
    return [Panel(None, fig, tab, None)]


# ---------------- LAI (time series) ----------------

def lai(bif, ctx, style):
    w = bp.group_wide(bif, "GRP_LAI")
    if "LAI" not in w.columns or "LAI_STATISTIC" not in w.columns:
        return None
    if "LAI_APPROACH" in w.columns:                      # exclude SP-I DHPs (R filter)
        w = w[~w["LAI_APPROACH"].astype(str).str.contains("sparse plot", case=False, na=False)]
    w = w.copy()
    # DATE2 = coalesce(LAI_DATE, LAI_DATE_START): many records are dated only by
    # DATE_START, so keying off LAI_DATE alone silently drops ~40% of the points.
    date = w["LAI_DATE"] if "LAI_DATE" in w.columns else pd.Series(pd.NA, index=w.index)
    ds = w["LAI_DATE_START"] if "LAI_DATE_START" in w.columns else pd.Series(pd.NA, index=w.index)
    d2 = date.where(date.notna() & (date.astype(str) != ""), ds)
    w["_d"] = pd.to_datetime(d2, format="%Y%m%d", errors="coerce")
    w["_v"] = num(w["LAI"])
    g = _mean_sd(w.dropna(subset=["_d"]), "LAI", keys=["_d"])
    if g is None or g.empty:
        return None
    g = g.sort_values("_d")
    fig = go.Figure(go.Scatter(
        x=g["_d"], y=g["mean"], mode="markers", marker=dict(color=style["line"], size=8),
        error_y=_err(g["sd"]) if g["sd"].notna().any() else None, name="LAI"))
    fig.update_layout(xaxis_title="Date", yaxis_title="LAI ± SD (m²/m²)")
    fig.update_yaxes(rangemode="tozero")
    _layout(fig, style, 420, f"Seasonal variation in LAI at {_station(bif)}")
    tab = g.rename(columns={"_d": "date"}).set_index("date")[["mean", "sd"]]
    return [Panel(None, fig, tab, None)]


# ---------------- litter ----------------

def litter(bif, ctx, style):
    w = bp.group_wide(bif, "GRP_LITTER")
    if "LITTER" not in w.columns or "LITTER_ORGAN" not in w.columns or "LITTER_STATISTIC" not in w.columns:
        return None
    site = _station(bif)
    panels = []
    annual = _litter_annual(w, style, site)
    if annual:
        panels += annual
    quarterly = _litter_quarterly(w, style, site)
    if quarterly:
        panels += quarterly
    return panels or None


def _litter_annual(w, style, site):
    organ_map = {"Total": "NonWoody", "Branches": "FineWoody", "Trunks": "CoarseWoody"}
    d = w[w["LITTER_ORGAN"].isin(organ_map) & w["LITTER_STATISTIC"].isin(["Mean", "Standard Deviation"])].copy()
    if d.empty:
        return None
    # Year = coalesce(LITTER_DATE, LITTER_DATE_START): the NonWoody (Total) rows are
    # dated only by DATE_START, so keying off LITTER_DATE alone drops them entirely.
    date = d["LITTER_DATE"] if "LITTER_DATE" in d.columns else pd.Series(pd.NA, index=d.index)
    ds = d["LITTER_DATE_START"] if "LITTER_DATE_START" in d.columns else pd.Series(pd.NA, index=d.index)
    yr = date.where(date.notna() & (date.astype(str) != ""), ds)
    d["Year"] = yr.astype(str).str.slice(0, 4)
    d["_type"] = d["LITTER_ORGAN"].map(organ_map)
    d["_v"] = num(d["LITTER"])
    g = (d.groupby(["Year", "_type", "LITTER_STATISTIC"])["_v"].sum()
           .unstack("LITTER_STATISTIC").reset_index())
    g.columns.name = None
    g = g.rename(columns={"Mean": "mean", "Standard Deviation": "sd"})
    if "mean" not in g.columns:
        return None
    if "sd" not in g.columns:
        g["sd"] = np.nan
    g = g[g["Year"].notna() & (g["Year"] != "")]
    # R color_override: the first year's fine-woody bar is the standing POOL (stock),
    # later years are the annual flux -> split FineWoody into FineWoodyPool (year 0) +
    # FineWoody, so the right panel carries three series, not two.
    minyear = g["Year"].min()
    g["_grp"] = np.where((g["_type"] == "FineWoody") & (g["Year"] == minyear),
                         "FineWoodyPool", g["_type"])
    # two panels: coarse woody | fine + non-woody (R splits these for scale)
    fig = make_subplots(rows=1, cols=2, subplot_titles=("Coarse woody", "Fine & non-woody"))
    colors = {"CoarseWoody": "#BEAED4", "FineWoodyPool": "#BF5B17",
              "FineWoody": "#FDC086", "NonWoody": "#7FC97F"}

    def _bar(t, col, offset):
        dd = g[g["_grp"] == t]
        if dd.empty:
            return
        fig.add_bar(x=dd["Year"], y=dd["mean"], name=t, legendgroup=t, offsetgroup=offset,
                    marker=dict(color=colors[t], line=dict(color="black", width=1)),
                    error_y=_err(dd["sd"]) if dd["sd"].notna().any() else None, row=1, col=col)

    _bar("CoarseWoody", 1, "coarse")
    _bar("FineWoodyPool", 2, "fine")     # FineWoodyPool & FineWoody share a slot (never same year)
    _bar("FineWoody", 2, "fine")
    _bar("NonWoody", 2, "nonwoody")
    fig.update_layout(barmode="group")
    fig.update_yaxes(title_text="kg/m²", row=1, col=1)
    fig.update_yaxes(title_text="kg/m²/year", row=1, col=2)
    _layout(fig, style, 440, f"Mean (± SD) annual variation in dry litter mass at {site}",
            "(n = number of CPs)")
    tab = g.pivot_table(index="Year", columns="_type", values="mean")
    return [Panel(None, fig, tab, None)]


def _litter_quarterly(w, style, site):
    if "LITTER_APPROACH" not in w.columns or "LITTER_DATE" not in w.columns:
        return None
    d = w[w["LITTER_APPROACH"].astype(str).str.startswith("Non-Woody Litter")
          & (w["LITTER_STATISTIC"] == "Mean")].copy()
    d["_d"] = pd.to_datetime(d["LITTER_DATE"], format="%Y%m%d", errors="coerce")
    d = d.dropna(subset=["_d"])
    if d.empty:
        return None
    d["Quarter"] = d["_d"].dt.year.astype(str) + "-Q" + d["_d"].dt.quarter.astype(str)
    d["_v"] = num(d["LITTER"])
    g = d.groupby(["Quarter", "LITTER_ORGAN"])["_v"].sum().reset_index()
    fig = go.Figure()
    cmap = _cmap(g["LITTER_ORGAN"])                       # colour fixed per litter type...
    for c in reversed(list(cmap)):                        # ...but stack/legend order reversed
        dd = g[g["LITTER_ORGAN"].astype(str) == c]
        fig.add_bar(x=dd["Quarter"], y=dd["_v"], name=c,
                    marker=dict(color=cmap[c], line=dict(color="black", width=1)))
    fig.update_layout(barmode="stack", xaxis_title="Quarter",
                      yaxis_title="summed dry litter mass (kg/m²)", legend_title_text="Litter type")
    _layout(fig, style, 420, f"Stacked non-woody litter per quarter at {site}")
    tab = g.pivot_table(index="Quarter", columns="LITTER_ORGAN", values="_v")
    return [Panel(None, fig, tab, None)]


# ---------------- soil depth profiles (Mean ± SD) ----------------

def _soil_profile(bif, style, group, measures, labels, title):
    pfx = group[4:]
    w = bp.group_wide(bif, group)
    dmin, dmax = f"{pfx}_PROFILE_MIN", f"{pfx}_PROFILE_MAX"
    if dmin not in w.columns or f"{pfx}_STATISTIC" not in w.columns and \
            not any(f"{pfx}_{m}_STATISTIC" in w.columns for m in measures):
        return None
    panels = []
    present = [m for m in measures if f"{pfx}_{m}" in w.columns]
    if not present:
        return None
    fig = make_subplots(rows=1, cols=len(present), shared_yaxes=True,
                        subplot_titles=[labels.get(m, m) for m in present])
    tabs = {}
    for i, m in enumerate(present, 1):
        var = f"{pfx}_{m}"
        stat = f"{var}_STATISTIC" if f"{var}_STATISTIC" in w.columns else f"{pfx}_STATISTIC"
        d = w[[dmin, dmax, var, stat]].copy()
        d["_v"] = num(d[var])
        d["_lo"], d["_hi"] = num(d[dmin]), num(d[dmax])
        d = d[d[stat].isin(["Mean", "Standard Deviation"])]
        g = (d.groupby(["_lo", "_hi", stat])["_v"].mean().unstack(stat).reset_index())
        g.columns.name = None
        g = g.rename(columns={"Mean": "mean", "Standard Deviation": "sd"})
        if "mean" not in g.columns:
            continue
        if "sd" not in g.columns:
            g["sd"] = np.nan
        g["_mid"] = (g["_lo"] + g["_hi"]) / 2
        g = g.dropna(subset=["mean", "_mid"]).sort_values("_mid")
        fig.add_trace(go.Scatter(
            x=g["mean"], y=g["_mid"], mode="lines+markers",
            line=dict(color=ACCENT[(i - 1) % len(ACCENT)]),
            error_x=_err(g["sd"]) if g["sd"].notna().any() else None,
            name=labels.get(m, m)), row=1, col=i)
        tabs[labels.get(m, m)] = g.set_index(g["_lo"].astype(str) + "–" + g["_hi"].astype(str))["mean"]
    fig.update_yaxes(autorange="reversed", row=1, col=1, title_text="depth [cm] (0 = surface)")
    _layout(fig, style, 460, f"{title} at {_station(bif)}").update_layout(showlegend=False)
    tab = pd.DataFrame(tabs)
    tab.index.name = "depth [cm]"
    return [Panel(None, fig, tab, None)]


def soil_chem(bif, ctx, style):
    return _soil_profile(bif, style, "GRP_SOIL_CHEM",
                         measures=["BD", "C_ORG", "N_TOT"],
                         labels={"C_ORG": "SOC content (gC/kg)", "N_TOT": "Total N (gN/kg)",
                                 "BD": "Bulk density (g/cm³)"},
                         title="Soil characteristics (mean ± SD) across soil depth")


def soil_stock(bif, ctx, style):
    pfx = "SOIL_STOCK"
    w = bp.group_wide(bif, "GRP_SOIL_STOCK")
    dmin, dmax = f"{pfx}_PROFILE_MIN", f"{pfx}_PROFILE_MAX"
    if dmin not in w.columns:
        return None
    if "SOIL_STOCK_COMMENT" in w.columns:
        w = w[w["SOIL_STOCK_COMMENT"].astype(str) != "Aggregation into O and M layers"]
    lo_all, hi_all = num(w[dmin]).min(), num(w[dmax]).max()
    is_cum = (num(w[dmin]) == lo_all) & (num(w[dmax]) == hi_all)
    panels = []
    layer = _soil_profile(bif, style, "GRP_SOIL_STOCK",
                          measures=["C_ORG", "N_TOT"],
                          labels={"C_ORG": "SOC stock (gC/m²)", "N_TOT": "Total N stock (gN/m²)"},
                          title="SOC and total N stocks (mean ± SD) across soil depth")
    if layer:
        panels += layer
    cum = _soil_stock_cumulative(w[is_cum], pfx, style, _station(bif))
    if cum:
        panels += cum
    return panels or None


def _soil_stock_cumulative(w, pfx, style, site):
    measures = {"C_ORG": "SOC stock (gC/m²)", "N_TOT": "Total N stock (gN/m²)"}
    present = [m for m in measures if f"{pfx}_{m}" in w.columns]
    if not present or w.empty:
        return None
    fig = make_subplots(rows=1, cols=len(present), subplot_titles=[measures[m] for m in present])
    rows = {}
    for i, m in enumerate(present, 1):
        var = f"{pfx}_{m}"
        stat = f"{var}_STATISTIC" if f"{var}_STATISTIC" in w.columns else f"{pfx}_STATISTIC"
        d = w[[var, stat]].copy()
        d["_v"] = num(d[var])
        d = d[d[stat].isin(["Mean", "Standard Deviation"])]
        agg = d.groupby(stat)["_v"].mean()
        mean, sd = agg.get("Mean", np.nan), agg.get("Standard Deviation", np.nan)
        if pd.isna(mean):
            continue
        fig.add_bar(x=[measures[m]], y=[mean], name=measures[m], showlegend=False,
                    marker=dict(color=ACCENT[(i - 1) % len(ACCENT)], line=dict(color="black", width=1)),
                    error_y=_err([sd]) if pd.notna(sd) else None,
                    text=[f"{mean:.0f}"], textposition="outside", row=1, col=i)
        rows[measures[m]] = {"mean": mean, "sd": sd}
    if not rows:
        return None
    _layout(fig, style, 420, f"Cumulative SOC and total N stocks (mean ± SD) at {site}")
    return [Panel(None, fig, pd.DataFrame(rows).T, None)]


# ---------------- foliar / vegetation chemistry (time series) ----------------

def _vegchem_series(df, value_cols, by="VEG_CHEM_SPP", date="VEG_CHEM_DATE",
                    stat="VEG_CHEM_STATISTIC", scale=1.0):
    """Pair Mean & SD per (date, species) for one or more value columns."""
    out = {}
    for vc in value_cols:
        if vc not in df.columns:
            continue
        d = df[[date, by, stat, vc]].copy()
        d["_v"] = num(d[vc]) * scale
        d["_d"] = pd.to_datetime(d[date], format="%Y%m%d", errors="coerce")
        d = d[d[stat].isin(["Mean", "Standard Deviation"])].dropna(subset=["_d"])
        if d.empty:
            continue
        g = d.groupby(["_d", by, stat])["_v"].mean().unstack(stat).reset_index()
        g.columns.name = None
        g = g.rename(columns={"Mean": "mean", "Standard Deviation": "sd"})
        if "mean" not in g.columns:
            continue
        if "sd" not in g.columns:
            g["sd"] = np.nan
        out[vc] = g.dropna(subset=["mean"]).sort_values("_d")
    return out


def veg_chem(bif, ctx, style):
    w = bp.group_wide(bif, "GRP_VEG_CHEM")
    if "VEG_CHEM_STATISTIC" not in w.columns or "VEG_CHEM_SPP" not in w.columns:
        return None
    site = _station(bif)
    panels = []
    panels += _vegchem_lma_cn(w, style, site) or []
    panels += _vegchem_nutrients(w, style, site) or []
    return panels or None


def _vegchem_lma_cn(w, style, site):
    series = _vegchem_series(w, ["VEG_CHEM_LMA"], scale=10.0)
    cn = None
    if {"VEG_CHEM_CONC_C", "VEG_CHEM_CONC_N"} <= set(w.columns):
        d = w[w["VEG_CHEM_STATISTIC"] == "Mean"].copy()
        d["_d"] = pd.to_datetime(d["VEG_CHEM_DATE"], format="%Y%m%d", errors="coerce")
        d["_cn"] = num(d["VEG_CHEM_CONC_C"]) / num(d["VEG_CHEM_CONC_N"])
        cn = d.dropna(subset=["_d", "_cn"]).sort_values("_d")
    if not series and (cn is None or cn.empty):
        return None
    fig = make_subplots(rows=1, cols=2, subplot_titles=("LMA ± SD (kg/m²)", "C/N ratio"))
    spp_all = set()
    if "VEG_CHEM_LMA" in series:
        spp_all |= set(series["VEG_CHEM_LMA"]["VEG_CHEM_SPP"].astype(str))
    if cn is not None:
        spp_all |= set(cn["VEG_CHEM_SPP"].astype(str))
    cmap = _cmap(sorted(spp_all), LONG_SEQ)
    if "VEG_CHEM_LMA" in series:
        for sp, g in series["VEG_CHEM_LMA"].groupby("VEG_CHEM_SPP"):
            fig.add_trace(go.Scatter(x=g["_d"], y=g["mean"], mode="lines+markers",
                          name=str(sp), legendgroup=str(sp), line=dict(color=cmap[str(sp)]),
                          error_y=_err(g["sd"]) if g["sd"].notna().any() else None), row=1, col=1)
    if cn is not None:
        for sp, g in cn.groupby("VEG_CHEM_SPP"):
            fig.add_trace(go.Scatter(x=g["_d"], y=g["_cn"], mode="lines+markers",
                          name=str(sp), legendgroup=str(sp), showlegend="VEG_CHEM_LMA" not in series,
                          line=dict(color=cmap[str(sp)])), row=1, col=2)
    _layout(fig, style, 440, f"LMA and C/N ratio at {site}")
    return [Panel(None, fig, None, None)]


_ELEMENTS = [("VEG_CHEM_CONC_C", "Carbon (C)"), ("VEG_CHEM_CONC_N", "Nitrogen (N)"),
             ("VEG_CHEM_CONC_P", "Phosphorus (P)"), ("VEG_CHEM_CONC_K", "Potassium (K)"),
             ("VEG_CHEM_CONC_FE", "Iron (Fe)"), ("VEG_CHEM_CONC_CU", "Copper (Cu)"),
             ("VEG_CHEM_CONC_MG", "Magnesium (Mg)"), ("VEG_CHEM_CONC_MN", "Manganese (Mn)"),
             ("VEG_CHEM_CONC_CA", "Calcium (Ca)"), ("VEG_CHEM_CONC_ZN", "Zinc (Zn)")]


def _vegchem_nutrients(w, style, site):
    present = [(c, lab) for c, lab in _ELEMENTS if c in w.columns and num(w[c]).notna().any()]
    if not present:
        return None
    series = _vegchem_series(w, [c for c, _ in present])
    present = [(c, lab) for c, lab in present if c in series and not series[c].empty]
    if not present:
        return None
    ncol = 2
    nrow = math.ceil(len(present) / ncol)
    fig = make_subplots(rows=nrow, cols=ncol, subplot_titles=[lab for _, lab in present],
                        vertical_spacing=0.06)
    spp_all = sorted({str(s) for c, _ in present for s in series[c]["VEG_CHEM_SPP"]})
    cmap = _cmap(spp_all, LONG_SEQ)
    for idx, (c, lab) in enumerate(present):
        r, col = idx // ncol + 1, idx % ncol + 1
        for sp, g in series[c].groupby("VEG_CHEM_SPP"):
            fig.add_trace(go.Scatter(
                x=g["_d"], y=g["mean"], mode="lines+markers", name=str(sp),
                legendgroup=str(sp), showlegend=(idx == 0), line=dict(color=cmap[str(sp)]),
                error_y=_err(g["sd"]) if g["sd"].notna().any() else None), row=r, col=col)
    _layout(fig, style, 300 * nrow + 60, f"Leaf macronutrient analysis at {site}")
    return [Panel(None, fig, None, None)]


# ---------------- crop / grassland renderers (time series, no tree data) ----------------

def _timeseries(g, *, title, ylab, style, colorname="Species", colors=None, height=440):
    """Dotted-line + marker + SD-errorbar time series; one trace per `_series`
    (the R crop AGB / height / GAI figures). `g` has columns _d, _series, mean, sd."""
    fig = go.Figure()
    cats = list(dict.fromkeys(g["_series"].astype(str)))
    cmap = colors or _cmap(cats, LONG_SEQ)
    for c in cats:
        d = g[g["_series"].astype(str) == c].sort_values("_d")
        fig.add_trace(go.Scatter(
            x=d["_d"], y=d["mean"], mode="lines+markers", name=c,
            line=dict(color=cmap.get(c, ACCENT[0]), dash="dot"),
            error_y=_err(d["sd"]) if d["sd"].notna().any() else None))
    fig.update_layout(xaxis_title="Date", yaxis_title=ylab, legend_title_text=colorname,
                      showlegend=len(cats) > 1)
    fig.update_yaxes(rangemode="tozero")
    return _layout(fig, style, height, title)


def _crop_biomass(bif, style):
    w = bp.group_wide(bif, "GRP_BIOMASS")
    if not {"BIOMASS", "BIOMASS_ORGAN", "BIOMASS_STATISTIC"} <= set(w.columns):
        return None
    d = w[w["BIOMASS_ORGAN"] == "Total AG"].copy()
    if "BIOMASS_STATISTIC_NUMBER" in d.columns:          # continuous plots (R: number < 6)
        d = d[num(d["BIOMASS_STATISTIC_NUMBER"]) < 6]
    if d.empty:
        return None
    d["_d"] = _date2(d, "BIOMASS")
    spp = d["BIOMASS_SPP"] if "BIOMASS_SPP" in d.columns else pd.Series("no_SPP", index=d.index)
    d["_series"] = spp.fillna("no_SPP").replace("", "no_SPP")
    d["_v"] = num(d["BIOMASS"])
    g = _mean_sd(d.dropna(subset=["_d"]), "BIOMASS", keys=["_d", "_series"])
    if g is None or g.empty:
        return None
    fig = _timeseries(g, title=f"Temporal variation in aboveground biomass at {_station(bif)}",
                      ylab="Biomass ± SD (kg DM/m²)", style=style)
    tab = g.pivot_table(index="_d", columns="_series", values="mean")
    tab.index.name = "date"
    return [Panel(None, fig, tab, None)]


def _crop_height(bif, style):
    w = bp.group_wide(bif, "GRP_HEIGHTC")
    if not {"HEIGHTC", "HEIGHTC_STATISTIC"} <= set(w.columns):
        return None
    d = w.copy()
    if "HEIGHTC_STATISTIC_NUMBER" in d.columns:          # continuous plots (R: number < 8)
        d = d[num(d["HEIGHTC_STATISTIC_NUMBER"]) < 8]
    d["_d"] = _date2(d, "HEIGHTC")
    d["_series"] = "Height"
    d["_v"] = num(d["HEIGHTC"])
    g = _mean_sd(d.dropna(subset=["_d"]), "HEIGHTC", keys=["_d", "_series"])
    if g is None or g.empty:
        return None
    fig = _timeseries(g, title=f"Temporal variation in vegetation height at {_station(bif)}",
                      ylab="Height ± SD (cm)", style=style, colors={"Height": "#1b9e77"})
    tab = g.set_index("_d")[["mean", "sd"]]
    tab.index.name = "date"
    return [Panel(None, fig, tab, None)]


def _crop_lai(bif, style):
    w = bp.group_wide(bif, "GRP_LAI")
    if not {"LAI", "LAI_STATISTIC"} <= set(w.columns):
        return None
    d = w.copy()
    if "LAI_STATISTIC_NUMBER" in d.columns:              # continuous plots (R: number < 8)
        d = d[num(d["LAI_STATISTIC_NUMBER"]) < 8]
    d["_d"] = _date2(d, "LAI")
    meth = d["LAI_METHOD"] if "LAI_METHOD" in d.columns else pd.Series("", index=d.index)
    d["_series"] = meth.fillna("").replace("", "Unknown method")
    d["_v"] = num(d["LAI"])
    g = _mean_sd(d.dropna(subset=["_d"]), "LAI", keys=["_d", "_series"])
    if g is None or g.empty:
        return None
    fig = _timeseries(g, title=f"Temporal variation in LAI at {_station(bif)}",
                      ylab="LAI ± SD (m²/m²)", style=style, colorname="Method")
    tab = g.pivot_table(index="_d", columns="_series", values="mean")
    tab.index.name = "date"
    return [Panel(None, fig, tab, None)]


def _icos_panel(bif, group, style):
    """Wrap ancillary_lib's single ICOS chart for a group as a classic Panel."""
    res = alib.section_for(group, bp.group_wide(bif, group), style)
    return [Panel(None, *res)] if res else None


# ---------------- stubs (figures needing external data) ----------------

def _stub(subtitle, note):
    return Panel(subtitle, None, None, note)


# ---------------- dispatch: umbrella report (mirrors the R report's TOC) ----------------

# BIF variable groups the umbrella sections consume; anything else a station reports
# (e.g. soil texture / classification) is appended afterwards via the ICOS renderer.
CONSUMED = {"GRP_BIOMASS", "GRP_BASAL_AREA", "GRP_TREES_NUM", "GRP_SPP_O", "GRP_SPP",
            "GRP_DBH", "GRP_HEIGHTC", "GRP_LAI", "GRP_LITTER", "GRP_SOIL_CHEM",
            "GRP_SOIL_STOCK", "GRP_VEG_CHEM"}


def _flatten(x):
    if x is None:
        return []
    return [x] if isinstance(x, Panel) else [p for p in x if p]


def report(bif, ctx, style):
    """Ordered umbrella Sections for the classic report, matching the R TOC.

    Forest stations (tree groups present): GRP_TREE stand/individual -> dead ->
    AGB understory -> GAI -> litter -> soil -> foliar. Crop/grassland stations
    (no tree data): GRP_SPP -> GRP_AGB (biomass time series) -> GRP_HEIGHTC ->
    GRP_GAI -> soil -> foliar."""
    out = []

    def add(title, *thunks):
        panels = []
        for t in thunks:
            try:
                panels += _flatten(t())
            except Exception as e:                       # noqa: BLE001 - never break a render
                panels.append(Panel(None, None, None,
                                    f"(classic render error: {type(e).__name__}: {e})"))
        if panels:
            out.append(Section(title, panels))

    if _is_forest(bif):
        add("GRP_TREE — data at stand level",
            lambda: _biomass_live(bif, ctx, style), lambda: basal_area(bif, ctx, style),
            lambda: trees_num(bif, ctx, style), lambda: spp_o(bif, ctx, style))
        add("GRP_TREE — data at individual level",
            lambda: _dbh_histogram(bif, ctx, style), lambda: _biomass_map(bif, style),
            lambda: dbh_bars(bif, ctx, style), lambda: heightc(bif, ctx, style))
        add("Extra — dead standing trees", lambda: _biomass_dead(bif, ctx, style))
        add("GRP_AGB — understory", lambda: _agb_understory(bif, style))
        add("GRP_GAI — leaf area index", lambda: lai(bif, ctx, style))
        add("GRP_LITTERPNT — litterfall", lambda: litter(bif, ctx, style))
    else:                                                # crop / grassland
        add("GRP_SPP — species composition", lambda: _icos_panel(bif, "GRP_SPP", style))
        add("GRP_AGB — aboveground biomass", lambda: _crop_biomass(bif, style))
        add("GRP_HEIGHTC — canopy height", lambda: _crop_height(bif, style))
        add("GRP_GAI — leaf area index", lambda: _crop_lai(bif, style))
    add("GRP_SOIL — chemistry & stocks",
        lambda: soil_chem(bif, ctx, style), lambda: soil_stock(bif, ctx, style))
    add("GRP_FLSM — foliar chemistry", lambda: veg_chem(bif, ctx, style))
    return out
