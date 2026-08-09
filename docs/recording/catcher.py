"""
Downstream catcher for the capture run.

POST /hook  -> 200, appends the delivery to received.jsonl
POST /fail  -> 500, so the retry -> dead-letter path can be exercised
GET  /stats -> counts, including how many distinct message_ids arrived

Runs on the host; the containers reach it at host.docker.internal:8080.
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUT = Path(__file__).parent / "capture" / "received.jsonl"
OUT.parent.mkdir(parents=True, exist_ok=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"_unparsed": raw.decode("utf-8", "replace")}

        # Hold the connection open when the event asks for it, so the worker can
        # be killed while it is genuinely mid-POST -- the only way to reproduce
        # "died after the request landed, before it learned the outcome".
        text = (body.get("text") or "") if isinstance(body, dict) else ""
        if "hold-the-line" in str(text):
            time.sleep(2.0)

        record = {
            "at": time.time(),
            "path": self.path,
            "correlation_id": self.headers.get("X-Correlation-Id"),
            "message_id": body.get("message_id"),
            "event_type": body.get("event_type"),
            "retry_count": body.get("retry_count", 0),
            "body": body,
        }
        with OUT.open("a") as fh:
            fh.write(json.dumps(record) + "\n")

        status = 500 if self.path.startswith("/fail") else 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}' if status == 200 else b'{"error":"downstream down"}')

    def do_GET(self) -> None:  # noqa: N802
        rows = [json.loads(line) for line in OUT.read_text().splitlines()] if OUT.exists() else []
        payload = {
            "total": len(rows),
            "distinct_message_ids": len({r["message_id"] for r in rows if r["message_id"]}),
            "by_path": {p: sum(1 for r in rows if r["path"] == p) for p in {r["path"] for r in rows}},
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload, indent=2).encode())

    def log_message(self, *_args) -> None:
        # Silence the default stderr access log; received.jsonl is the record.
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"catcher listening on :{port}, writing {OUT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
