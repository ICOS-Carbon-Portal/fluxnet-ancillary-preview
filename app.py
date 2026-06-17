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
import classic_lib

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


def section_specs(bif, style, style_name):
    """Ordered list of classic_lib.Section(title, panels) for the page & export.

    Classic mode builds the faithful umbrella report (R TOC order); any variable
    group not consumed there — and every group in ICOS mode — is appended via
    ancillary_lib's single simplified chart."""
    Panel, Section = classic_lib.Panel, classic_lib.Section
    if style_name == "classic":
        specs = classic_lib.report(bif, classic_lib.build_context(bif), style)
        consumed = classic_lib.CONSUMED
    else:
        specs, consumed = [], set()
    for g in lib.groups_present(bif):
        if g in consumed:
            continue
        res = lib.section_for(g, lib.bp.group_wide(bif, g), style)
        if res:
            specs.append(Section(lib.title_of(g), [Panel(None, *res)]))
    return specs


def build_sections(bif, style, style_name):
    nav, sections = [], []
    for i, sec in enumerate(section_specs(bif, style, style_name)):
        anchor = f"sec{i}"
        nav.append(html.A(sec.title, href=f"#{anchor}", className="btn"))
        body = [html.H2(sec.title)]
        for p in sec.panels:
            if p.subtitle:
                body.append(html.H3(p.subtitle, className="sub-h"))
            if p.desc:
                body.append(html.P(p.desc, className="desc"))
            if p.fig is not None:
                body.append(dcc.Graph(figure=p.fig, config={"displaylogo": False}))
            if p.table is not None:
                body.append(html.Details([html.Summary(f"Show data ({len(p.table):,} rows)"),
                                          df_table(p.table)]))
        sections.append(html.Section(id=anchor, children=body))
    return nav, sections


app.layout = html.Div([
    dcc.Location(id="url"),
    dcc.Download(id="dl"),
    dcc.Store(id="style-store", data=lib.DEFAULT_STYLE),
    html.Div(id="page"),
])


def topbar(code, style):
    return html.Div(className="topbar", children=[
        html.Div([html.B(f"Ancillary data — {code}" if code else "ICOS ancillary viewer"),
                  html.Span(" · ICOS ETC L2 BIF product", className="sub")]),
        html.Div(style={"display": "flex", "gap": "12px", "alignItems": "center"}, children=[
            dcc.RadioItems(id="style-sel", className="style-sel", value=style,
                           options=[{"label": s["label"], "value": k}
                                    for k, s in lib.STYLES.items()],
                           inline=True),
            dcc.Dropdown(id="station-dd",
                         options=[{"label": s, "value": s} for s in STATIONS],
                         value=code, placeholder="Select a station…", clearable=False,
                         style={"width": "200px"}),
            html.Button("⬇ Export zipped HTML", id="export", className="exp",
                        disabled=code is None),
        ]),
    ])


@app.callback(Output("page", "children"),
              Input("url", "search"), Input("style-store", "data"))
def render(search, style_name):
    style = lib.get_style(style_name)
    station, pid = parse_query(search)
    body, code = [], None
    if station or pid:
        try:
            code, bif = lib.fetch_ancillary(station=station, pid=pid)
        except Exception as e:                   # noqa: BLE001 - surface any fetch/auth error
            body = [html.Div([html.H3("Could not load station data"),
                              html.Pre(f"{type(e).__name__}: {e}")], className="hint")]
    if code and not body:
        nav, sections = build_sections(bif, style, style_name or lib.DEFAULT_STYLE)
        body = [html.Div(nav, className="nav"), html.Div(sections, className="body")]
    elif not body:
        body = [html.Div("Select a station above to view its ancillary data "
                         f"({len(STATIONS)} available).", className="hint")]
    return html.Div(className=f"theme-{style_name or lib.DEFAULT_STYLE}", children=[
        topbar(code, style_name or lib.DEFAULT_STYLE), *body,
        html.Footer("Data: ICOS ETC ancillary (L2). Please cite and acknowledge ICOS · "
                    "data.icos-cp.eu · CC-BY-4.0")])


@app.callback(Output("style-store", "data"), Input("style-sel", "value"),
              State("style-store", "data"), prevent_initial_call=True)
def pick_style(value, current):
    # style-sel is re-created on every render; ignore the echo so we don't re-render twice
    if not value or value == current:
        return no_update
    return value


