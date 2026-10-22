# Free Demo Deployment (Vercel + Render + Supabase)

A zero-cost setup for showing TILVG to people. It is not production: it trades
background jobs and warm starts for a $0 bill.

```txt
Vercel (Hobby)          skrap-ui     Next.js frontend + Better Auth
Render (Free web)       skrap-api    FastAPI backend (Docker)
Render (Free Key Value) skrap-redis  Redis for rate limits and the due index
Supabase (Free)         skrap        Postgres for both apps
```

## What does not work on the free tier

- **No scheduler.** Render has no free background workers, so the notifier runs
  inside the API container (see `dockerCommand` in `render.yaml`) but the
  scheduler does not run. Tracked items are never re-checked automatically and
  no price-drop alerts are sent. Telegram linking and "tracking started/stopped"
  messages work. Scraping, discovery, and creating/listing tracked items work.
- **Cold starts.** The Render service sleeps after 15 minutes idle; the first
  request after that takes about a minute. Open the API URL before a demo.
- **512 MB RAM.** Plain HTTP scrapes are fine. Sites that need a headless browser
  may run out of memory.
- **Supabase pauses** free projects after about a week of inactivity. Restore it
  from the Supabase dashboard before a demo if needed.
- **Email is off.** The frontend runs with `AUTH_DEMO_MODE=true`: sign-up skips
  email verification and signs the user straight in. Google/GitHub sign-in stays
  off unless you add those OAuth apps' callback URLs for the new domain.

## 1. Frontend on Vercel

1. vercel.com → **Add New → Project** → import `FurkanCodes/skrap-ui`.
2. Project name `skrap-ui`. Framework preset Next.js is detected automatically.
3. Under **Environment Variables**, paste the contents of `vercel.env`
   (the import screen accepts a pasted `.env` block).
4. Deploy and note the domain (for example `https://skrap-ui.vercel.app`).

Required variables (billing/Paddle are not needed; checkout buttons just fail if clicked):

| Variable | Value |
| --- | --- |
| `BACKEND_BASE_URL` | Render API URL |
| `BETTER_AUTH_URL`, `NEXT_PUBLIC_BETTER_AUTH_URL` | Vercel URL (also used as the JWT issuer) |
| `BETTER_AUTH_JWT_AUDIENCE` | Render API URL |
| `BETTER_AUTH_SECRET` | 32+ char secret |
| `DATABASE_URL` | Supabase transaction pooler URL (port 6543) |
| `AUTH_DEMO_MODE` | `true` |

## 2. Backend on Render

1. render.com → **New → Blueprint** → connect `FurkanCodes/skrap`.
   Render reads `render.yaml` and creates `skrap-api` and `skrap-redis`.
2. Fill in the variables marked `sync: false` from `render.env`:
   `TRACKING_DATABASE_URL`, `CORS_ALLOW_ORIGINS`, `BETTER_AUTH_JWKS_URL`,
   `BETTER_AUTH_JWT_ISSUER`, `BETTER_AUTH_JWT_AUDIENCE`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_WEBHOOK_SECRET`. Everything else,
   including the Redis URL, is set by the blueprint. `ENVIRONMENT=demo` (not
   `production`) so the backend does not require Paddle billing config.
3. Apply. The first build takes several minutes (Playwright base image).
4. Check `https://<api>/health` and `https://<api>/ready`.

## 3. Connect them

If either platform gave a different domain than the one used in the env files,
update these and redeploy the affected service:

- Vercel: `BACKEND_BASE_URL`, `BETTER_AUTH_JWT_AUDIENCE`, `BETTER_AUTH_URL`, `NEXT_PUBLIC_BETTER_AUTH_URL`.
- Render: `CORS_ALLOW_ORIGINS`, `BETTER_AUTH_JWKS_URL`, `BETTER_AUTH_JWT_ISSUER`, `BETTER_AUTH_JWT_AUDIENCE`.

## 4. Point the Telegram bot at Render

Telegram must know where to deliver bot updates (used when a user links their
chat). Register the webhook once the API is live, using the same secret as
`TELEGRAM_WEBHOOK_SECRET`:

```bash
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://skrap-api.onrender.com/api/v1/integrations/telegram/webhook" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

Check it with `getWebhookInfo`. Because the free service sleeps, the first
update after an idle period can take about a minute; Telegram retries until the
API responds.

## Notes

The backend verifies frontend-issued JWTs by fetching
`<vercel-url>/api/auth/jwks`, so issuer/audience must match on both sides exactly.
