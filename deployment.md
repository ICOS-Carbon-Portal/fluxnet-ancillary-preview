# Deployment

The app is a Dash/Flask application served by **gunicorn** in a Docker container.
The Flask server is exposed as `app:server`.

## Files

| File | Purpose |
|------|---------|
| `Dockerfile` | `python:3.12-slim`, installs `requirements.txt`, runs gunicorn (`app:server`, 2 workers) |
| `docker-compose.yml` | base service: port `8050`, mounts `./cache` → `/data/cache` |
| `docker-compose.override.yml` | **local-only** — mounts WSL `~/.icoscp` credentials into the container |
| `.dockerignore` | keeps the build context small (excludes `.venv`, parquet, images, big CSVs) |
| `requirements.txt` | pinned dependencies |

`docker compose` automatically merges `docker-compose.override.yml` when it is present, so
**local** runs get credentials while the **server** deployment (which doesn't ship the
override) stays clean.

## Configuration

| Setting | Default | Notes |
|---------|---------|-------|
| `ANCILLARY_CACHE` (env) | `/data/cache` in the image | where station CSVs + `stations.json` live; mount a volume here |
| Port | `8050` | change the `ports:` mapping in compose |
| gunicorn workers | `2` | edit the `Dockerfile` `CMD` |
| iframe hosts | `frame-ancestors 'self' https://*.icos-cp.eu` | edit `allow_iframe()` in `app.py` |

## Local test run (WSL + Docker)

```bash
# from the project directory (contains Dockerfile + cache/)
docker compose up --build -d           # build image, start container (merges the override)
docker compose ps                      # should show "Up"
curl http://localhost:8050/?station=BE-Bra   # 200; reachable from Windows too (WSL2 forwarding)

docker compose logs -f                 # tail logs
docker compose down                    # stop & remove
```

With the override in place, the container mounts `${HOME}/.icoscp` → `/root/.icoscp`, so
`icoscp_core` authenticates and **auto-refreshes the token** (the stored cookie can be
expired). Cached stations serve instantly; any other station is fetched live on first
request and then cached.

## Authentication model

- **Metadata** (SPARQL station/object lookups) needs **no** auth — the dropdown and PID/station
  resolution work everywhere.
- **Data extraction** (`/zip/<pid>/extractFile/...`) needs a session:
  - **On ICOS's own servers** the data is reachable without auth → deploy the base compose only,
    no credentials needed. `fetch_ancillary` tries a cookie and proceeds without one.
  - **Anywhere else** mount valid ICOS credentials. Locally that's the WSL override above.
    For a non-WSL/headless server, bake an `~/.icoscp/cpauthToken_auth_conf.json` (a service
    account) into the deployment instead of using the override.

## Cache / data volume

`cache/` holds `ICOSETC_<site>_ANCILLARY_L2.csv` files and `stations.json` (the dropdown list).
- Pre-seed it to make stations load instantly with no network.
- On a server, point the `cache` volume at shared/persistent storage so fetched files survive
  restarts and are shared across replicas.
- `stations.json` is generated on first metadata call if absent; delete it to refresh the list.

## Production server deployment

1. Copy the app files **without** `docker-compose.override.yml`.
2. Point the `cache` volume at shared storage (and, if the server can't reach the portal
   anonymously, mount a service-account `~/.icoscp`).
3. Put the container behind your reverse proxy / TLS; set the iframe `frame-ancestors` host list
   to the embedding origin(s).
4. `docker compose up --build -d`.

## Updating

After changing `app.py` / `ancillary_lib.py` / `bif_parser.py`:

```bash
docker compose up --build -d           # rebuild image and recreate the container
```

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Non-cached station shows "Could not load station data" | no/invalid credentials — mount `~/.icoscp` (local) or a service account (server) |
| Dropdown empty | metadata endpoint unreachable; check network, or pre-seed `cache/stations.json` |
| Token errors | the mounted `cpauthToken_auth_conf.json` must contain valid `user_id`/`password`; the token auto-refreshes |
| Build slow over `/mnt/...` | build from a native filesystem path (e.g. copy to `~/app` in WSL) |
