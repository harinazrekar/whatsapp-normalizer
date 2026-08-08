# Security

## Threat model

This service sits on the public internet with a URL that Meta must be able to
reach. That single fact drives everything below: **the URL is not a secret**, and
anything that treats it as one is a bug.

### What this service assumes

- The webhook URL will be discovered. It appears in the Meta App Dashboard, in
  DNS/certificate transparency logs, and in anything that ever proxied it.
- Anyone can send arbitrary bytes to `POST /webhook`.
- WhatsApp redelivers the same event multiple times, by design, and that is not
  an attack — it is normal traffic that must not produce duplicate side effects.

### Threats and mitigations

| Threat | Mitigation |
| --- | --- |
| Forged webhook deliveries from anyone who knows the URL | `X-Hub-Signature-256` HMAC-SHA256 over the raw request body, keyed with the Meta App Secret. Unsigned or mis-signed requests get `403` before any parsing or queueing. |
| Timing attacks against the signature or verify token | All secret comparisons use `hmac.compare_digest`, never `==`. |
| Signature bypass via body mutation | The HMAC is computed over the exact bytes read off the wire, before JSON parsing. Re-serialized JSON is never used for verification. |
| Replay of a previously-valid signed payload | Every `message_id` is claimed atomically in Redis (`SET NX EX`) for `DEDUP_TTL_SECONDS`. A replayed delivery is counted and dropped, not forwarded. |
| Request flooding / replay storms | Per-IP rate limiting on `POST /webhook` (`WEBHOOK_RATE_LIMIT`, default 120/min). Behind a reverse proxy this requires `FORWARDED_ALLOW_IPS` to be set (the prod compose file does this) — otherwise uvicorn ignores `X-Forwarded-For` and every client shares a single bucket keyed on the proxy's address. Caddy *overwrites* `X-Forwarded-For` rather than appending, so the header cannot be spoofed. |
| Non-ASCII input crashing signature comparison | All secret comparisons encode to bytes first. `hmac.compare_digest` raises `TypeError` on non-ASCII `str`, and header values are latin-1 decoded, so this was reachable pre-authentication. |
| Unbounded growth exhausting Redis | Both the event stream (`STREAM_MAXLEN`) and the DLQ (`DLQ_MAXLEN`) are capped. With `noeviction` set in production, an uncapped DLQ would eventually make ingest writes fail. |
| Downstream webhook URL leaking via logs | Only the origin is logged (`https://host/...`). For Slack and n8n endpoints the full URL is itself the credential. |
| Accidentally running with no secret configured | `Settings.validate()` runs at startup and aborts the process if the verify token or app secret is missing or still a known placeholder. |
| Malformed payloads causing 5xx (which makes Meta back off deliveries) | The normalizer coerces null/wrong-typed nodes rather than raising; invalid JSON returns `400`, never an unhandled `500`. Note the one *deliberate* 5xx: if Redis is unreachable, ingest returns `503` so Meta redelivers, because answering `200` would drop the event silently. Alert on it. |
| Secrets leaking into logs | Log records carry `message_id`, `event_type`, and correlation IDs — never the app secret or verify token. |

### Explicitly out of scope

- **Message content confidentiality at rest.** Normalized events, including
  message text, are stored in Redis until delivered. Run Redis on a private
  network, and enable TLS and a password if it is not colocated.
- **Downstream authentication.** The service POSTs to `DOWNSTREAM_WEBHOOK_URL`
  as configured. Securing that endpoint is the operator's responsibility.
- **TLS termination.** Handled by the reverse proxy (see the Caddy setup in the
  README), not by the application.

## Configuration checklist before going live

- [ ] `WHATSAPP_VERIFY_TOKEN` set to a random string, not a guessable one
- [ ] `WHATSAPP_APP_SECRET` set from the Meta App Dashboard
- [ ] `REQUIRE_SIGNATURE` left at `true`
- [ ] `.env` is not committed (it is in `.gitignore` — keep it there)
- [ ] Redis is not exposed on a public interface
- [ ] The webhook is served over HTTPS with a valid certificate

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems.

Email **businesswhari@gmail.com** with:

- a description of the issue and its impact,
- the steps or a proof-of-concept needed to reproduce it,
- the commit or version you tested against.

You can expect an acknowledgement within 3 business days and an assessment with
a remediation plan within 10 business days. If you would like credit in the
release notes for the fix, say so and it will be included.

## Supported versions

| Version | Supported |
| --- | --- |
| 1.0.x | ✅ |
| < 1.0 | ❌ (pre-hardening proof-of-concept, do not deploy) |
