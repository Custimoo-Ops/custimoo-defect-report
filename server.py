#!/usr/bin/env python3
import http.server, json, os, urllib.request, socketserver, sys

TOK = os.environ.get("GH_WORKFLOW_TOKEN", "")
REPO = "lars-lakr/custimoo-defect-report"
PORT = int(os.environ.get("PORT", 8080))

class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory="/app", **kwargs)

    def do_GET(self):
        if self.path == "/api/refresh":
            self._refresh()
        elif self.path == "/api/status":
            self._status()
        elif self.path == "/":
            self.path = "/index.html"
            super().do_GET()
        else:
            super().do_GET()

    def _call(self, url, method="GET", body=None):
        headers = {"Authorization": "Bearer " + TOK, "Accept": "application/vnd.github+json"}
        if body:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}

    def _refresh(self):
        try:
            runs = self._call(f"https://api.github.com/repos/{REPO}/actions/runs?per_page=1&status=in_progress")
            if runs.get("workflow_runs"):
                return self._json(429, {"ok": False, "error": "Already running"})
            self._call(f"https://api.github.com/repos/{REPO}/actions/workflows/deploy.yml/dispatches",
                       method="POST", body=json.dumps({"ref": "main"}).encode())
            self._json(200, {"ok": True, "message": "Refresh triggered"})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)[:200]})

    def _status(self):
        try:
            data = self._call(f"https://api.github.com/repos/{REPO}/actions/runs?per_page=1&event=push&status=completed")
            runs = data.get("workflow_runs", [])
            if runs:
                self._json(200, {"conclusion": runs[0]["conclusion"], "updated_at": runs[0]["updated_at"]})
            else:
                self._json(200, {"conclusion": "unknown"})
        except Exception as e:
            self._json(500, {"error": str(e)[:200]})

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

print(f"Server on :{PORT}", flush=True)
socketserver.TCPServer.allow_reuse_address = True
httpd = socketserver.TCPServer(("0.0.0.0", PORT), H)
httpd.serve_forever()
