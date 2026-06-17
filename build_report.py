"""Build an ICOS-style HTML ancillary report from the Parquet tables.

Python imitation of the ETC_PROCESSING_SUITE R pages. Data-driven: it renders
whatever variable groups are present, so it works for forest, grassland,
cropland and wetland stations alike (a grassland simply has no tree groups).
Soil/chem groups get curated depth-profile / texture / foliar-chemistry charts;
all other groups fall back to a generic "mean by category" chart.
"""
import base64
import io
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(r"d:\agent-test")
PQ = HERE / "parquet"
SITE = "BE-Bra"
CYAN, CYAN_D, INK = "#00abc9", "#0092b3", "#222"

DIC = pd.read_parquet(PQ / "variable_dictionary.parquet").set_index("VARIABLE")

# Human titles for known groups; unknown groups get a humanized fallback,
# so non-forest sites still render their own groups without code changes.
TITLES = {
    "GRP_BIOMASS": "Biomass", "GRP_BASAL_AREA": "Basal area",
    "GRP_DBH": "Diameter at breast height", "GRP_HEIGHTC": "Tree height",
    "GRP_TREES_NUM": "Tree density", "GRP_SPP_O": "Overstory species",
    "GRP_LAI": "Leaf area index", "GRP_LITTER": "Litterfall",
    "GRP_VEG_CHEM": "Foliar / vegetation chemistry", "GRP_SOIL_CHEM": "Soil chemistry",
    "GRP_SOIL_STOCK": "Soil C/N stock", "GRP_SOIL_TEX": "Soil texture",
}
# Preferred display order; any extra groups appear after, alphabetically.
ORDER = ["GRP_SPP_O", "GRP_BIOMASS", "GRP_BASAL_AREA", "GRP_DBH", "GRP_HEIGHTC",
         "GRP_TREES_NUM", "GRP_LAI", "GRP_LITTER", "GRP_VEG_CHEM",
         "GRP_SOIL_CHEM", "GRP_SOIL_STOCK", "GRP_SOIL_TEX"]

# Curated specs for the soil/chem groups. `measures` are suffixes on the group
# prefix (e.g. SOIL_CHEM + _C_ORG). Missing measures are silently skipped.
SPECS = {
    "GRP_SOIL_CHEM":  {"kind": "profile", "measures": ["C_ORG", "N_TOT", "CN_RATIO", "BD"]},
    "GRP_SOIL_STOCK": {"kind": "profile", "measures": ["C_ORG", "N_TOT"]},
    "GRP_SOIL_TEX":   {"kind": "texture", "measures": ["CLAY", "SILT", "SAND", "ROCK"]},
    "GRP_VEG_CHEM":   {"kind": "elements",
                       "measures": ["CONC_C", "CONC_N", "CONC_P", "CONC_K", "CONC_CA", "CONC_MG"],
                       "by": "SPP"},
}
TEX_COLORS = {"CLAY": "#1f9e89", "SILT": "#fdc23e", "SAND": "#e76f51", "ROCK": "#9aa0a6"}


def title_of(group):
    return TITLES.get(group, group.replace("GRP_", "").replace("_", " ").capitalize())


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


