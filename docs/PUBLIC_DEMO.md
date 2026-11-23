# Public demo

`serve --public` runs the workspace as a shared demo on the synthetic CRM sandbox. Local mode is unchanged: it binds to loopback only and trusts its single user.

```sh
PUBLIC_HOSTNAMES=demo.example.com OPENAI_API_KEY=... OPENAI_MODEL=gpt-5.4-mini \
  .venv/bin/python main.py --config config.synthetic.json serve --public --provider openai
```

## What changes in public mode

| Concern | Local mode | Public mode |
| --- | --- | --- |
| Network | `127.0.0.1` only | All interfaces on `$PORT`; requests accepted only for the configured hostnames (`PUBLIC_HOSTNAMES`, plus `RENDER_EXTERNAL_HOSTNAME` on Render) |
| Visitors' data | One user sees everything | A random `HttpOnly` session cookie scopes conversations, runs and history. Other visitors' IDs return 404 |
| Model spending | Unlimited | Per-visitor limits (default 5 a minute, 30 a day), a global daily question cap (400) and a global **estimated** daily spend ceiling (default $1.00). A run with no cost estimate counts as $0.03, never as free |
| Concurrency | Unlimited | At most 3 model requests at once; extra requests get a "busy" message |
| Message size | 8,000 characters | 1,000 characters |
| Quality checks | Can run paid evaluations | Running is disabled. Bundled reports from real runs (`demo/reports`) and their traces (`demo/runs`) are read-only, so visitors can open each recorded answer |
| Data sources | Any configured integration | Startup fails if a live connector such as HubSpot is configured |

The counters live in memory and reset at UTC midnight and on restart. They are estimates checked between requests, not a billing cap. Set a hard monthly budget on the OpenAI project that owns the key as well.

Per-visitor limits key on the `True-Client-IP` header that Render's Cloudflare edge sets (`DEMO_CLIENT_IP_HEADER` changes it). Render appends to, but does not reset, `X-Forwarded-For`, so that header is only a fallback. A visitor who spoofs headers can at most get more than their share. The global ceilings still bind.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `PUBLIC_HOSTNAMES` | none | Comma-separated hostnames to accept (Render adds its own automatically) |
| `DEMO_DAILY_BUDGET_USD` | 1.00 | Estimated spend ceiling across all visitors per UTC day |
| `DEMO_PER_DAY` | 30 | Questions per visitor per UTC day |
| `DEMO_PER_MINUTE` | 5 | Questions per visitor per minute |
| `DEMO_GLOBAL_PER_DAY` | 400 | Questions across all visitors per UTC day |
| `DEMO_MAX_CONCURRENT` | 3 | Simultaneous model requests |
| `DEMO_MAX_MESSAGE_CHARS` | 1000 | Longest accepted message |

## Deploy on Render

1. In the OpenAI dashboard, create a project for the demo with a monthly budget (for example $5) and create an API key in it.
2. In Render: **New → Blueprint**, then choose this repository. Render reads `render.yaml`.
3. When asked, paste the key into `OPENAI_API_KEY`. It is stored by Render, not in the repository or image.
4. Deploy. The service is live at `https://<name>.onrender.com`; `/healthz` is the health check.

On the free plan the service sleeps after inactivity, so the first request can take about a minute. Conversations and traces are written to the container's disk and are discarded when it restarts or redeploys.

## Updating the bundled evidence

`demo/reports` holds evaluation reports, and `demo/runs` holds the traces for every case in them. They come from the synthetic development and held-out suites run on 2026-11-22 (see `docs/BASELINE.md`). To refresh them, copy new report JSON from `.evaluations/synthetic/` and the matching run JSON from `.runs/synthetic/`, check that they contain only synthetic data, and redeploy.
