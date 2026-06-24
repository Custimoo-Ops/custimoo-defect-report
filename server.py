"""Single HTTP server: serves report + /api/refresh endpoint."""
import http.server, json, os, urllib.request, time, socketserver
from datetime import datetime, timezone

GITHUB_TOKEN = os.environ.get("GH_WORKFLOW_TOKEN", "")
REPO = "lars-lakr/custimoo-defect-report"
WORKFLOW_FILE = "deploy.yml"
PORT = int(os.environ.get("PORT", 8080))
REPORT_PATH = "/app/report.html"

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory="/app", **kwargs)

    def do_GET(self):
        if self.path == "/api/refresh":
            self._trigger()
        elif self.path == "/api/status":
            self._status()
        elif self.path == "/" or self.path == "":
            self.path = "/report.html"
            super().do_GET()
        else:
            super().do_GET()

    def do_POST(self):
        self.do_GET()

    def _trigger(self):
        if self._already_running():
            self._json(429, {"ok": False, "error": "Workflow already running"})
            return
        try:
            url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
            data = json.dumps({"ref": "main"}).encode()
            req = urllib.request.Request(url, data=data, method="POST",
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                         "Accept": "application/vnd.github+json",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req) as resp:
                self._json(200, {"ok": True, "message": "Refresh triggered — updates in ~2 min"})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)[:200]})

    def _status(self):
        try:
            url = f"https://api.github.com/repos/{REPO}/actions/runs?per_page=1&event=push&status=completed"
            req = urllib.request.Request(url,
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                         "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
            runs = data.get("workflow_runs", [])
            if runs:
                self._json(200, {"conclusion": runs[0]["conclusion"], "updated_at": runs[0]["updated_at"]})
            else:
                self._json(200, {"conclusion": "unknown"})
        except Exception as e:
            self._json(500, {"error": str(e)[:200]})

    def _already_running(self):
        try:
            url = f"https://api.github.com/repos/{REPO}/actions/runs?per_page=1&status=in_progress"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
            return len(data.get("workflow_runs", [])) > 0
        except:
            return False

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Serving on port {PORT}")
        httpd.serve_forever()