def b64_chart(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return f'<img src="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}" style="max-width:100%;">'


def mean_rows(d, var):
    """Rows where `var` has a value and its statistic (if any) is the Mean."""
    stat = f"{var}_STATISTIC"
    s = d.copy()
    s[var] = pd.to_numeric(s[var], errors="coerce")
    if stat in s.columns:
        s = s[(s[stat] == "Mean") | s[stat].isna()]
    return s.dropna(subset=[var])


def html_table(df, floatfmt="{:,.3g}"):
    return df.to_html(border=0, classes="dtab", na_rep="",
                      float_format=lambda x: floatfmt.format(x))


# ---------------- curated renderers (soil / chem) ----------------

def render_profile(d, group, spec):
    pfx = group.replace("GRP_", "")
    dmin, dmax = f"{pfx}_PROFILE_MIN", f"{pfx}_PROFILE_MAX"
    if dmin not in d.columns:
        return None
    panels = []
    for m in spec["measures"]:
        var = f"{pfx}_{m}"
        if var not in d.columns:
            continue
        s = mean_rows(d, var)
        if s.empty:
            continue
        s = s.assign(_d=(pd.to_numeric(s[dmin], errors="coerce")
                         + pd.to_numeric(s[dmax], errors="coerce")) / 2)
        s = s.dropna(subset=["_d"]).sort_values("_d")
        if not s.empty:
            panels.append((m, var, s))
    if not panels:
        return None

    fig, axes = plt.subplots(1, len(panels), figsize=(3.1 * len(panels), 4.2), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, (m, var, s) in zip(axes, panels):
        ax.plot(s[var].values, s["_d"].values, "o-", color=CYAN)
        ax.set_xlabel(f"{m.replace('_', ' ')}\n[{unit_of(var)}]", fontsize=9)
        ax.grid(alpha=.3)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].set_ylabel("depth [cm]  (0 = mineral surface)")
    axes[0].invert_yaxis()
    fig.suptitle(f"{title_of(group)} — depth profile", color=INK, fontsize=11)

    recs = []
    for m, var, s in panels:
        for _, r in s.iterrows():
            lo = pd.to_numeric(r[dmin], errors="coerce")
            hi = pd.to_numeric(r[dmax], errors="coerce")
            recs.append((f"{lo:g} to {hi:g}", m.replace("_", " "), r[var]))
    tab = (pd.DataFrame(recs, columns=["depth [cm]", "measure", "mean"])
             .pivot_table(index="depth [cm]", columns="measure", values="mean", aggfunc="mean"))
    return b64_chart(fig), html_table(tab), f"Mean by soil depth layer ({len(panels)} measurements)."


def render_texture(d, group, spec):
    pfx = group.replace("GRP_", "")
    dmin, dmax = f"{pfx}_PROFILE_MIN", f"{pfx}_PROFILE_MAX"
    fr = [f for f in spec["measures"] if f"{pfx}_{f}" in d.columns]
    rows = {}
    for f in fr:
        s = mean_rows(d, f"{pfx}_{f}")
        if s.empty:
            continue
        g = s.assign(_lo=pd.to_numeric(s[dmin], errors="coerce"),
                     _hi=pd.to_numeric(s[dmax], errors="coerce")) \
             .groupby(["_lo", "_hi"])[f"{pfx}_{f}"].mean()
        rows[f.title()] = g
    if not rows:
        return None
    mat = pd.DataFrame(rows).sort_index()
    labels = [f"{lo:g}–{hi:g}" for lo, hi in mat.index]

    fig, ax = plt.subplots(figsize=(8, max(2.4, .55 * len(mat))))
    left = np.zeros(len(mat))
    for f in mat.columns:
        vals = mat[f].fillna(0).values
        ax.barh(labels, vals, left=left, label=f, color=TEX_COLORS.get(f.upper(), "#888"))
        left += vals
    ax.set_xlabel("% by mass")
    ax.set_ylabel("depth layer [cm]")
    ax.invert_yaxis()
    ax.legend(ncol=len(mat.columns), fontsize=9, loc="lower right")
    ax.set_title(f"{title_of(group)} fractions by depth", color=INK, fontsize=11)
    tab = mat.copy()
    tab.index = labels
    tab.index.name = "depth [cm]"
    return b64_chart(fig), html_table(tab), "Particle-size fractions per soil depth layer."


def render_elements(d, group, spec):
    pfx = group.replace("GRP_", "")
    by = f"{pfx}_{spec['by']}" if f"{pfx}_{spec['by']}" in d.columns else None
    measures = [(m, f"{pfx}_{m}") for m in spec["measures"] if f"{pfx}_{m}" in d.columns]
    measures = [(m, v) for m, v in measures if not mean_rows(d, v).empty]
    if not measures:
        return None

    n = len(measures)
    ncol = min(3, n)
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(3.4 * ncol, 2.6 * nrow), squeeze=False)
    tab_rows = {}
    for ax, (m, var) in zip(axes.flat, measures):
        s = mean_rows(d, var)
        if by and s[by].notna().any():
            agg = s.groupby(by)[var].mean().sort_values()
        else:
            agg = pd.Series({"all": s[var].mean()})
        ax.barh(agg.index.astype(str), agg.values, color=CYAN)
        ax.set_title(f"{m.replace('CONC_', '')}  [{unit_of(var)}]", fontsize=9)
        ax.spines[["top", "right"]].set_visible(False)
        tab_rows[m.replace("CONC_", "")] = agg
    for ax in axes.flat[n:]:
        ax.set_visible(False)
    fig.suptitle(f"{title_of(group)} — mean concentration"
                 + (f" by {spec['by'].lower()}" if by else ""), color=INK, fontsize=11)
    tab = pd.DataFrame(tab_rows)
    return b64_chart(fig), html_table(tab), "Mean elemental concentrations."


