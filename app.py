"""ICOS ancillary data viewer — Dash app for portal iframe embedding.

URL parameters (either one):
    ?station=BE-Bra            station code
    ?pid=EqDNfea8CmY5ZA3...    PID (hash or full URI) of the ETC L2 ARCHIVE object

Renders every variable group reported for the station as an interactive Plotly
chart + collapsible table, and offers a one-click export to a self-contained
zipped HTML snapshot. Run:  python app.py   ->  http://localhost:8050/?station=BE-Bra
"""
import io
import urllib.parse
import zipfile

import plotly.io as pio
from dash import Dash, Input, Output, State, dcc, html, no_update

import ancillary_lib as lib

CYAN, CYAN_D = "#00abc9", "#0092b3"
app = Dash(__name__, title="ICOS ancillary viewer")
server = app.server

try:
    STATIONS = lib.available_stations()
except Exception:                       # offline / metadata down -> params still work
    STATIONS = []


@server.after_request
def allow_iframe(resp):
    # let the Carbon Portal embed this app in an iframe
    resp.headers.pop("X-Frame-Options", None)
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'self' https://*.icos-cp.eu"
    return resp


def parse_query(search):
    q = urllib.parse.parse_qs((search or "").lstrip("?"))
    return (q.get("station", [None])[0], q.get("pid", [None])[0])


def df_table(df):
    df = df.reset_index()
    return html.Table(className="dtab", children=[
        html.Thead(html.Tr([html.Th(str(c)) for c in df.columns])),
        html.Tbody([
            html.Tr([html.Td("" if lib.pd.isna(v) else
                             (f"{v:,.3g}" if isinstance(v, float) else str(v)))
                     for v in row])
            for row in df.itertuples(index=False)
        ]),
    ])


def build_sections(station, bif):
    nav, sections = [], []
    for g in lib.groups_present(bif):
        w = lib.bp.group_wide(bif, g)
        res = lib.section_for(g, w)
        if not res:
            continue
        fig, tab, desc = res
        anchor = g.lower().replace("_", "-")
        nav.append(html.A(lib.title_of(g), href=f"#{anchor}", className="btn"))
        body = [html.H2([lib.title_of(g), html.Span(f"  {g}", className="grp")]),
                html.P(desc, className="desc")]
        if fig is not None:
            body.append(dcc.Graph(figure=fig, config={"displaylogo": False}))
        body.append(html.Details([html.Summary(f"Show data ({len(w):,} records)"), df_table(tab)]))
        sections.append(html.Section(id=anchor, children=body))
    return nav, sections


app.layout = html.Div([
    dcc.Location(id="url"),
    dcc.Download(id="dl"),
    html.Div(id="page"),
])


def topbar(code):
    return html.Div(className="topbar", children=[
        html.Div([html.B(f"Ancillary data — {code}" if code else "ICOS ancillary viewer"),
                  html.Span(" · ICOS ETC L2 BIF product", className="sub")]),
        html.Div(style={"display": "flex", "gap": "10px", "alignItems": "center"}, children=[
            dcc.Dropdown(id="station-dd",
                         options=[{"label": s, "value": s} for s in STATIONS],
                         value=code, placeholder="Select a station…", clearable=False,
                         style={"width": "200px"}),
            html.Button("⬇ Export zipped HTML", id="export", className="exp",
                        disabled=code is None),
        ]),
    ])


@app.callback(Output("page", "children"), Input("url", "search"))
def render(search):
    station, pid = parse_query(search)
    body, code = [], None
    if station or pid:
        try:
            code, bif = lib.fetch_ancillary(station=station, pid=pid)
        except Exception as e:                   # noqa: BLE001 - surface any fetch/auth error
            body = [html.Div([html.H3("Could not load station data"),
                              html.Pre(f"{type(e).__name__}: {e}")], className="hint")]
    if code and not body:
        nav, sections = build_sections(code, bif)
        body = [html.Div(nav, className="nav"), html.Div(sections, className="body")]
    elif not body:
        body = [html.Div("Select a station above to view its ancillary data "
                         f"({len(STATIONS)} available).", className="hint")]
    return [topbar(code), *body,
            html.Footer("Data: ICOS ETC ancillary (L2). Please cite and acknowledge ICOS · "
                        "data.icos-cp.eu · CC-BY-4.0")]


