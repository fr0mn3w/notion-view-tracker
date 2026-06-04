"""Persist rotated OAuth tokens back into GitHub Actions secrets.

X rotates the refresh token on every use, so a stateless Actions run MUST write the new
token back or the next run can't authenticate. Requires a fine-grained PAT with
'Secrets' read+write permission on this repo, supplied as the GH_PAT env var.
"""
import logging
from base64 import b64encode

import requests

log = logging.getLogger(__name__)
GH_API = "https://api.github.com"


def _encrypt(public_key, value):
    # Lazy import so environments without PyNaCl can still import this module.
    from nacl import encoding, public

    pk = public.PublicKey(public_key.encode(), encoding.Base64Encoder())
    return b64encode(public.SealedBox(pk).encrypt(value.encode())).decode()


def update_repo_secret(repo, name, value, pat, session=None):
    """Upsert one repository Actions secret (libsodium sealed-box encrypted). Raises on failure."""
    session = session or requests.Session()
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    pk = session.get(
        f"{GH_API}/repos/{repo}/actions/secrets/public-key", headers=headers, timeout=30
    )
    pk.raise_for_status()
    pkj = pk.json()
    resp = session.put(
        f"{GH_API}/repos/{repo}/actions/secrets/{name}",
        headers=headers,
        json={"encrypted_value": _encrypt(pkj["key"], value), "key_id": pkj["key_id"]},
        timeout=30,
    )
    resp.raise_for_status()
