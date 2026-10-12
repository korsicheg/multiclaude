"""Tests for system/claude-usage against a local fake of the OAuth and usage endpoints."""
import http.server
import json
import os
import subprocess
import threading
import time

import pytest

from conftest import REPO

SCRIPT = REPO / "system" / "claude-usage"
USAGE = {"five_hour": {"utilization": 40.0, "resets_at": "2099-01-01T00:00:00+00:00"}}


class FakeAnthropic:
    def __init__(self):
        self.requests, self.valid_token = [], "fresh-access"
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data):
                out = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self):
                fake.requests.append(("GET", self.path, dict(self.headers), None))
                ok = self.headers.get("Authorization") == f"Bearer {fake.valid_token}"
                self.reply(200 if ok else 401, USAGE if ok else {"error": "expired"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(("POST", self.path, dict(self.headers), body))
                self.reply(200, {"access_token": fake.valid_token, "refresh_token": "next-refresh", "expires_in": 3600})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def anthropic():
    fake = FakeAnthropic()
    yield fake
    fake.server.shutdown()


def run(home, anthropic, *args):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "HOME": str(home), "CLAUDE_USAGE_URL": f"{anthropic.url}/usage",
                               "CLAUDE_USAGE_TOKEN_URL": f"{anthropic.url}/token", "no_proxy": "*"})


def credentials(home, access, expires_in):
    path = home / ".config" / "claude-usage" / "credentials.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"accessToken": access, "refreshToken": "old-refresh",
                                "expiresAt": int((time.time() + expires_in) * 1000)}))
    return path


def test_fetches_usage_with_a_valid_token(tmp_path, anthropic):
    credentials(tmp_path, "fresh-access", 3600)

    r = run(tmp_path, anthropic)

    assert r.returncode == 0 and json.loads(r.stdout) == USAGE
    assert [m for m, *_ in anthropic.requests] == ["GET"]


def test_refreshes_a_token_about_to_expire(tmp_path, anthropic):
    creds = credentials(tmp_path, "stale-access", 60)

    r = run(tmp_path, anthropic, "--cache")

    assert r.returncode == 0, r.stderr
    method, path, _, body = anthropic.requests[0]
    assert (method, path) == ("POST", "/token")
    assert body["grant_type"] == "refresh_token" and body["refresh_token"] == "old-refresh"
    saved = json.loads(creds.read_text())
    assert (saved["accessToken"], saved["refreshToken"]) == ("fresh-access", "next-refresh")
    assert json.loads((tmp_path / ".cache" / "claude-usage" / "usage.json").read_text()) == USAGE


def test_a_rejected_token_is_refreshed_once(tmp_path, anthropic):
    credentials(tmp_path, "revoked-access", 3600)

    r = run(tmp_path, anthropic)

    assert r.returncode == 0
    assert [m for m, *_ in anthropic.requests] == ["GET", "POST", "GET"]


def test_login_prints_an_authorize_url(tmp_path, anthropic):
    r = run(tmp_path, anthropic, "login")
    assert "redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback" in r.stdout
    assert (tmp_path / ".config" / "claude-usage" / "pending.json").exists()
