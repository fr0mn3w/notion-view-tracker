#!/usr/bin/env python3
"""Mint an X (Twitter) OAuth2 user-context access + refresh token, one account at a time.

Run once per team account. Sign into the TARGET account in your browser first, then run
this. It opens X's consent screen, captures the redirect on localhost, exchanges the code,
and prints the access + refresh tokens to paste into GitHub Secrets.

Prereqs:
- An X app with OAuth 2.0 enabled, type "Web App, Automated App or Bot" (confidential
  client), Read permission, and a redirect URI of exactly:
      http://localhost:8080/callback
- Environment: X_CLIENT_ID, X_CLIENT_SECRET

Usage:
    export X_CLIENT_ID=...  X_CLIENT_SECRET=...
    python scripts/mint_x_token.py
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request

CLIENT_ID = os.environ.get("X_CLIENT_ID")
CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET")
REDIRECT_URI = "http://localhost:8080/callback"
SCOPES = "tweet.read users.read offline.access"
AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"

_result = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        _result["code"] = params.get("code", [None])[0]
        _result["state"] = params.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>Got it. Close this tab and return to the terminal.</h2>")

    def log_message(self, *args):  # silence the default request logging
        pass


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit("Set X_CLIENT_ID and X_CLIENT_SECRET in the environment first.")

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(16)

    auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )

    server = http.server.HTTPServer(("localhost", 8080), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("\nMake sure you're logged into the TARGET account in your browser, then open:\n")
    print(auth_url + "\n")
    try:
        import webbrowser

        webbrowser.open(auth_url)
    except Exception:
        pass

    for _ in range(300):  # wait up to 5 minutes for the redirect
        if _result.get("code"):
            break
        time.sleep(1)
    server.shutdown()

    code = _result.get("code")
    if not code:
        sys.exit("No authorization code captured (timed out or denied).")
    if _result.get("state") != state:
        sys.exit("State mismatch; aborting.")

    data = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "client_id": CLIENT_ID,
        }
    ).encode()
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            tokens = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Token exchange failed: {e.code} {e.read().decode()}")

    print("\n=== TOKENS (store as GitHub Secrets, then discard this output) ===")
    print("\nACCESS_TOKEN:\n" + tokens.get("access_token", "(none)"))
    print("\nREFRESH_TOKEN:\n" + tokens.get("refresh_token", "(none)"))
    print("\nGranted scopes:", tokens.get("scope"))


if __name__ == "__main__":
    main()