@app.callback(Output("url", "search"), Input("station-dd", "value"),
              State("url", "search"), prevent_initial_call=True)
def pick_station(value, search):
    if not value or value == parse_query(search)[0]:   # break the url->dropdown->url loop
        return no_update
    return f"?station={value}"


@app.callback(Output("dl", "data"), Input("export", "n_clicks"),
              State("url", "search"), State("style-store", "data"), prevent_initial_call=True)
def export(n_clicks, search, style_name):
    # the button is re-created on every station change; a dynamically-added Input
    # fires even with prevent_initial_call, so only proceed on an actual click.
    if not n_clicks:
        return no_update
    style = lib.get_style(style_name)
    style_name = style_name or lib.DEFAULT_STYLE
    station, pid = parse_query(search)
    code, bif = lib.fetch_ancillary(station=station, pid=pid)
    parts, first = [], True
    for i, sec in enumerate(section_specs(bif, style, style_name)):
        blocks = [f'<h2>{sec.title}</h2>']
        for p in sec.panels:
            if p.subtitle:
                blocks.append(f'<h3 class="sub-h">{p.subtitle}</h3>')
            if p.desc:
                blocks.append(f'<p class="desc">{p.desc}</p>')
            if p.fig is not None:
                blocks.append(pio.to_html(p.fig, full_html=False,
                                          include_plotlyjs="inline" if first else False))
                first = False
            if p.table is not None:
                blocks.append('<details><summary>Show data</summary>'
                              f'{p.table.reset_index().to_html(index=False, border=0, classes="dtab")}'
                              '</details>')
        parts.append(f'<section id="sec{i}">{"".join(blocks)}</section>')
    html_doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Ancillary data — {code}</title><style>{CSS}</style></head>
<body class="theme-{style_name or lib.DEFAULT_STYLE}">
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
 :root{--accent:#00abc9;--accent-d:#0092b3;--th:#e7f6fa;--font:'Segoe UI',Arial,sans-serif}
 .theme-classic{--accent:#386cb0;--accent-d:#2c5690;--th:#eef2f8;--font:'Helvetica Neue',Helvetica,Arial,sans-serif}
 body,.theme-icos,.theme-classic{font-family:var(--font)}
 body{color:#222;max-width:1000px;margin:0 auto;padding:0 18px 40px;line-height:1.5}
 h1{font-weight:900} h2{border-bottom:2px solid var(--accent);padding-bottom:4px;margin-top:34px}
 .grp{font-size:.5em;color:#999;font-weight:400}
 .sub-h{margin:22px 0 2px;color:#333;font-size:16px;font-weight:700}
 .desc{color:#555;font-size:14px}
 .dtab{border-collapse:collapse;font-size:13px;margin-top:8px}
 .dtab th{background:var(--th);text-align:left;padding:5px 9px} .dtab td{border-bottom:1px solid #eee;padding:4px 9px}
 details summary{cursor:pointer;color:var(--accent-d);font-weight:bold;margin:6px 0}
 footer{margin-top:40px;border-top:1px solid #eee;padding-top:12px;color:#888;font-size:12px}
"""
# inject CSS + nav/topbar styling into the Dash page shell
app.index_string = """<!doctype html><html><head>{%metas%}<title>{%title%}</title>{%favicon%}{%css%}
<style>""" + CSS + """
 .topbar{position:sticky;top:0;background:#fff;display:flex;justify-content:space-between;align-items:center;
   padding:12px 0;border-bottom:1px solid #eee;z-index:20}
 .topbar .sub{color:#888;font-size:13px}
 .style-sel{display:flex;gap:10px;align-items:center;font-size:13px;color:#555}
 .style-sel label{display:flex;gap:4px;align-items:center;cursor:pointer}
 .exp{background:var(--accent);color:#fff;border:none;border-radius:4px;padding:8px 14px;font-weight:bold;cursor:pointer}
 .exp:hover{background:var(--accent-d)}
 .nav{position:sticky;top:50px;background:#fff;padding:8px 0;border-bottom:1px solid #eee;z-index:10}
 .btn{background:var(--accent);color:#fff!important;padding:6px 11px;border-radius:4px;text-decoration:none;
   display:inline-block;margin:3px;font-size:13px} .btn:hover{background:var(--accent-d)}
 .hint{padding:40px;color:#666;font-size:15px}
</style></head>
<body>{%app_entry%}<footer>{%config%}{%scripts%}{%renderer%}</footer></body></html>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8050, debug=False)