@app.callback(Output("url", "search"), Input("station-dd", "value"),
              State("url", "search"), prevent_initial_call=True)
def pick_station(value, search):
    if not value or value == parse_query(search)[0]:   # break the url->dropdown->url loop
        return no_update
    return f"?station={value}"


@app.callback(Output("dl", "data"), Input("export", "n_clicks"),
              State("url", "search"), prevent_initial_call=True)
def export(n_clicks, search):
    # the button is re-created on every station change; a dynamically-added Input
    # fires even with prevent_initial_call, so only proceed on an actual click.
    if not n_clicks:
        return no_update
    station, pid = parse_query(search)
    code, bif = lib.fetch_ancillary(station=station, pid=pid)
    parts, first = [], True
    for g in lib.groups_present(bif):
        res = lib.section_for(g, lib.bp.group_wide(bif, g))
        if not res:
            continue
        fig, tab, desc = res
        if fig is not None:
            chart = pio.to_html(fig, full_html=False,
                                include_plotlyjs="inline" if first else False)
            first = False
        else:
            chart = ""
        parts.append(f'<section id="{g.lower().replace("_","-")}"><h2>{lib.title_of(g)} '
                     f'<span class="grp">{g}</span></h2><p class="desc">{desc}</p>'
                     f'{chart}<details><summary>Show data</summary>'
                     f'{tab.reset_index().to_html(index=False, border=0, classes="dtab")}'
                     f'</details></section>')
    html_doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Ancillary data — {code}</title><style>{CSS}</style></head><body>
<h1>Summary Ancillary Data for {code}</h1>
<p style="color:#666">ICOS ETC L2 BIF product · interactive snapshot</p>
{''.join(parts)}
<footer>Data: ICOS ETC ancillary (L2). Please cite and acknowledge ICOS · CC-BY-4.0</footer>
</body></html>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"ICOSETC_{code}_Ancillary_Report.html", html_doc)
    buf.seek(0)
    return dcc.send_bytes(buf.getvalue(), f"ICOSETC_{code}_Ancillary_Report.zip")


CSS = """
 body{font-family:Segoe UI,Arial,sans-serif;color:#222;max-width:1000px;margin:0 auto;padding:0 18px 40px;line-height:1.5}
 h1{font-weight:900} h2{border-bottom:2px solid #00abc9;padding-bottom:4px;margin-top:34px}
 .grp{font-size:.5em;color:#999;font-weight:400}
 .desc{color:#555;font-size:14px}
 .dtab{border-collapse:collapse;font-size:13px;margin-top:8px}
 .dtab th{background:#e7f6fa;text-align:left;padding:5px 9px} .dtab td{border-bottom:1px solid #eee;padding:4px 9px}
 details summary{cursor:pointer;color:#0092b3;font-weight:bold;margin:6px 0}
 footer{margin-top:40px;border-top:1px solid #eee;padding-top:12px;color:#888;font-size:12px}
"""
# inject CSS + nav/topbar styling into the Dash page shell
app.index_string = """<!doctype html><html><head>{%metas%}<title>{%title%}</title>{%favicon%}{%css%}
<style>""" + CSS + """
 .topbar{position:sticky;top:0;background:#fff;display:flex;justify-content:space-between;align-items:center;
   padding:12px 0;border-bottom:1px solid #eee;z-index:20}
 .topbar .sub{color:#888;font-size:13px}
 .exp{background:#00abc9;color:#fff;border:none;border-radius:4px;padding:8px 14px;font-weight:bold;cursor:pointer}
 .exp:hover{background:#0092b3}
 .nav{position:sticky;top:50px;background:#fff;padding:8px 0;border-bottom:1px solid #eee;z-index:10}
 .btn{background:#00abc9;color:#fff!important;padding:6px 11px;border-radius:4px;text-decoration:none;
   display:inline-block;margin:3px;font-size:13px} .btn:hover{background:#0092b3}
 .hint{padding:40px;color:#666;font-size:15px}
</style></head>
<body>{%app_entry%}<footer>{%config%}{%scripts%}{%renderer%}</footer></body></html>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8050, debug=False)