CURATED = {"profile": render_profile, "texture": render_texture, "elements": render_elements}


# ---------------- generic renderer (everything else) ----------------

def primary_var(w, group):
    base = group.replace("GRP_", "")
    skip = ("_STATISTIC_NUMBER", "_DATE", "_DATE_START", "_DATE_END", "_UNC", "_LAT", "_LONG")
    for cand in (base, base + "_PERC"):
        if cand in w.columns and pd.to_numeric(w[cand], errors="coerce").notna().any():
            return cand
    best, best_n = None, 0
    for c in w.columns:
        if c in ("SITE_ID", "GROUP_ID", "YEAR") or any(c.endswith(s) for s in skip):
            continue
        n = pd.to_numeric(w[c], errors="coerce").notna().sum()
        if n > best_n:
            best, best_n = c, n
    return best


def category_col(w):
    for suff in ("_SPP", "_ORGAN", "_LIFESTAGE", "_TYPE"):
        hit = next((c for c in w.columns if c.endswith(suff)), None)
        if hit and w[hit].notna().any() and w[hit].nunique() > 1:
            return hit
    return "YEAR" if "YEAR" in w.columns else None


def render_generic(w, group):
    pv = primary_var(w, group)
    if pv is None:
        return None
    w = w.copy()
    w[pv] = pd.to_numeric(w[pv], errors="coerce")
    stat = next((c for c in w.columns if c.endswith("_STATISTIC")), None)
    d = w[w[stat] == "Mean"].copy() if stat else w.copy()
    if d[pv].notna().sum() == 0:
        d = w.copy()

    cat = category_col(d)
    unit = unit_of(pv)
    if not unit or "specified in" in unit.lower():
        ucol = pv + "_UNIT"
        if ucol in d.columns and d[ucol].notna().any():
            unit = str(d[ucol].dropna().mode().iat[0])

    img = ""
    if cat and d[cat].notna().any():
        agg = d.dropna(subset=[pv]).groupby(cat)[pv].mean().sort_values().tail(12)
        if len(agg):
            fig, ax = plt.subplots(figsize=(8, max(2.2, 0.42 * len(agg))))
            ax.barh(agg.index.astype(str), agg.values, color=CYAN)
            ax.set_xlabel(f"{pv}" + (f"  [{unit}]" if unit else ""))
            ax.set_title(f"Mean {title_of(group).lower()} by {cat.lower().replace('_', ' ')}",
                         color=INK, fontsize=11)
            ax.spines[["top", "right"]].set_visible(False)
            img = b64_chart(fig)

    show = list(dict.fromkeys(c for c in (cat, pv, "YEAR") if c and c in d.columns))
    tbl = (d[show].dropna(subset=[pv]).sort_values(pv, ascending=False).head(15)
             .rename(columns={pv: pv + (f" [{unit}]" if unit else "")}))
    return img, html_table(tbl.reset_index(drop=True).set_index(show[0]) if show else tbl), desc_of(pv)


# ---------------- assembly ----------------

