# Weighcast

Dispatch desk for weather-ranked truck runs. Weighcast pulls Valhalla (OpenStreetMap) truck routes,
samples Open-Meteo at each checkpoint ETA, scores wind / rain / snow against load (gust-aware),
and ranks the safest corridor. Share a plan link, see when leaving later helps, and jump to the
worst stretch on the map.

## Stack

- Frontend: React 18, Vite, TypeScript, Tailwind CSS, MapLibre GL
- Backend: Django 5.2 (ASGI), Django REST Framework, gunicorn + Uvicorn workers
- APIs: Valhalla truck routing (OSM), Open-Meteo geocoding and weather
- Hosting: Vercel (frontend), Render (backend + Redis)

## How it works

```
Form → Valhalla truck routes → sample checkpoints → Open-Meteo at ETA
    → risk score + rank → map gradients, heatmap, truck marker
```

Checkpoints are placed every 10, 25, or 50 miles. Weather is interpolated to each ETA. Routes are
ranked by No Travel, then severe miles, high miles, average risk, then duration. The map paints
each alternative with a risk gradient and overlays a 0–48 hour corridor heatmap.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/RISK_RULES.md](docs/RISK_RULES.md).

## What makes Weighcast different

Not StormLane (TomTom + 3D brochure) and not Haulcast (same weather-route brief under another
name). Weighcast sells the **load**: gust-aware scoring against weight, Hold the gate leave-later
hints, shareable `?p=` plans that restore city names, worst-stretch jump, and a corridor heatmap —
asphalt/amber dispatch desk, not a navy weather portfolio.

## Project structure

```
backend/
  weighcast/
    core/       geometry, polyline, risk, heatmap
    routing/    Valhalla client
    weather/    Open-Meteo client
    api/        plan, heatmap, geocode, health
  tests/
frontend/
  src/
    pages/          landing + dispatch desk (/app)
    features/map    MapLibre layers, slider, legend
    features/plan   form, results, queries
  public/           favicon, logo, mark
docs/
```

## Setup

Requirements: Python 3.14, Node 22, and optionally Docker.

```bash
cp .env.example .env
```

### Docker

```bash
docker compose up --build
```

### Local

Backend:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn weighcast.asgi:application --reload --port 8000
```

Frontend:

```bash
cd frontend
nvm use
npm install
npm run dev
```

Landing is http://localhost:5173, the desk is http://localhost:5173/app, and the API is
http://localhost:8000. In development, Vite proxies `/api` to the backend. OpenAPI is at
http://localhost:8000/api/docs.

## Checks

Backend:

```bash
ruff check .
ruff format --check .
mypy .
pytest
```

Frontend:

```bash
npm run lint
npm run format:check
npm run typecheck
npm test
npm run build
```

## Environment variables

| Variable               | Description                                  |
| ---------------------- | -------------------------------------------- |
| `DJANGO_DEBUG`         | Enables debug mode                           |
| `DJANGO_SECRET_KEY`    | Required when debug is off                   |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated list of hosts                |
| `CORS_ALLOWED_ORIGINS` | Origins allowed to call the API              |
| `CSRF_TRUSTED_ORIGINS` | Trusted origins for CSRF                     |
| `REDIS_URL`            | **Required in production.** Shared Redis cache for Open-Meteo + plans (LocMem when empty in debug) |
| `VALHALLA_URL`         | Valhalla route endpoint                      |
| `OPEN_METEO_BASE_URL`  | Open-Meteo forecast endpoint                 |
| `OPEN_METEO_API_KEY`   | Optional commercial Open-Meteo key           |
| `HEATMAP_MAX_CELLS`    | Max cells in the corridor heatmap grid       |
| `LOG_LEVEL`            | Log level, defaults to `INFO`                |
| `WEB_CONCURRENCY`      | Gunicorn workers in the Docker image         |
| `VITE_API_URL`         | Backend origin for production builds         |

Weather is Redis-first: fresh hits skip Open-Meteo, stale hits return immediately (refresh in background). On Open-Meteo **429**, the API opens a short circuit and falls back to **NWS (weather.gov)** for US corridors (and stale Redis when present). No third-party API keys are required.

## Production

### Render (API)

`render.yaml` builds `backend/Dockerfile` and a Key Value cache (`weighcast-cache` → `REDIS_URL`).

1. Create the Blueprint from this repo (API + Redis Key Value must both exist).
2. Confirm `REDIS_URL` is linked from `weighcast-cache` — production refuses to boot without it.
3. Set `DJANGO_ALLOWED_HOSTS` to the Render hostname, for example `weighcast-api.onrender.com`
   (no `https://`).
4. Set `CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS` to the Vercel origin:
   `https://weighcast.vercel.app`.
5. `DJANGO_SECRET_KEY` is generated. Leave `DJANGO_DEBUG=false`.
6. Health check: `GET /api/health` → `{"status":"ok","cache":"redis","cache_ok":true}`.

The image runs `gunicorn` with `UvicornWorker`, two workers, 60s timeout, bound to `$PORT`.

### Vercel (frontend)

1. New project, **Root Directory** `frontend`.
2. Framework preset Vite (`frontend/vercel.json`).
3. Set `VITE_API_URL` to the Render origin, no trailing slash, for example
   `https://weighcast-api.onrender.com`.
4. Redeploy after changing the env var so it is baked into the bundle.

### Secrets

Keep `DJANGO_SECRET_KEY` in Render only. Do not put it in Vercel or the repo. CORS and CSRF must
list the exact HTTPS frontend origin. Redis stays on the Render private network (`ipAllowList: []`).

## Known limits

- The public Valhalla demo (`valhalla1.openstreetmap.de`) is fair-use, about one request per user
  per second, and is not a production SLA. Point `VALHALLA_URL` at your own instance for real traffic.
- Open-Meteo geocoding is strongest for cities. Street-level addresses are weaker.
- Forecasts are hourly and interpolated. This is a planning aid, not a dispatch or safety system.
- Route shapes use Google polylines at precision 6. The heatmap grid coarsens on long corridors
  so it stays under `HEATMAP_MAX_CELLS`.
