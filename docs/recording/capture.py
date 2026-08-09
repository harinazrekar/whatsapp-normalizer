"""
Drives the live stack through the scenarios the case-study video shows, and
records exactly what came back. Everything that appears on screen has to come
out of this file -- no hand-written log lines.
"""

import hashlib
import hmac
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:8000"
WORKER_METRICS = "http://127.0.0.1:9100/metrics"
CATCHER = "http://127.0.0.1:8080"
VERIFY_TOKEN = "devi-capture-verify-2f9c"
APP_SECRET = "capture-app-secret-7b41d0e3a95c"
COMPOSE_DIR = str(Path(__file__).resolve().parents[2])  # repo root

OUT = Path(__file__).parent  # rewrites the committed capture in place
results: dict[str, object] = {}


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def request(method: str, url: str, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode(), (time.monotonic() - started) * 1000
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), (time.monotonic() - started) * 1000


def payload(message_id: str, text: str, from_number: str = "919820117733") -> dict:
    """A real WhatsApp Cloud API text-message webhook body."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "102290129340398",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550783881",
                                "phone_number_id": "106540352242922",
                            },
                            "contacts": [
                                {"profile": {"name": "Priya Raman"}, "wa_id": from_number}
                            ],
                            "messages": [
                                {
                                    "from": from_number,
                                    "id": message_id,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def post_webhook(body: dict, signature: str | None) -> tuple:
    raw = json.dumps(body, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return request("POST", f"{API}/webhook", raw, headers)


def compose(*args: str) -> str:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
    ).stdout


def logs(service: str, since: str = "5m") -> list[dict]:
    raw = subprocess.run(
        ["docker", "compose", "logs", "--no-log-prefix", "--since", since, service],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
    ).stdout
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def step(name: str) -> None:
    print(f"\n=== {name} ===", flush=True)


# --- 1. Meta verification handshake -----------------------------------------
step("handshake")
challenge = "1158201444"
status, body, ms = request(
    "GET",
    f"{API}/webhook?hub.mode=subscribe&hub.verify_token={VERIFY_TOKEN}&hub.challenge={challenge}",
)
results["handshake_ok"] = {"status": status, "body": body, "ms": round(ms, 1)}
print(f"valid token   -> {status} {body!r}")

status, body, ms = request(
    "GET",
    f"{API}/webhook?hub.mode=subscribe&hub.verify_token=guessed-it&hub.challenge={challenge}",
)
results["handshake_bad_token"] = {"status": status, "body": body, "ms": round(ms, 1)}
print(f"wrong token   -> {status} {body!r}")

# --- 2. Signature enforcement ------------------------------------------------
step("signature")
body_dict = payload("wamid.HBgMOTE5ODIwMTE3NzMzFQIAEhggQTVCNkQ3RjhFOTAxMjM0", "Is the 9am slot free?")

status, body, ms = post_webhook(body_dict, None)
results["unsigned"] = {"status": status, "body": body, "ms": round(ms, 1)}
print(f"no signature  -> {status} {body!r}")

status, body, ms = post_webhook(body_dict, "sha256=" + "0" * 64)
results["bad_signature"] = {"status": status, "body": body, "ms": round(ms, 1)}
print(f"bad signature -> {status} {body!r}")

# --- 3. A real signed delivery ----------------------------------------------
step("signed delivery")
raw = json.dumps(body_dict, separators=(",", ":")).encode()
status, body, ms = post_webhook(body_dict, sign(raw))
results["signed"] = {"status": status, "body": body, "ms": round(ms, 1)}
results["signature_header"] = sign(raw)
print(f"signed        -> {status} {body!r} in {ms:.0f}ms")

time.sleep(3)

# --- 4. Meta redelivers the same message ------------------------------------
step("duplicate")
status, body, ms = post_webhook(body_dict, sign(raw))
results["duplicate"] = {"status": status, "body": body, "ms": round(ms, 1)}
print(f"redelivered   -> {status} {body!r}")

time.sleep(2)
_, catcher_after_dup, _ = request("GET", f"{CATCHER}/stats")
results["catcher_after_duplicate"] = json.loads(catcher_after_dup)
print("catcher       ->", catcher_after_dup.replace("\n", " "))

# --- 5. kill -9 the worker mid-delivery -------------------------------------
step("kill -9 mid-delivery")
slow_body = payload(
    "wamid.HBgMOTE5ODIwMTE3NzMzFQIAEhggQzdFOEY5QTBCMTIzNDU2",
    "Booking confirmed - hold-the-line",
    from_number="919820117733",
)
slow_raw = json.dumps(slow_body, separators=(",", ":")).encode()
status, body, ms = post_webhook(slow_body, sign(slow_raw))
results["kill_ingest"] = {"status": status, "body": body, "ms": round(ms, 1)}
print(f"ingested      -> {status} {body!r}")

# The catcher holds this one open for 2s; kill the worker inside that window.
time.sleep(1.2)
kill_at = time.time()
subprocess.run(["docker", "kill", "-s", "KILL", "whatsapp-normalizer-worker-1"], capture_output=True)
print("killed worker mid-POST at", kill_at)
results["killed_at"] = kill_at

time.sleep(1.5)
_, catcher_after_kill, _ = request("GET", f"{CATCHER}/stats")
results["catcher_after_kill"] = json.loads(catcher_after_kill)
print("catcher       ->", catcher_after_kill.replace("\n", " "))

pending = subprocess.run(
    [
        "docker", "compose", "exec", "-T", "redis", "redis-cli",
        "XPENDING", "wa:events:stream", "wa-normalizer",
    ],
    cwd=COMPOSE_DIR, capture_output=True, text=True,
).stdout.strip()
results["xpending_after_kill"] = pending
print("XPENDING      ->", pending.replace("\n", " | "))

# --- 6. Bring a worker back; it must reclaim and finish the job -------------
step("recovery")
compose("up", "-d", "worker")
print("worker restarted, waiting for the reclaim window (CLAIM_MIN_IDLE_MS=20s)...")

# The kill landed AFTER the catcher had already logged the request, so
# distinct_message_ids is already 2 here -- watching that number would report
# success instantly and prove nothing. What recovery actually looks like is the
# pending entry draining: the reclaimed event is re-POSTed (total goes up) and
# in_flight returns to 0.
baseline_total = results["catcher_after_kill"]["total"]
reclaimed = None
for _ in range(45):
    time.sleep(2)
    _, stats, _ = request("GET", f"{CATCHER}/stats")
    parsed = json.loads(stats)
    _, api_stats, _ = request("GET", f"{API}/stats")
    depth = json.loads(api_stats)
    if parsed["total"] > baseline_total and depth["in_flight"] == 0:
        reclaimed = {"catcher": parsed, "depth": depth}
        break
results["catcher_after_recovery"] = reclaimed
print("catcher       ->", json.dumps(reclaimed))
results["recovery_seconds"] = round(time.time() - kill_at, 1)
print(f"recovered in  -> {results['recovery_seconds']}s after the kill")
if reclaimed is None:
    print("!! reclaim never observed -- do not use this run", file=sys.stderr)

# --- 7. Metrics from both targets -------------------------------------------
step("metrics")
_, api_metrics, _ = request("GET", f"{API}/metrics")
_, worker_metrics, _ = request("GET", WORKER_METRICS)
(OUT / "api-metrics.txt").write_text(api_metrics)
(OUT / "worker-metrics.txt").write_text(worker_metrics)

_, stats_body, _ = request("GET", f"{API}/stats")
results["stats"] = json.loads(stats_body)
print("stats         ->", stats_body)

_, health_body, _ = request("GET", f"{API}/health")
results["health"] = json.loads(health_body)

# --- 8. Save the raw logs ----------------------------------------------------
(OUT / "api-logs.json").write_text(json.dumps(logs("api"), indent=2))
(OUT / "worker-logs.json").write_text(json.dumps(logs("worker"), indent=2))
(OUT / "results.json").write_text(json.dumps(results, indent=2))

print("\nWrote", OUT)
