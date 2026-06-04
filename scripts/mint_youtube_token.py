#!/usr/bin/env python3
"""Mint a YouTube (Google) OAuth access + refresh token, one channel at a time.

Manual-paste flow (no local server). Run signed into the Google account that OWNS the
target channel, so the Analytics API can read its data.

  1. Script prints an authorize URL.
  2. Open it (as the channel owner), pick the right channel/brand account, click Allow.
  3. The browser lands on a http://localhost:8080/callback?... page that will say
     "This site can't be reached" - that is EXPECTED. Copy the FULL URL from the address
     bar and paste it back here.
  4. Script exchanges the code and prints the access + refresh tokens.

Prereqs:
- A Google Cloud project with the YouTube Data API v3 and YouTube Analytics API enabled.
- An OAuth 2.0 Client ID of type "Web application", with redirect URI exactly:
      http://localhost:8080/callback
- Environment: YT_CLIENT_ID, YT_CLIENT_SECRET

Usage:
    export YT_CLIENT_ID=...  YT_CLIENT_SECRET=...
    python3 scripts/mint_youtube_token.py
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

CLIENT_ID = os.environ.get("YT_CLIENT_ID")
CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET")
REDIRECT_URI = "http://localhost:8080/callback"
SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit("Set YT_CLIENT_ID and YT_CLIENT_SECRET in the environment first.")

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
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",  # ask for a refresh token
            "prompt": "consent",       # force the refresh token to be returned
        }
    )

    print("\n1) Sign into the Google account that OWNS the target channel, then open:\n")
    print(auth_url + "\n")
    print("2) Pick the right channel/brand account and click Allow.")
    print("3) The browser lands on a 'localhost' page that probably says")
    print('   "This site can\'t be reached" - that is EXPECTED.')
    print("4) Copy the FULL address bar URL (http://localhost:8080/callback?code=...) below.\n")

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
            "client_secret": CLIENT_SECRET,
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            tokens = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Token exchange failed: {e.code} {e.read().decode()}")

    if "refresh_token" not in tokens:
        print("\nWARNING: no refresh_token returned. Revoke the app's access at")
        print("https://myaccount.google.com/permissions and run again (needs prompt=consent).")

    print("\n=== TOKENS (store as GitHub Secrets, then discard this output) ===")
    print("\nACCESS_TOKEN:\n" + tokens.get("access_token", "(none)"))
    print("\nREFRESH_TOKEN:\n" + tokens.get("refresh_token", "(none)"))
    print("\nGranted scope:", tokens.get("scope"))


if __name__ == "__main__":
    main()
