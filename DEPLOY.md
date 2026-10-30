# Deployment

One container serves both the API and the web UI, so there is a single URL to submit.

## Option A: any VM (EC2 / DigitalOcean / college server) with Docker
    git clone <your repo> && cd apollocare
    export JWT_SECRET="$(openssl rand -hex 32)"      # required in real deployments
    export OPENAI_API_KEY=sk-...                      # optional
    docker compose up -d --build
    # UI + API docs: http://<server-ip>:8000   and   /docs
Put Nginx/Caddy in front for HTTPS if you have a domain.

## Option B: Render.com (free tier works)
1. Push the repo to GitHub.
2. New > PostgreSQL (copy its Internal Database URL).
3. New > Web Service > connect repo > Runtime: Docker (uses the root Dockerfile).
4. Environment variables: `DATABASE_URL` (the URL from step 2; the app rewrites `postgres://` itself),
   `JWT_SECRET` (long random string), `OPENAI_API_KEY` (optional).
5. Deploy. Open the service URL. Health check path: `/health`.
Railway and Fly.io work the same way: Dockerfile + Postgres + the same three variables.

## Before showing it to anyone
- Change `JWT_SECRET`. Sign in as `admin` / `Demo@1234` and treat demo passwords as public: this is synthetic demo data.
- Logs: `logs/app.log` (JSON lines). On Docker they are in the `applogs` volume: `docker compose exec app tail -f /app/logs/app.log`.
