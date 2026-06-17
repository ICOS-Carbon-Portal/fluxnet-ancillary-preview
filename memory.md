# Project memory — ICOS Ancillary Data Viewer

Context and hard-won details for anyone (human or agent) picking this up later.
Date of last update: 2026-06-17.

## Goal

Reimplement the R `ETC_PROCESSING_SUITE` (per-site HTML reports of ICOS ancillary/BADM
data) in Python, as **one parametrized Dash app** (station/PID → report) that can be
embedded in the Carbon Portal, with a zipped-HTML export. Must work across **all
ecosystem types**, not just forests.

## Data source & access

- Ancillary values live inside each station's **ETC L2 ARCHIVE** zip
  (`datatype = etcArchiveProduct`, "ETC L2 ARCHIVE"). 93 stations have one.
- Pull a single file from the zip without downloading it all via the Carbon Portal
  endpoint: `https://data.icos-cp.eu/zip/<PID-hash>/extractFile/<filename>`.
- The data file is `ICOSETC_<site>_ANCILLARY_L2.csv`; the **dictionary** is the
  standardized `BIF_Ancillary_Variables.csv` (same in every archive — bundle it once).
- There is also a separate raw-BIF datatype `etcAncillaryRawBif`, but only **one** object
  exists portal-wide (BE-Bra), so it's not the route to use. Use the L2 archive.
- **Auth**: metadata/SPARQL needs none; `extractFile` needs a login session. On ICOS's own
  servers the data is reachable without auth. Locally, `icoscp_core`'s `auth.get_token()`
  reads `~/.icoscp/cpauthToken_auth_conf.json` and **auto-refreshes** an expired token
  (the stored `cookie_value` is often expired; the stored `user_id`/`password` refresh it).

## BADM "BIF" format

Long table: `SITE_ID, GROUP_ID, VARIABLE_GROUP, VARIABLE, DATAVALUE`. One pivot per
`VARIABLE_GROUP` (index `GROUP_ID`, columns `VARIABLE`) gives a clean wide table — this
is the BIFTAB the R suite produced via a manual Colab step. Each measurement row carries
its own `<VAR>_STATISTIC` (Mean / Standard Deviation / Min / Max / percentiles /
"Single observation") and depth via `<GRP>_PROFILE_MIN/MAX`.

## Data quirks discovered (important)

- **Encoding**: ancillary CSVs contain Latin-1 bytes (e.g. `°`). Read with cp1252/latin-1
  fallback (`bif_parser._read_csv`).
- **Do NOT numeric-coerce a group's base column** in the pivot — it silently turns
  categorical values (e.g. `SOIL_WRB_GROUP = "Gleysol"`) into NaN. Keep values as strings;
  coerce locally in figure code. (This was a real bug; fixed.)
- **Each measurement is in its own row** — when reading soil chem/texture, filter
  per-variable on its own `_STATISTIC`, don't expect one row to hold all measures.
- **Schema is not uniform across sites**:
  - Species cover: most sites `SPP_O` = species name + `SPP_O_PERC` = cover; **BE-Bra**
    `GRP_SPP_O` stores cover in numeric `SPP_O` and the species name in `SPP_O_SPP`.
    `_cover` auto-detects which is which.
  - Croplands record one species/season as `STATISTIC = "Single observation"` (not "Mean")
    — the cover renderer must keep both.
- **Group sets differ by ecosystem**: forests have `GRP_DBH/BASAL_AREA/TREES_NUM/SPP_O`;
  grass/crop/wetland have `GRP_SPP` and sometimes `GRP_SOIL_DEPTH/CLASSIFICATION/WRB_GROUP`.
  `GRP_HEIGHTC` is **canopy** height (renamed from "Tree height" — it's not tree-specific).
- Depth profiles use **negative = organic layers above the mineral surface**, positive below.

## Key design decisions

- **Data-driven rendering**: groups come from what the station reported (glob/`groups_present`),
  not a hardcoded forest list → non-forest sites just work.
- **Curated vs generic**: `SPECS` maps a few groups to curated renderers
  (profile/texture/elements/cover); everything else falls back to `_generic`
  (mean-by-category bar). Chart-less groups fall back to an `_info` key→value table.
- **Plotly** (interactive) chosen over the original static matplotlib; the **export** writes
  an interactive self-contained HTML (Plotly inlined once) into a zip → subsumes the static report.
- **Dropdown↔URL**: dropdown change sets `?station=`; URL drives rendering. Loop is broken by
  a guard in `pick_station` (`value == current → no_update`).
- **Export only on real click**: the Export button is re-created on each station change, and a
  dynamically-added Dash Input fires even with `prevent_initial_call`; `export()` guards on
  `if not n_clicks`. (Fixed an auto-download-on-station-change bug.)
- **Paths**: `ancillary_lib` uses `Path(__file__).parent` + `ANCILLARY_CACHE` env so it runs
  the same locally and in the container.

## Verified working

Forest **BE-Bra** (12 groups), grassland **DE-Gri** (10), cropland **BE-Lon** (8),
mire **SE-Deg** (8); plus live fetch of non-cached **NL-Loo**/**DE-Tha** in the container
using mounted credentials. Dockerized (gunicorn), reachable from Windows at
`http://localhost:8050/`. Local WSL build dir: `~/ancillary-app`.

## Open / next steps

- Add `/health` endpoint + compose `healthcheck`.
- Pre-warm the cache for all 93 stations for an instant demo.
- Consider per-group curated specs for any further non-forest groups as they appear.
- A `dcc.Loading` spinner (first load fetches+parses ~tens of thousands of rows).
- (Earlier, unrelated task in this folder: North-Atlantic fCO2 from the SOCAT zarr at
  `localhost:8077` — see `na_fco2_annual.py`; not part of this app.)
