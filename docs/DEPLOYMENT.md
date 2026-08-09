# Deployment

There are exactly two supported ways to run this service, and the difference
between them is only how WhatsApp reaches you over HTTPS:

| | Local development | Production |
| --- | --- | --- |
| Compose file | `docker-compose.yml` | `docker-compose.prod.yml` |
| Public URL | ngrok tunnel (`https://<id>.ngrok-free.app`) | your domain (`https://wa.example.com`) |
| TLS | terminated by ngrok | terminated by Caddy, certificate from Let's Encrypt |
| URL stability | changes every restart on the free plan | permanent |
| Redis host port | `127.0.0.1:6379` (for `redis-cli`) | not published at all |
| `REQUIRE_SIGNATURE` | may be `false` before you have an app secret | always `true` |
| Extra env vars | none | `DOMAIN`, `ACME_EMAIL` |

Meta will not accept an `http://` webhook URL, and it will not accept an HTTPS
URL whose certificate does not validate. That constraint is the reason both
paths exist — ngrok borrows someone else's certificate, Caddy gets you your own.

---

## Local development (ngrok)

### 1. Configure

```bash
cp .env.example .env
```

Edit `.env`:

- `WHATSAPP_VERIFY_TOKEN` — any random string. Generate one:
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- `WHATSAPP_APP_SECRET` — Meta App Dashboard → App Settings → Basic → App Secret.
- `REQUIRE_SIGNATURE` — leave `true` if you have the app secret. Set it to
  `false` *only* while you don't, and set it back before the tunnel stays up
  for longer than a debugging session. A tunnel URL is a public URL.
- `DOWNSTREAM_WEBHOOK_URL` — where normalized events get POSTed. Leave blank to
  queue only and inspect via `/stats`.
- `REDIS_URL` — leave as `redis://redis:6379/0`; that's the compose service name.

`DOMAIN` and `ACME_EMAIL` are ignored here.

### 2. Start the stack

```bash
docker compose up -d --build
docker compose ps          # api and redis should both read "healthy"
curl -s localhost:8000/health
```

`api` reports healthy only once it can talk to Redis, so a healthy `api` means
the whole chain is up.

`worker` shows no health status, by design. It runs the same image as `api`, and
that image's `HEALTHCHECK` probes the API's `/health` over HTTP — but the worker
binds no port, so the probe can never pass. Leaving the `healthcheck:` block out
does not disable it; the inherited one still runs and marks a perfectly healthy
worker unhealthy forever. Both compose files therefore set
`healthcheck: disable: true` on the worker. Judge it by `/stats` instead —
`in_flight` moving and `queued` draining.

### 3. Open the tunnel

In a second terminal:

```bash
ngrok http 8000
```

ngrok prints a forwarding line:

```
Forwarding    https://a1b2-93-184-216-34.ngrok-free.app -> http://localhost:8000
```

Copy the **https** URL. On the free plan it changes every time you restart
ngrok, and each change means redoing step 4.

Verify the tunnel reaches the app before touching the dashboard:

```bash
curl -s https://a1b2-93-184-216-34.ngrok-free.app/health
```

### 4. Point Meta at it

1. Meta App Dashboard → your app → **WhatsApp → Configuration**.
2. Under **Webhook**, click **Edit**.
3. **Callback URL**: the ngrok HTTPS URL plus the route — e.g.
   `https://a1b2-93-184-216-34.ngrok-free.app/webhook`. The `/webhook` suffix is
   required; pasting the bare host is the most common verification failure.
4. **Verify token**: the exact `WHATSAPP_VERIFY_TOKEN` from your `.env`.
5. Click **Verify and save**. Meta immediately issues a `GET /webhook` with
   `hub.mode=subscribe`, and the dialog closes only if the service echoes the
   challenge back.
6. Under **Webhook fields**, click **Manage** and subscribe to **messages**.
   Verification alone does not deliver anything — without this subscription the
   webhook stays silent and looks broken.

### 5. Confirm deliveries

Send a WhatsApp message to your test number, then:

```bash
docker compose logs -f api worker
curl -s localhost:8000/stats
```

You should see the event ingested by `api` and delivered by `worker`, and the
stream depth move in `/stats`.

---

## Production (real domain + Caddy)

### 1. DNS

Point a hostname at the server's public IP **before** starting the stack —
Caddy's ACME challenge fails if the name doesn't already resolve to this host.

```
Type  Name  Value            TTL
A     wa    203.0.113.10     300
```

(and an `AAAA` record too if the host has IPv6). Confirm propagation:

```bash
dig +short wa.example.com
```

TCP 80 and 443 must be open inbound. Port 80 is not optional — it carries the
HTTP-01 challenge and the redirect to HTTPS.

### 2. Configure

```bash
cp .env.example .env
```

Everything from local dev applies, plus:

