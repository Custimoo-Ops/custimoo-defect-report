#!/usr/bin/env python3
"""Single HTTP server: serves report + /api/refresh endpoint."""
import http.server
import json
import os
import urllib.request
import socketserver

TOK = os.environ.get("GH_WORKFLOW_TOKEN", "")
REPO = "lars-lakr/custimoo-defect-report"
PORT = int(os.environ.get("PORT", 8080))
REPORT = "/app/report.html"
WF = "deploy.yml"

class H(http.server.BaseHTTPRequestHandler):
    def _file(self, path, ct="text/html"):
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404")

    def do_GET(self):
        if self.path == "/api/refresh":
            return self._refresh()
        if self.path == "/api/status":
            return self._status()
        if self.path in ("/", "/index.html"):
            return self._file(REPORT)
        return self._file(self.path.lstrip("/"), "text/plain")

    def do_POST(self):
        self.do_GET()

    def _refresh(self):
        if self._running():
            return self._json(429, {"ok": False, "error": "Already running"})
        try:
            u = f"https://api.github.com/repos/{REPO}/actions/workflows/{WF}/dispatches"
            d = json.dumps({"ref": "main"}).encode()
            r = urllib.request.Request(u, data=d, method="POST",
                headers={"Authorization": "Bearer " + TOK,
                         "Accept": "application/vnd.github+json",
                         "Content-Type": "application/json"})
            urllib.request.urlopen(r)
            self._json(200, {"ok": True, "message": "Triggered"})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)[:200]})

    def _status(self):
        try:
            u = f"https://api.github.com/repos/{REPO}/actions/runs?per_page=1"
            r = urllib.request.Request(u,
                headers={"Authorization": "Bearer " + TOK, "Accept": "application/vnd.github+json"})
            data = json.loads(urllib.request.urlopen(r).read())
            runs = data.get("workflow_runs", [])
            self._json(200, {
                "conclusion": runs[0]["conclusion"] if runs else "unknown",
                "ts": runs[0]["updated_at"] if runs else None
            })
        except Exception as e:
            self._json(500, {"error": str(e)[:200]})

    def _running(self):
        try:
            u = f"https://api.github.com/repos/{REPO}/actions/runs?per_page=1&status=in_progress"
            r = urllib.request.Request(u,
                headers={"Authorization": "Bearer " + TOK, "Accept": "application/vnd.github+json"})
            data = json.loads(urllib.request.urlopen(r).read())
            return len(data.get("workflow_runs", [])) > 0
        except Exception:
            return False

    def _json(self, code, data):
        b = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, f, *a):
        pass

print("Serving on port", PORT, flush=True)
httpd = socketserver.TCPServer(("0.0.0.0", PORT), H)
httpd.serve_forever()
