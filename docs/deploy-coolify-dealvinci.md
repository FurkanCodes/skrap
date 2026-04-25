# Dealvinci Coolify Production Setup

This is the ELI5 production runbook for running Dealvinci on one DigitalOcean Droplet with Coolify.

Final public URLs:

```txt
https://dealvinci.com              frontend app
https://dealvinci.com/login        frontend login
https://dealvinci.com/dashboard    frontend dashboard
https://api.dealvinci.com          FastAPI backend
https://coolify.dealvinci.com      Coolify admin dashboard
```

Infrastructure:

```txt
DigitalOcean Droplet     runs Coolify, backend containers, frontend container, Redis
Supabase                 managed Postgres
Cloudflare               DNS, TLS proxy, dashboard protection
GitHub                   source code and deployment trigger
Coolify                  builds, runs, and redeploys services
```

## 0. Before You Start

You need:

- A DigitalOcean account with the $200 credits.
- A Cloudflare zone for `dealvinci.com`.
- A GitHub repository for the backend.
- A GitHub repository or monorepo folder for the frontend.
- Supabase project database URL.
- Your SSH public key added to DigitalOcean.

Do not put real secrets in GitHub. Secrets belong in Coolify environment variables or GitHub Actions secrets.

## 1. Create The Droplet

In DigitalOcean:

1. Go to **Droplets**.
2. Click **Create Droplet**.
3. Choose **Ubuntu 24.04 LTS**.
4. Choose at least **2 vCPU / 4 GB RAM**.
5. Add your SSH key.
6. Add tag:

```txt
dealvinci-prod
```

7. Create the Droplet.
8. Copy the public IPv4 address.

Recommended starting size:

```txt
2 vCPU / 4 GB RAM
```

Upgrade later if scraping, workers, or browser runtimes need more CPU.

## 2. Create The Firewall

In DigitalOcean:

1. Go to **Networking**.
2. Open **Firewalls**.
3. Create a firewall named:

```txt
dealvinci-prod-firewall
```

4. Attach it to Droplets with tag:

```txt
dealvinci-prod
```

Inbound rules:

```txt
SSH       TCP 22    your current IP only
HTTP      TCP 80    0.0.0.0/0 and ::/0
HTTPS     TCP 443   0.0.0.0/0 and ::/0
Coolify   TCP 8000  your current IP only, temporary
```

Outbound rules:

```txt
All TCP
All UDP
All ICMP
```

After `https://coolify.dealvinci.com` works, remove the `TCP 8000` inbound rule.

## 3. Create Cloudflare DNS Records

In Cloudflare:

1. Open `dealvinci.com`.
2. Go to **DNS**.
3. Add these records.

Initial setup records:

```txt
Type   Name      Value           Proxy
A      @         <droplet-ip>    DNS only
A      api       <droplet-ip>    DNS only
A      coolify   <droplet-ip>    DNS only
```

Use **DNS only** during initial setup. This makes Let's Encrypt certificates easier to issue.

After everything works:

```txt
Type   Name      Proxy
A      @         Proxied
A      api       Proxied or DNS only
A      coolify   DNS only, or protected with Cloudflare Access
```

Cloudflare SSL/TLS mode:

```txt
Full (strict)
```

Only switch to **Full (strict)** after Coolify has issued working HTTPS certificates.

## 4. Install Coolify

SSH into the Droplet:

```bash
ssh root@<droplet-ip>
```

Install Coolify:

```bash
curl -fsSL https://cdn.coollabs.io/coolify/install.sh | sudo bash
```

Open Coolify for the first time:

```txt
http://<droplet-ip>:8000
```

Create the first admin account.

## 5. Put Coolify On Its Domain

In Coolify:

1. Go to **Settings**.
2. Find the instance/domain setting.
3. Set:

```txt
https://coolify.dealvinci.com
```

4. Save.
5. Wait for Coolify to issue the certificate.
6. Open:

```txt
https://coolify.dealvinci.com
```

When that works:

1. Go back to DigitalOcean firewall.
2. Remove inbound `TCP 8000`.
3. Keep only `22`, `80`, and `443`.

## 6. Secure Coolify

Coolify can deploy code and read production secrets, so protect it like root access.

Do this immediately:

1. Enable 2FA on your Coolify account.
2. Use a long unique password.
3. Protect `coolify.dealvinci.com`.

Best Cloudflare option:

```txt
Cloudflare Zero Trust Access
```

Simpler first option:

