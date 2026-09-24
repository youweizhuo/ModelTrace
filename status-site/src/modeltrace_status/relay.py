"""Local pass-through for one probe that times the provider's response stream.

Codex reports whole items, not tokens, so the runner points it at this relay.
Requests and responses are forwarded unchanged apart from hop-by-hop headers and
Accept-Encoding (dropped so the event stream stays readable). Nothing is logged
or stored; only event types and arrival times are inspected.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
       "transfer-encoding", "upgrade", "host", "accept-encoding", "content-length"}


class Relay:
    def __init__(self, base_url, timeout):
        target = urlsplit(base_url)
        self.https = target.scheme == "https"
        self.host, self.port = target.hostname, target.port
        self.netloc = target.netloc.rpartition("@")[2]
        self.timeout = timeout
        self.failure = None  # connection error text, for diagnostics Codex can no longer see
        self.stream = None   # arrival times for the latest Responses stream
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                relay.forward(self)
            do_GET = do_PUT = do_PATCH = do_DELETE = do_POST

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_port}{target.path}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def forward(self, client):
        length = int(client.headers.get("Content-Length") or 0)
        body = client.rfile.read(length) if length else None
        headers = {k: v for k, v in client.headers.items() if k.lower() not in HOP} | {"Host": self.netloc}
        upstream = (http.client.HTTPSConnection if self.https else http.client.HTTPConnection)(self.host, self.port, timeout=self.timeout)
        sent = time.monotonic()
        try:
            upstream.request(client.command, client.path, body=body, headers=headers)
            response = upstream.getresponse()
        except (OSError, http.client.HTTPException) as error:
            self.failure = f"failed to connect: {error}"
            upstream.close()
            client.send_error(502)
            return
        timing = None
        if client.command == "POST" and response.status == 200 and client.path.split("?")[0].rstrip("/").endswith("/responses"):
            timing = self.stream = {"sent": sent, "first": None, "text_first": None, "text_last": None}
        size = response.getheader("Content-Length")
        client.send_response_only(response.status, response.reason)
        for key, value in response.getheaders():
            if key.lower() not in HOP:
                client.send_header(key, value)
        client.send_header(*(("Content-Length", size) if size is not None else ("Transfer-Encoding", "chunked")))
        client.end_headers()
        pending = b""
        try:
            while chunk := response.read1(65536):
                if timing:
                    pending = observe(timing, pending + chunk)
                client.wfile.write(chunk if size is not None else b"%x\r\n%s\r\n" % (len(chunk), chunk))
            if size is None:
                client.wfile.write(b"0\r\n\r\n")
        except (OSError, http.client.HTTPException):
            # Dropping the connection lets Codex report the interrupted stream itself.
            client.close_connection = True
        finally:
            upstream.close()

    def metrics(self, usage):
        """TTFT to the first streamed token (hidden reasoning included) and answer decode rate."""
        t = self.stream
        if not t or t["first"] is None:
            return {"ttft_ms": None, "output_tps": None}
        tokens = usage.get("output_tokens", 0) - usage.get("reasoning_output_tokens", 0)
        span = (t["text_last"] or 0) - (t["text_first"] or 0)
        return {"ttft_ms": round((t["first"] - t["sent"]) * 1000),
                "output_tps": round(tokens / span, 1) if tokens > 1 and span >= .05 else None}


def observe(timing, data):
    *lines, rest = data.split(b"\n")
    now = time.monotonic()
    for line in lines:
        if not line.startswith(b"data:"):
            continue
        try:
            kind = str(json.loads(line[5:]).get("type", ""))
        except (ValueError, AttributeError):
            continue
        if kind.endswith(".delta") and timing["first"] is None:
            timing["first"] = now
        if kind == "response.output_text.delta":
            timing["text_first"] = timing["text_first"] or now
            timing["text_last"] = now
    return rest
