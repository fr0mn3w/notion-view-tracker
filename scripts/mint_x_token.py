#!/usr/bin/env python3
"""Mint an X (Twitter) OAuth2 user-context access + refresh token, one account at a time.

Manual-paste flow (no local server, so no localhost / port / HTTPS headaches):
  1. Script prints an authorize URL.
  2. You open it (signed into the TARGET account) and click Authorize.
  3. The browser lands on a http://localhost:8080/callback?... page. It will probably say
     "This site can't be reached" - that is EXPECTED and totally fine. Nothing is listening
     there; we just need the URL.
  4. Copy the FULL URL from the browser's address bar and paste it back into the terminal.
  5. Script exchanges the code and prints the access + refresh tokens.

IMPORTANT: X authorization codes expire fast (~30 seconds). Paste the URL promptly after
authorizing. If it says the code is invalid/expired, just run the script again.

Prereqs:
- X app with OAuth 2.0 on, type "Web App, Automated App or Bot" (confidential client),
  Read permission, redirect URI exactly: http://localhost:8080/callback
- Environment: X_CLIENT_ID, X_CLIENT_SECRET

Usage:
    export X_CLIENT_ID=...  X_CLIENT_SECRET=...
    python3 scripts/mint_x_token.py
"""
import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = os.environ.get("X_CLIENT_ID")
CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET")
REDIRECT_URI = "http://localhost:8080/callback"
SCOPES = "tweet.read users.read offline.access"
AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"


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

    print("\n1) Make sure the TARGET account is the one logged into x.com.")
    print("   Then open this URL in your browser and click Authorize:\n")
    print(auth_url + "\n")
    print("2) The browser will jump to a 'localhost' page that probably says")
    print('   "This site can\'t be reached" - that is EXPECTED. Nothing runs there.')
    print("3) Copy the FULL address from the browser's address bar")
    print("   (it starts with http://localhost:8080/callback?code=...) and paste it below.")
    print("   Do this promptly - the code expires in about 30 seconds.\n")

    pasted = input("Paste the full localhost URL (or just the code) here: ").strip()

    code = None
    returned_state = None
    if "code=" in pasted:
        query = urllib.parse.urlparse(pasted).query or pasted.split("?", 1)[-1]
        params = urllib.parse.parse_qs(query)
        code = (params.get("code") or [None])[0]
        returned_state = (params.get("state") or [None])[0]
    else:
        code = pasted or None

    if not code:
        sys.exit("Couldn't find an authorization code in what you pasted.")
    if returned_state and returned_state != state:
        sys.exit("State mismatch (paste came from a different run). Start over.")

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
        body = e.read().decode()
        if "invalid_request" in body or "expired" in body or e.code == 400:
            sys.exit(f"Token exchange failed ({e.code}). The code likely expired - "
                     f"just run the script again and paste faster.\n{body}")
        sys.exit(f"Token exchange failed: {e.code} {body}")

    print("\n=== TOKENS (store as GitHub Secrets, then discard this output) ===")
    print("\nACCESS_TOKEN:\n" + tokens.get("access_token", "(none)"))
    print("\nREFRESH_TOKEN:\n" + tokens.get("refresh_token", "(none)"))
    print("\nGranted scopes:", tokens.get("scope"))


if __name__ == "__main__":
    main()