```txt
Cloudflare WAF rule:
if hostname equals coolify.dealvinci.com
and IP is not your IP
then block
```

Do not expose Coolify on raw port `8000` after setup.

## 7. Connect GitHub To Coolify

In Coolify:

1. Go to **Sources**.
2. Add a **GitHub App** source.
3. Install it on the Dealvinci repository only.
4. Confirm Coolify can see the repo.

Preferred deployment model:

```txt
Pull request       -> GitHub Actions runs tests
Merge to main      -> GitHub Actions triggers Coolify deploy
Coolify            -> pulls/builds latest app
```

Avoid direct auto-deploy on every push until the test pipeline is in place.

## 8. Create The Coolify Project

In Coolify:

1. Click **Projects**.
2. Create project:

```txt
dealvinci
```

3. Create environment:

```txt
production
```

All resources below should live inside:

```txt
dealvinci / production
```

## 9. Backend Stack In Coolify

Use two separate Coolify resources:

```txt
dealvinci-backend    backend repo, API, workers, Redis
dealvinci-frontend   frontend repo, public website/app
```

Do not put the frontend inside the backend Docker Compose stack unless this is a monorepo and you intentionally want one combined stack. The clean setup is:

```txt
Backend repo/resource:
  api container
  scheduler_dispatcher container
  scheduler_worker container
  notifier container
  redis container

Frontend repo/resource:
  frontend container only
```

So yes: hook up the backend and frontend repos separately in Coolify. Each resource builds/runs its own Docker image. The backend resource runs multiple containers because the API, workers, notifier, and Redis are separate runtime processes.

If you have separate repos:

```txt
GitHub repo: dealvinci-backend  -> Coolify resource: dealvinci-backend
GitHub repo: dealvinci-frontend -> Coolify resource: dealvinci-frontend
```

If you have one monorepo:

```txt
GitHub repo: dealvinci
  /backend  -> Coolify resource: dealvinci-backend, base directory /backend
  /frontend -> Coolify resource: dealvinci-frontend, base directory /frontend
```

The rest of this section assumes the backend has its own repo or that Coolify's base directory is set to the backend folder.

Create a new resource:

```txt
Type: Docker Compose
Name: dealvinci-backend
Source: GitHub repo containing this backend
Branch: main
```

In the Coolify resource setup:

```txt
Build Pack / Type: Docker Compose
Repository: backend repo
Branch: main
Base directory: empty if backend repo, /backend if monorepo
Compose file location: docker-compose.coolify.yml if you add one, or paste the Compose content in Coolify
```

This repo includes the Compose file Coolify should use:

```txt
docker-compose.coolify.yml
```

In Coolify, set the Compose file location to:

```txt
docker-compose.coolify.yml
```

That makes deployments repeatable and reviewable in GitHub.

Use this Compose shape in Coolify.

```yaml
services:
  api:
    build:
      context: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips="*"
    environment:
      PORT: "8000"
    depends_on:
      redis:
        condition: service_healthy
    expose:
      - "8000"
    restart: unless-stopped

  scheduler_dispatcher:
    build:
      context: .
    command: python -m app.jobs.scheduler_dispatcher
    depends_on:
      redis:
        condition: service_healthy
    restart: unless-stopped

  scheduler_worker:
    build:
      context: .
    command: python -m app.jobs.scheduler_worker
    depends_on:
      redis:
        condition: service_healthy
    restart: unless-stopped

  notifier:
    build:
      context: .
    command: python -m app.jobs.notifier
    depends_on:
      redis:
        condition: service_healthy
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy noeviction
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10
    volumes:
      - redis-data:/data
    restart: unless-stopped

volumes:
  redis-data:
```

Important:

- Only `api` gets a public domain.
- `redis` gets no public domain.
- `redis` gets no published port.
- Do not run the old `python -m app.jobs.scheduler` process together with dispatcher/worker mode.

Backend public domain:

```txt
https://api.dealvinci.com
```

Backend internal port:

```txt
8000
```

Healthcheck path:

```txt
/health
```

Readiness path for monitoring:

```txt
/ready
```

Use `/health` for container liveness. Use `/ready` for external uptime monitoring because `/ready` depends on Supabase, Redis, and JWKS.

## 10. Backend Production Environment

In Coolify backend resource:

1. Open **Environment Variables**.
2. Paste the production env.
3. Replace placeholders.