def render_group(group):
    w = pd.read_parquet(PQ / "groups" / f"{group}.parquet")
    spec = SPECS.get(group)
    out = None
    if spec:
        out = CURATED[spec["kind"]](w, group, spec)
    if out is None:                       # no spec, or curated produced nothing -> generic
        out = render_generic(w, group)
    if out is None:
        return ""
    img, table, desc = out
    anchor = group.lower().replace("_", "-")
    return f"""
    <section id="{anchor}">
      <h2>{title_of(group)} <span class="grp">{group}</span></h2>
      <p class="desc">{desc}</p>
      <div class="chart">{img}</div>
      <details><summary>Show data ({len(w):,} records)</summary>{table}</details>
    </section>"""


def build():
    present = sorted(p.stem for p in (PQ / "groups").glob("*.parquet"))
    groups = [g for g in ORDER if g in present] + [g for g in present if g not in ORDER]
    nav = "\n".join(f'<a href="#{g.lower().replace("_","-")}" class="btn">{title_of(g)}</a>'
                    for g in groups)
    body = "\n".join(render_group(g) for g in groups)
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Ancillary data — {SITE}</title>
<style>
 body{{font-family:Segoe UI,Arial,sans-serif;color:{INK};max-width:980px;margin:0 auto;padding:24px;line-height:1.5}}
 h1{{font-weight:900}} h2{{border-bottom:2px solid {CYAN};padding-bottom:4px;margin-top:38px}}
 .grp{{font-size:.5em;color:#888;font-weight:400}}
 a{{color:{CYAN};font-weight:bold;text-decoration:underline}}
 .nav{{position:sticky;top:0;background:#fff;padding:10px 0;border-bottom:1px solid #eee;z-index:9}}
 .btn{{background:{CYAN};color:#fff!important;padding:7px 12px;border-radius:4px;text-decoration:none;display:inline-block;margin:3px;font-size:13px}}
 .btn:hover{{background:{CYAN_D}}}
 .desc{{color:#555;font-size:14px}}
 .chart{{margin:14px 0}}
 details{{margin:8px 0 4px}} summary{{cursor:pointer;color:{CYAN_D};font-weight:bold}}
 table.dtab{{border-collapse:collapse;font-size:13px;margin-top:8px}}
 table.dtab th{{background:#e7f6fa;text-align:left;padding:5px 9px}}
 table.dtab td{{border-bottom:1px solid #eee;padding:4px 9px}}
 .flow{{background:#f3fbfd;border-left:4px solid {CYAN};padding:10px 16px;font-size:13px;border-radius:4px}}
 footer{{margin-top:48px;border-top:1px solid #eee;padding-top:12px;color:#888;font-size:12px}}
</style></head><body>
<h1>Summary Ancillary Data for {SITE}</h1>
<p style="color:#666">ICOS Ecosystem station · generated from the L2 BIF ancillary product · Python port of the ETC processing suite</p>
<div class="nav">{nav}</div>
<div class="flow">
 <b>Data flow.</b> Source: <code>ICOSETC_{SITE}_ANCILLARY_L2.csv</code> from the station's
 ETC L2 ARCHIVE, pulled via <code>icoscp_core</code> and the Carbon Portal
 <code>extractFile</code> endpoint. The BADM long format was pivoted per variable group
 into tidy Parquet tables and aggregated here to per-group means. Only the variable
 groups actually reported for this station are shown. Units and descriptions come from
 <code>BIF_Ancillary_Variables.csv</code>.
</div>
{body}
<footer>
 Data: ICOS ETC — Surface ancillary data, station {SITE} (L2). Please cite and acknowledge ICOS.<br>
 Carbon Portal: https://data.icos-cp.eu · License CC-BY-4.0.
</footer>
</body></html>"""
    out = HERE / f"{SITE}_Ancillary_Report.html"
    out.write_text(html, encoding="utf-8")
    print("WROTE", out, f"({len(html):,} bytes, {len(groups)} groups: {', '.join(groups)})")


if __name__ == "__main__":
    build()