- `DOMAIN=wa.example.com` — must match the DNS record exactly. Caddy requests a
  certificate for this literal name.
- `ACME_EMAIL=ops@example.com` — a mailbox you actually read; it's where
  expiry warnings go.
- `REQUIRE_SIGNATURE=true` — non-negotiable here.
- `WHATSAPP_APP_SECRET` — must be set; the service refuses to boot without it
  while `REQUIRE_SIGNATURE` is true.

### 3. Start

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f caddy
```

First-time certificate issuance takes a few seconds. Watch the Caddy logs for a
`certificate obtained successfully` line; ACME failures are logged there in full
and are almost always DNS or a closed port 80.

Verify from **outside** the server (a laptop, not the box itself):

```bash
curl -sS https://wa.example.com/health
curl -sSI https://wa.example.com/webhook | head -1     # 403 or 422, not a TLS error
```

A TLS error means the certificate isn't issued yet. A `403` on the bare
`GET /webhook` is correct and expected — it means the handshake validator is
running and rejected a request with no `hub.*` parameters.

### 4. Point Meta at it

Same as local dev step 4, with `https://wa.example.com/webhook` as the callback
URL. Because the URL is permanent, this is a one-time step — no re-pasting after
every restart.

### 5. Verify it's live

```bash
# From outside the host
curl -sS https://wa.example.com/health

# On the host
docker compose -f docker-compose.prod.yml exec api \
  python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/stats').read().decode())"
```

Then send a real WhatsApp message to the connected number and watch
`docker compose -f docker-compose.prod.yml logs -f api worker`. An event that
appears in the `api` log but never in the `worker` log means the downstream POST
is failing — check `DOWNSTREAM_WEBHOOK_URL` and the DLQ size in `/stats`.

`/metrics` is served only to private-address scrapers (see `Caddyfile`); from the
public internet it returns 404 by design. Scrape it from the host or from inside
the compose network.

---

## Operational notes

**Redis persistence.** Redis holds the durable event stream now, not a
throwaway list, so both compose files run it with `appendonly yes` and
`appendfsync everysec` against a named volume. Do not delete `redis-data`
between deploys — that discards undelivered events and the consumer group's
pending list. Production also sets `maxmemory-policy noeviction`: when the
stream outgrows memory, writes should fail loudly rather than have Redis quietly
evict events that were never delivered.

**Deploys.** `docker compose -f docker-compose.prod.yml up -d --build` recreates
changed services only. The worker gets a 30s grace period on SIGTERM to finish
and acknowledge the event it is holding; an entry killed before its ack stays
pending and is picked up by the reclaim routine, so a hard kill costs a
redelivery, not an event.

**Certificate renewal** is automatic and needs no cron. It depends on the
`caddy-data` volume surviving — that's where the ACME account key and issued
certificates live. Recreating it on every deploy will eventually hit Let's
Encrypt rate limits.

**Rotating the app secret.** Update `WHATSAPP_APP_SECRET` in `.env`, then
`docker compose -f docker-compose.prod.yml up -d api worker`. There is a brief
window where in-flight deliveries signed with the old secret are rejected with
403; Meta retries them, so they are not lost.

**Logs** are capped at 5 × 10 MB per service via the json-file driver. Ship them
somewhere durable before relying on them for an incident more than a day old.

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| "The callback URL or verify token couldn't be validated" | Wrong verify token, missing `/webhook` suffix, or the service isn't reachable. Curl the exact callback URL from outside first. |
| Verification succeeds, no messages arrive | Webhook fields not subscribed — Configuration → Webhook fields → Manage → **messages**. |
| Works, then stops after a restart | ngrok free-plan URL changed. Re-paste the new URL into the dashboard. |
| Every delivery returns 403 | `WHATSAPP_APP_SECRET` doesn't match the app sending the webhook — check you copied it from the same app, not a sibling test app. |
| Caddy never gets a certificate | DNS not resolving to this host yet, or port 80 blocked upstream (cloud security group, not just the host firewall). |
| `api` container stuck unhealthy | It can't reach Redis. `docker compose logs redis`, and confirm `REDIS_URL` uses the service name `redis`, not `localhost`. |
| `worker` container reads `unhealthy` | Not a worker fault. It inherits the image's `HEALTHCHECK`, an HTTP probe of the API's `/health`, and binds no port of its own. Both compose files disable it with `healthcheck: disable: true`; if you still see this, you are running an older compose file. |
| `worker` restarts every ~5s, delivers nothing | The Redis socket read timeout is at or below `BLOCK_MS`, so the blocking `XREADGROUP` on an idle stream raises `TimeoutError` instead of returning empty. `get_redis()` derives it from `Settings.socket_timeout_seconds()` (`BLOCK_MS / 1000 + 5`) — check nothing is overriding `socket_timeout`. |
| Service exits immediately at boot | Config validation refused to start insecurely — the log line names the offending variable. |