```env
ENVIRONMENT=production
API_PREFIX=/api/v1
AUTH_REQUIRED=true

CORS_ALLOW_ORIGINS=https://dealvinci.com

BETTER_AUTH_JWKS_URL=https://dealvinci.com/api/auth/jwks
BETTER_AUTH_JWT_ISSUER=https://dealvinci.com
BETTER_AUTH_JWT_AUDIENCE=https://api.dealvinci.com
BETTER_AUTH_JWT_ALGORITHMS=EdDSA
BETTER_AUTH_JWKS_CACHE_TTL_SECONDS=300
BETTER_AUTH_JWKS_TIMEOUT_SECONDS=2

TRACKING_DATABASE_URL=postgresql://postgres.<project-ref>:<password>@<supabase-pooler-host>:6543/postgres?sslmode=require
TRACKING_DB_POOL_MIN_SIZE=1
TRACKING_DB_POOL_MAX_SIZE=3

SCHEDULER_POLL_SECONDS=10
SCHEDULER_CLAIM_BATCH_SIZE=100
SCHEDULER_MAX_INFLIGHT=8
SCHEDULER_LEASE_SECONDS=180
SCHEDULER_JITTER_PERCENT=10
SCHEDULER_ALIGN_CHECKS_TO_INTERVAL=false
SCHEDULER_ALERT_COOLDOWN_MINUTES=60

SCHEDULER_QUEUE_REDIS_URL=redis://redis:6379/0
SCHEDULER_QUEUE_STREAM_KEY=tracking:scheduler:jobs
SCHEDULER_QUEUE_CONSUMER_GROUP=tracking-scheduler-workers
SCHEDULER_QUEUE_MAX_STREAM_LENGTH=1000000
SCHEDULER_WORKER_CLAIM_BATCH_SIZE=16
SCHEDULER_WORKER_BLOCK_SECONDS=5
SCHEDULER_WORKER_PENDING_IDLE_SECONDS=60

NOTIFIER_POLL_SECONDS=5
NOTIFIER_CLAIM_BATCH_SIZE=100
NOTIFIER_LEASE_SECONDS=120
NOTIFIER_MAX_ATTEMPTS=5
NOTIFIER_BASE_BACKOFF_SECONDS=30
NOTIFIER_BACKOFF_MAX_SECONDS=3600
NOTIFIER_RETRY_JITTER_PERCENT=20
NOTIFIER_MAX_INFLIGHT=8

RATE_LIMIT_ENABLED=true
RATE_LIMIT_REDIS_URL=redis://redis:6379/0
RATE_LIMIT_WINDOW_SECONDS=60
RATE_LIMIT_DEFAULT_REQUESTS=120
RATE_LIMIT_SCRAPE_REQUESTS=20
RATE_LIMIT_DISCOVER_REQUESTS=10
RATE_LIMIT_FAIL_OPEN=false

SERVER_FETCH_ALLOWED_PORTS=80,443
SERVER_FETCH_ALLOW_PRIVATE_NETWORKS=false
SERVER_FETCH_RESOLVE_DNS=true

PERF_TRACE_ENABLED=false
PERF_SLOW_MS=100

TELEGRAM_BOT_TOKEN=
TELEGRAM_BOT_USERNAME=
TELEGRAM_WEBHOOK_SECRET=
TELEGRAM_CONNECT_TTL_MINUTES=15

AFFILIATE_LINK_TEMPLATE=
```

Notes:

- `TRACKING_DATABASE_URL` must use Supabase SSL.
- Keep DB pool small because API, dispatcher, worker, and notifier each create their own connections.
- `RATE_LIMIT_FAIL_OPEN=false` means if Redis is down, protected endpoints fail closed in production.
- `SERVER_FETCH_RESOLVE_DNS=true` helps block SSRF through domains that resolve to private IPs.

## 11. Frontend App In Coolify

Create another resource:

```txt
Type: Application
Name: dealvinci-frontend
Source: GitHub frontend repo or frontend folder
Branch: main
```

In the Coolify resource setup:

```txt
Build Pack / Type: Nixpacks, Dockerfile, or Docker Compose
Repository: frontend repo
Branch: main
Base directory: empty if frontend repo, /frontend if monorepo
Public domain: https://dealvinci.com
Public port: usually 3000 for Next.js
```

For most Next.js apps, start with a normal Application resource using Nixpacks or the repo's Dockerfile. You do not need Redis or backend workers in the frontend resource.

Domain:

```txt
https://dealvinci.com
```

If the frontend is Next.js, typical settings are:

```txt
Build command: npm run build
Start command: npm run start
Port: 3000
```

