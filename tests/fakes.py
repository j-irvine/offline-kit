"""Stand-ins for the two outside services, so the tests need no internet:
a fake Ollama (the AI engine) and a fake Kiwix download server."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"127.0.0.1:{server.server_address[1]}"


class FakeOllama(BaseHTTPRequestHandler):
    models = ["bonsai-2:4b"]
    requests = []          # every /api/chat body received
    queries = ["water purification", "boiling water"]
    answer = ["Boil ", "the water ", "for one minute [1].\n\n", "1. Filter\n\n", "2. Boil\n"]
    no_thinking = False    # behave like a model that can't "think"
    pulled = []

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/version":
            return self._json({"version": "0.0.0"})
        if self.path == "/api/tags":
            return self._json({"models": [{"name": m} for m in self.models]})
        self._json({}, 404)

    def do_POST(self):
        cls = type(self)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/pull":
            cls.pulled.append(body["model"])
            self.send_response(200)
            self.end_headers()
            if body["model"].startswith("missing"):
                self.wfile.write(json.dumps({"error": "pull model manifest: file does not exist"}).encode() + b"\n")
                return
            for done in (10, 50, 100):
                self.wfile.write(json.dumps({"status": "pulling", "total": 100, "completed": done}).encode() + b"\n")
            self.wfile.write(b'{"status": "success"}\n')
            if body["model"] not in cls.models:
                cls.models.append(body["model"])
            return
        if self.path != "/api/chat":
            return self._json({}, 404)
        cls.requests.append(body)
        if body["model"] not in cls.models:
            return self._json({"error": f"model '{body['model']}' not found"}, 404)
        if body.get("think") and cls.no_thinking:
            return self._json({"error": f"\"{body['model']}\" does not support thinking"}, 400)
        if body.get("format"):
            return self._json({"message": {"role": "assistant", "content": json.dumps({"queries": cls.queries})}})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        if body.get("think"):
            self.wfile.write(json.dumps({"message": {"thinking": "Let me think."}}).encode() + b"\n")
        for tok in cls.answer:
            self.wfile.write(json.dumps({"message": {"content": tok}}).encode() + b"\n")
            self.wfile.flush()
        self.wfile.write(json.dumps({"done": True, "eval_count": 12, "eval_duration": 2_000_000_000}).encode() + b"\n")


class FakeKiwix(BaseHTTPRequestHandler):
    """Serves /zim/<dir>/ listings, files (with resume support) and .meta4 files."""
    files = {}             # "dir/name.zim" -> bytes
    corrupt_once = set()   # paths served with wrong bytes the first time
    fail_meta = set()
    hits = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = type(self)
        path = self.path.split("?")[0]
        cls.hits.append((path, self.headers.get("Range")))
        if not path.startswith("/zim/"):
            return self.send_error(404)
        rest = path[len("/zim/"):]
        if rest.endswith("/"):
            d = rest.rstrip("/")
            rows = ['<html><body><h1>Index of /zim/%s/</h1><hr><pre><a href="../">../</a>' % d]
            for key, data in sorted(cls.files.items()):
                fd, name = key.split("/")
                if fd == d:
                    size = len(data)
                    shown = f"{size / 1000:.0f}K" if size >= 1000 else str(size)
                    rows.append(f'<a href="{name}">{name}</a>{" " * 20}2026-01-15 10:30  {shown}')
            body = ("\n".join(rows) + "</pre><hr></body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if rest.endswith(".meta4"):
            key = rest[:-len(".meta4")]
            if key not in cls.files or key in cls.fail_meta:
                return self.send_error(404)
            data = cls.files[key]
            body = (f'<metalink><file name="{key.split("/")[1]}"><size>{len(data)}</size>'
                    f'<hash type="sha-1">x</hash><hash type="sha-256">{hashlib.sha256(data).hexdigest()}</hash>'
                    '</file></metalink>').encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if rest not in cls.files:
            return self.send_error(404)
        data = cls.files[rest]
        if rest in cls.corrupt_once:
            cls.corrupt_once.discard(rest)
            data = b"X" * len(data)
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            end = rng.split("-")[1]
            data = data[start:int(end) + 1] if end else data[start:]
            if start >= len(cls.files[rest]):
                return self.send_error(416)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{start + len(data) - 1}/{len(cls.files[rest])}")
        else:
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
