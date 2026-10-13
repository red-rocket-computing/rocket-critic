"""A local stand-in for the GitHub App REST endpoints, verifying the app JWT with openssl."""

import base64
import json
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class FakeGitHubAPI:
    def __init__(self, tmp: Path, app_id: str = "123456", slug: str = "rocket-critic"):
        self.key = tmp / "app.pem"
        self.pub = tmp / "app.pub.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(self.key), "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", str(self.key), "-pubout", "-out", str(self.pub)],
                       check=True, capture_output=True)
        self.app_id = app_id
        self.slug = slug
        self.installations = {"o/r": 42, "o/two": 42}
        self.tokens_issued: list[dict] = []
        self.requests: list[tuple[str, str]] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self, method):
                api.requests.append((method, self.path))
                problem = api.check_jwt(self.headers.get("Authorization", ""))
                if problem:
                    return self.reply(401, {"message": problem})
                if method == "GET" and self.path == "/app":
                    return self.reply(200, {"slug": api.slug, "id": int(api.app_id)})
                if method == "GET" and self.path.startswith("/repos/") and self.path.endswith("/installation"):
                    repo = self.path[len("/repos/"):-len("/installation")]
                    if repo in api.installations:
                        return self.reply(200, {"id": api.installations[repo]})
                    return self.reply(404, {"message": "Not Found"})
                if method == "POST" and self.path.startswith("/app/installations/"):
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    token = f"ghs_test{len(api.tokens_issued) + 1}"
                    api.tokens_issued.append({"path": self.path, "body": body, "token": token})
                    return self.reply(201, {"token": token, "expires_at": "2099-01-01T00:00:00Z"})
                return self.reply(404, {"message": "Not Found"})

            def do_GET(self):
                self.handle_any("GET")

            def do_POST(self):
                self.handle_any("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def check_jwt(self, auth: str) -> str | None:
        if not auth.startswith("Bearer "):
            return "expected a Bearer JWT"
        try:
            header, payload, sig = auth[len("Bearer "):].split(".")
        except ValueError:
            return "malformed JWT"
        if json.loads(b64url_decode(header)) != {"alg": "RS256", "typ": "JWT"}:
            return "bad JWT header"
        claims = json.loads(b64url_decode(payload))
        now = time.time()
        if claims.get("iss") != self.app_id or not claims["iat"] <= now < claims["exp"] \
                or claims["exp"] - claims["iat"] > 600:
            return f"bad JWT claims {claims}"
        with tempfile.NamedTemporaryFile() as fh:
            fh.write(b64url_decode(sig))
            fh.flush()
            res = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(self.pub), "-signature", fh.name],
                                 input=f"{header}.{payload}".encode(), capture_output=True)
        return None if res.returncode == 0 else "bad JWT signature"

    def close(self):
        self.server.shutdown()