Frontend production env should include the equivalent of:

```env
NODE_ENV=production
NEXT_PUBLIC_API_URL=https://api.dealvinci.com

BETTER_AUTH_URL=https://dealvinci.com
BETTER_AUTH_SECRET=<long-random-secret>
BETTER_AUTH_TRUSTED_ORIGINS=https://dealvinci.com
```

If Better Auth uses a database adapter, add the required database env vars to the frontend resource too.

Better Auth must expose:

```txt
https://dealvinci.com/api/auth/jwks
```

The backend verifies JWTs from that JWKS endpoint.

## 12. Frontend And Backend Must Agree

These values must match exactly:

```txt
Frontend public origin: https://dealvinci.com
Backend public origin:  https://api.dealvinci.com
JWT issuer:             https://dealvinci.com
JWT audience:           https://api.dealvinci.com
JWKS URL:               https://dealvinci.com/api/auth/jwks
Backend CORS origin:    https://dealvinci.com
```

Backend:

```env
CORS_ALLOW_ORIGINS=https://dealvinci.com
BETTER_AUTH_JWKS_URL=https://dealvinci.com/api/auth/jwks
BETTER_AUTH_JWT_ISSUER=https://dealvinci.com
BETTER_AUTH_JWT_AUDIENCE=https://api.dealvinci.com
```

Frontend:

```env
NEXT_PUBLIC_API_URL=https://api.dealvinci.com
BETTER_AUTH_URL=https://dealvinci.com
BETTER_AUTH_TRUSTED_ORIGINS=https://dealvinci.com
```

## 13. Deploy Order

Use this order:

1. Deploy Coolify.
2. Deploy backend stack with Redis.
3. Deploy frontend.
4. Confirm frontend JWKS works.
5. Restart backend so JWKS warmup succeeds.
6. Test login.
7. Enable Cloudflare proxy and Full Strict TLS.
8. Lock down Coolify.

Temporary fallback:

```env
AUTH_REQUIRED=false
```

Only use this if you need to bring up the backend before Better Auth is ready. Switch it back to `true` before real users.

## 14. Smoke Test Checklist

Open these URLs:

```txt
https://dealvinci.com
https://dealvinci.com/login
https://dealvinci.com/api/auth/jwks
https://api.dealvinci.com/health
https://api.dealvinci.com/ready
```

Expected:

```txt
dealvinci.com                 loads frontend
dealvinci.com/login           loads login
dealvinci.com/api/auth/jwks   returns public keys JSON
api.dealvinci.com/health      200
api.dealvinci.com/ready       200 after Supabase, Redis, and JWKS are reachable
```

Then test auth:

1. Go to `https://dealvinci.com/login`.
2. Sign in.
3. Open dashboard.
4. Browser calls `https://api.dealvinci.com/api/v1/me`.
5. Expected result: `200`.
6. No CORS error.
7. No slow first-login JWKS delay.

## 15. GitHub Pipeline

Use GitHub Actions to gate production deploys.

Flow:

```txt
feature branch
pull request
tests run
merge to main
main workflow runs
Coolify deploy webhook fires
production redeploys
```

In Coolify:

1. Open backend resource.
2. Find deploy webhook.
3. Copy it.
4. Open frontend resource.
5. Find deploy webhook.
6. Copy it.

In GitHub:

1. Go to repo **Settings**.
2. Open **Secrets and variables**.
3. Open **Actions**.
4. Add:

```txt
COOLIFY_TOKEN
COOLIFY_BACKEND_WEBHOOK
COOLIFY_FRONTEND_WEBHOOK
```

Backend workflow example:

```yaml
name: Backend Deploy

on:
  push:
    branches: ["main"]
    paths:
      - "app/**"
      - "tests/**"
      - "requirements.txt"
      - "Dockerfile"
      - "docker-compose.coolify.yml"
      - ".github/workflows/backend-deploy.yml"

jobs:
  test-and-deploy:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Run tests
        run: pytest

      - name: Trigger Coolify backend deploy
        run: |
          curl --fail --request GET "${{ secrets.COOLIFY_BACKEND_WEBHOOK }}" \
            --header "Authorization: Bearer ${{ secrets.COOLIFY_TOKEN }}"
```

Frontend workflow example:

```yaml
name: Frontend Deploy

on:
  push:
    branches: ["main"]
    paths:
      - "frontend/**"
      - ".github/workflows/frontend-deploy.yml"

jobs:
  test-and-deploy:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: "22"
          cache: "npm"
          cache-dependency-path: frontend/package-lock.json

      - name: Install dependencies
        run: |
          cd frontend
          npm ci

      - name: Lint
        run: |
          cd frontend
          npm run lint

      - name: Build
        run: |
          cd frontend
          npm run build

      - name: Trigger Coolify frontend deploy
        run: |
          curl --fail --request GET "${{ secrets.COOLIFY_FRONTEND_WEBHOOK }}" \
            --header "Authorization: Bearer ${{ secrets.COOLIFY_TOKEN }}"
```

If frontend and backend are separate repositories, keep one workflow in each repo.

If they are in one monorepo, keep both workflows and adjust the `paths`.

## 16. Rollback

In Coolify:

1. Open the broken resource.
2. Go to deployments.
3. Redeploy the last known good deployment.

If backend is broken and frontend is still fine:

```txt
rollback backend only
```

If frontend auth is broken and backend is fine:

```txt
rollback frontend only
```

If Redis is broken:

1. Open backend stack logs.
2. Check `redis` container.
3. If scheduler worker logs show `NOGROUP`, unblock Redis:
   `redis-cli XGROUP CREATE tracking:scheduler:jobs tracking-scheduler-workers 0-0 MKSTREAM`
4. If Redis warns about memory overcommit, run `sysctl vm.overcommit_memory=1` on the host and persist `vm.overcommit_memory = 1` in `/etc/sysctl.conf`.
5. Restart only Redis if needed.
6. Then restart API and workers.

## 17. Logs To Check

Backend API:

```txt
api container logs
```

Scheduler:

```txt
scheduler_dispatcher logs
scheduler_worker logs
```

Notifier:

```txt
notifier logs
```

Redis:

```txt
redis logs
```

Useful backend log patterns:

```txt
application_startup
jwks_warm
jwks_fetch
request_complete
auth_failed
rate_limit
```

## 18. Production Hardening Checklist

Before real users:

- `ENVIRONMENT=production`.
- `AUTH_REQUIRED=true`.
- `CORS_ALLOW_ORIGINS=https://dealvinci.com`.
- No wildcard CORS.
- `RATE_LIMIT_ENABLED=true`.
- `RATE_LIMIT_FAIL_OPEN=false`.
- `SERVER_FETCH_ALLOW_PRIVATE_NETWORKS=false`.
- `SERVER_FETCH_RESOLVE_DNS=true`.
- Supabase URL uses `sslmode=require`.
- Redis has no public port.
- Coolify port `8000` is closed.
- Coolify dashboard has 2FA.
- Cloudflare SSL/TLS is `Full (strict)`.
- Cloudflare protects `coolify.dealvinci.com`.
- GitHub deploy requires passing tests.
- GitHub does not contain production secrets.

## 19. Common Problems

### Backend says token issuer is wrong

Check:

```env
BETTER_AUTH_JWT_ISSUER=https://dealvinci.com
BETTER_AUTH_URL=https://dealvinci.com
```

They must match the token issuer exactly.

### Backend says audience is wrong

Check:

```env
BETTER_AUTH_JWT_AUDIENCE=https://api.dealvinci.com
```

The frontend/Better Auth token must issue the same audience.

### Browser shows CORS error

Backend must have:

```env
CORS_ALLOW_ORIGINS=https://dealvinci.com
```

Do not use `*` in production.

### `/ready` fails but `/health` works

That means the API process is alive but one dependency is not ready.

Check:

```txt
Supabase connection
Redis connection
JWKS URL
```

### First login is slow

Check backend logs for:

```txt
jwks_warm
jwks_fetch
```

If JWKS warmup fails, make sure:

```txt
https://dealvinci.com/api/auth/jwks
```

is public and reachable from the Droplet.

### Redis connection fails

Inside backend stack, Redis URL should be:

```env
redis://redis:6379/0
```

Not:

```env
redis://127.0.0.1:6379/0
```

In Docker Compose, `127.0.0.1` means the current container, not the Redis container.

## 20. Maintenance Rhythm

Weekly:

- Check Coolify deployments.
- Check backend errors.
- Check Supabase connection count.
- Check Droplet CPU/RAM/disk.

Monthly:

- Apply OS updates.
- Rotate any exposed secrets.
- Review Cloudflare firewall events.
- Review GitHub Actions failures.

Before major launches:

- Upgrade Droplet temporarily.
- Lower scraper concurrency if target sites are unstable.
- Confirm Supabase pool capacity.
- Confirm `/ready` is green.
