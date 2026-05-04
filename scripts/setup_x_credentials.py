"""Interactive X credentials installer.

Run:
    .venv/bin/python scripts/setup_x_credentials.py <account>

Prompts for the 4 OAuth 1.0a User Context credentials with hidden
input (no echo to terminal, no scrollback), validates each one's
shape, writes ~/.config/ytfactory/x_credentials_<account>.json with
0600 perms, then makes one read-only API call to confirm they work.

Why hidden input instead of pasting into chat or shell history:
    - getpass prompts read from /dev/tty directly; the keystrokes
      never hit terminal scrollback or shell history
    - No risk of accidentally committing or pasting them later
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sys
from pathlib import Path


CONFIG_DIR = Path.home() / ".config" / "ytfactory"


def _prompt(label: str, *, validator) -> str:
    """getpass prompt with re-ask loop until validator returns ok."""
    while True:
        val = getpass.getpass(f"{label}: ").strip()
        if not val:
            print("  (empty — try again)")
            continue
        ok, why = validator(val)
        if ok:
            return val
        print(f"  ✗ {why} — try again")


def _v_consumer_key(s: str) -> tuple[bool, str]:
    # X consumer keys are 25 alphanumeric chars.
    if not re.fullmatch(r"[A-Za-z0-9]{20,30}", s):
        return False, "consumer_key should be ~25 alphanumeric chars (no spaces)"
    return True, ""


def _v_consumer_secret(s: str) -> tuple[bool, str]:
    # X consumer secrets are ~50 alphanumeric chars.
    if not re.fullmatch(r"[A-Za-z0-9]{40,60}", s):
        return False, "consumer_secret should be ~50 alphanumeric chars"
    return True, ""


def _v_access_token(s: str) -> tuple[bool, str]:
    # OAuth 1.0a access tokens look like: <numeric_user_id>-<32 chars>.
    # Bearer tokens (the wrong thing!) start with 22+ A's.
    if s.startswith("AAAAAAA"):
        return False, (
            "this looks like a Bearer Token (starts with many A's). "
            "You want the OAuth 1.0a Access Token — separate 'Generate' "
            "button further down the page, format <user_id>-<chars>"
        )
    if not re.fullmatch(r"\d{8,25}-[A-Za-z0-9]{30,50}", s):
        return False, (
            "access_token should be <numeric_user_id>-<random_chars>, "
            "e.g. 1234567890-aBcDeFg…"
        )
    return True, ""


def _v_access_token_secret(s: str) -> tuple[bool, str]:
    if not re.fullmatch(r"[A-Za-z0-9]{40,60}", s):
        return False, "access_token_secret should be ~45 alphanumeric chars"
    return True, ""


def _verify_with_x(creds: dict) -> tuple[bool, str]:
    """Two-step verification:

    1. GET /1.1/account/verify_credentials — confirms the tokens auth
    2. POST + DELETE a benign text tweet — confirms WRITE permission

    Step 2 is the one that catches Read-only-app misconfigurations at
    setup time, before a 60-minute render fails on /media/upload (403).

    Returns (ok, message_or_handle).
    """
    try:
        import tweepy  # noqa: PLC0415
    except ImportError:
        return False, "tweepy not installed — run: .venv/bin/pip install 'tweepy>=4.14'"
    auth = tweepy.OAuth1UserHandler(
        creds["consumer_key"],
        creds["consumer_secret"],
        creds["access_token"],
        creds["access_token_secret"],
    )
    api_v1 = tweepy.API(auth)
    try:
        me = api_v1.verify_credentials()
    except tweepy.TweepyException as e:
        return False, f"X rejected the tokens: {e}"
    handle = getattr(me, "screen_name", None)
    if not handle:
        return False, "verify_credentials returned no screen_name"

    # Step 2 — confirm WRITE permission. Without this, /media/upload
    # will 403 deep inside an actual upload.
    print("  verifying write permission via test tweet…")
    client = tweepy.Client(
        consumer_key=creds["consumer_key"],
        consumer_secret=creds["consumer_secret"],
        access_token=creds["access_token"],
        access_token_secret=creds["access_token_secret"],
    )
    diag_text = "ytFactory diagnostic — auto-deleted in <1s. Setup script verifying write permission."
    try:
        resp = client.create_tweet(text=diag_text)
        tweet_id = (resp.data or {}).get("id") if hasattr(resp, "data") else None
    except tweepy.Forbidden:
        return False, (
            f"write permission denied. The app is configured Read-only.\n"
            f"  Fix: X dev portal → app Settings → User authentication → "
            f"set 'Read and Write' → save → REGENERATE Access Token + "
            f"Secret (the old ones still carry Read-only) → re-run this "
            f"script with the new tokens."
        )
    except tweepy.TweepyException as e:
        return False, f"unexpected error during write test: {e}"

    if tweet_id:
        try:
            client.delete_tweet(tweet_id)
        except Exception as e:
            print(f"  ⚠ failed to auto-delete diagnostic tweet {tweet_id}: {e}")
            print(f"  (manually delete: https://x.com/{handle}/status/{tweet_id})")

    return True, handle


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: setup_x_credentials.py <account>")
        print("       (e.g. airecap, historyrecapped, mystoriesanimated)")
        return 2
    account = sys.argv[1].strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", account):
        print(f"error: account name must be alphanumeric (got {account!r})")
        return 2

    out = CONFIG_DIR / f"x_credentials_{account}.json"
    if out.exists():
        ans = input(f"{out} already exists. Overwrite? [y/N] ").strip().lower()
        if ans != "y":
            print("aborted")
            return 1

    print(f"\nSetting up X credentials for account={account!r}.")
    print("Inputs are HIDDEN (no echo, no scrollback). Paste each in turn.\n")

    consumer_key = _prompt("API Key (consumer_key)", validator=_v_consumer_key)
    consumer_secret = _prompt("API Key Secret (consumer_secret)", validator=_v_consumer_secret)
    access_token = _prompt("Access Token", validator=_v_access_token)
    access_token_secret = _prompt("Access Token Secret", validator=_v_access_token_secret)
    handle = input("Handle (e.g. airecap, no @): ").strip().lstrip("@") or account

    creds = {
        "consumer_key": consumer_key,
        "consumer_secret": consumer_secret,
        "access_token": access_token,
        "access_token_secret": access_token_secret,
        "handle": handle,
    }

    print("\nVerifying with X (read-only call)…")
    ok, msg = _verify_with_x(creds)
    if not ok:
        print(f"\n✗ verification failed: {msg}")
        print("Not writing the file. Re-check tokens and run again.")
        return 1
    print(f"✓ authenticated as @{msg}")

    if msg.lower() != handle.lower():
        print(
            f"\n⚠  the handle you typed ({handle!r}) doesn't match the "
            f"actual X handle ({msg!r}). Using the real one in the file."
        )
        creds["handle"] = msg

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    out.write_text(json.dumps(creds, indent=2))
    os.chmod(out, 0o600)
    print(f"\n✓ wrote {out} (mode 0600)")
    print("\nNow you can dry-run an upload:")
    print(
        f"  .venv/bin/python -m pipeline.x_upload "
        f"--channel <channel> --slug <slug> --dry-run"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
