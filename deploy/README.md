# Deployment

Topology and operational notes for running the ICOS ancillary preview app in production.

## Topology

```
            https://preview.icos-cp.eu
                      │  (TLS terminated here)
            ┌─────────▼──────────┐
            │  front-end nginx   │   deploy/nginx/preview.icos-cp.eu.conf
            │  (separate host)   │
            └─────────┬──────────┘
                      │  http  →  10.10.10.153:8049
            ┌─────────▼──────────────────────────┐
            │  app VM  10.10.10.153               │
            │  docker compose → gunicorn :8050    │
            │  host port-map  8049:8050           │
            └─────────────────────────────────────┘
```

- **Front-end nginx** (separate host) terminates TLS and reverse-proxies to the app VM.
  Config: [nginx/preview.icos-cp.eu.conf](nginx/preview.icos-cp.eu.conf). It deliberately
  does **not** set `X-Frame-Options`/CSP — the app emits its own
  `frame-ancestors https://*.icos-cp.eu` so it can embed in the Carbon Portal.
- **App VM** `10.10.10.153` runs the container via `docker compose`. gunicorn listens on
  `8050` inside the container; the compose `ports` maps host `8049` → container `8050`.
  Reachable from the front-end as `10.10.10.153:8049`.

## Ports

- Compose `ports` is `HOST:CONTAINER`. gunicorn's bind is hardcoded to `8050` in the
  Dockerfile, so the container side must stay `8050` unless you also change the bind +
  `EXPOSE` and rebuild. To change only the public-facing port, edit the host side, e.g.
  `8049:8050`, then `docker compose up -d` (a `restart` will NOT pick up compose changes).
- Open `8049` on the app VM's firewall **only** to the front-end host's IP — never publicly.
  The internet reaches the app exclusively through nginx on 443.

## Credentials (icoscp_core)

`extractFile` on `data.icos-cp.eu` needs an ICOS login session unless the request originates
from inside the ICOS data-center network. This app VM is outside it, so credentials are
required (a missing token surfaces as `401 Unauthorized`).

One-time on the app VM (as root):

```bash
pip install icoscp_core
python3 -c "from icoscp_core.icos import auth; auth.init_config_file()"   # prompts email + password
```

This writes `/root/.icoscp/`. Mount it into the container (rw, so the auto-refreshed token
persists) in `docker-compose.yml`:

```yaml
    volumes:
      - ./cache:/data/cache
      - /root/.icoscp:/root/.icoscp
```

Verify the token resolves inside the container:

```bash
docker compose exec ancillary-viewer python -c "from icoscp_core.icos import auth; print(auth.get_token().cookie_value[:20])"
```

Note: each station's CSV is cached under `./cache` after first fetch, so auth is only hit on
the first request per station; a pre-warmed cache can run fully offline.

## Restart / redeploy

```bash
cd /opt/fluxnet-ancillary-preview
git pull origin main            # public repo, HTTPS, no token needed
docker compose up -d            # recreates container if image/compose changed
docker compose up -d --build    # add --build when app code or Dockerfile changed
```

## Report styles & the per-tree biomass database

The app offers two report styles (toggle in the top bar):

- **ICOS** — the native cyan / plotly_white charts (one mean per category).
- **Classic (R report)** — faithfully reproduces the ETC_PROCESSING_SUITE ggplot
  figures (campaign/year/species-resolved bars and time series with SD error bars,
  ColorBrewer "Accent" palette, black outlines).

Two Classic figures — the **DBH frequency histogram** and the **spatial biomass map** —
are not derivable from the L2 BIF product; they need the individual-tree biomass
database (`DATAFILE_Biomass_ICOS_*.xlsx`). The app finds it via, in order:

1. the `ICOS_TREE_DB` environment variable (full path), or
2. a `DATAFILE_Biomass_ICOS_*.xlsx` file in the app dir (`/app`) or the cache dir (`/data/cache`).

Simplest in production: drop the file into the mounted cache volume — no rebuild:

```bash
cp DATAFILE_Biomass_ICOS_20260310.xlsx /opt/fluxnet-ancillary-preview/cache/
docker compose restart        # picks up the new file (it's read once per process)
```

If the file is absent these two plots degrade gracefully to a short explanatory note;
all other Classic figures still render. The file is read once and cached in memory.

## TLS

The nginx config assumes Let's Encrypt certs and keeps the ACME challenge path open on :80.
If using the ICOS `*.icos-cp.eu` wildcard instead, point `ssl_certificate` /
`ssl_certificate_key` at it and drop the ACME `location` block.
