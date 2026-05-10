"""Channel state client — skills' replacement for local file I/O.

Skills used to ``Read`` / ``Edit`` / ``Write`` channel JSON files
(<channel>/narrations/<slug>.json, cast/<slug>.json, etc.) on the
laptop filesystem. The launchd ``state-sync`` plist mirrored those to
``gs://ytfactory-prod-v2-state``. Post-2026-05-09 cloud cutover, the
laptop has zero data: skills hit the cloud state API instead, the
website is the only writer, and the plist is retired.

Two surfaces:

  Library form (skills can also import this directly if running
  in the same Python process — uncommon, but cleaner than shelling
  out for tight loops):

      from pipeline.utils.state_client import get, put, list_slugs, delete
      narration = get("hindutavaanimated", "narrations", "<slug>")
      put("hindutavaanimated", "narrations", "<slug>", narration)

  CLI form (the canonical way skills bash-invoke this):

      python -m pipeline.utils.state_client get  <channel> <kind> <slug>
      python -m pipeline.utils.state_client put  <channel> <kind> <slug> < body.json
      python -m pipeline.utils.state_client list <channel> <kind>
      python -m pipeline.utils.state_client delete <channel> <kind> <slug>

Reuses ``pipeline.skill_dispatch``'s WEBSITE_URL + ID-token machinery
so skills only need one set of env vars to point at cloud or local.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

from pipeline.cloud.skill_dispatch import WEBSITE_URL, _auth_headers, WebsiteUnreachableError


class StateNotFoundError(KeyError):
    """The requested (channel, kind, slug) doesn't exist in state."""


def _url(path: str) -> str:
    return f"{WEBSITE_URL.rstrip('/')}{path}"


def _request(method: str, path: str, *, body: Any = None, timeout: float = 30) -> Any:
    url = _url(path)
    headers = dict(_auth_headers(url))
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")
        if e.code == 404:
            raise StateNotFoundError(msg) from e
        raise RuntimeError(f"{method} {url} → HTTP {e.code}: {msg}") from e
    except urllib.error.URLError as e:
        raise WebsiteUnreachableError(
            f"state API not reachable at {WEBSITE_URL}: {e}"
        ) from e


# ---------------------------------------------------------------------------
# Library API
# ---------------------------------------------------------------------------


def get(channel: str, kind: str, slug: str) -> dict[str, Any]:
    """Fetch a state JSON document. Raises StateNotFoundError on 404."""
    return _request("GET", f"/api/state/{channel}/{kind}/{slug}")


def put(channel: str, kind: str, slug: str, payload: Any) -> dict[str, Any]:
    """Write a state JSON document. Returns {ok, uri}. Overwrites blindly."""
    return _request("PUT", f"/api/state/{channel}/{kind}/{slug}", body=payload)


def delete(channel: str, kind: str, slug: str) -> dict[str, Any]:
    """Remove a state JSON document. Raises StateNotFoundError if absent."""
    return _request("DELETE", f"/api/state/{channel}/{kind}/{slug}")


def list_slugs(channel: str, kind: str) -> list[str]:
    """List slugs under <channel>/<kind>/. Returns empty list if none."""
    resp = _request("GET", f"/api/state/{channel}/{kind}")
    return resp.get("slugs", [])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_get(args: argparse.Namespace) -> int:
    try:
        data = get(args.channel, args.kind, args.slug)
    except StateNotFoundError:
        print(f"not found: {args.channel}/{args.kind}/{args.slug}", file=sys.stderr)
        return 4
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _cli_put(args: argparse.Namespace) -> int:
    if args.file:
        raw = open(args.file, encoding="utf-8").read()
    else:
        raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"input is not valid JSON: {e}", file=sys.stderr)
        return 2
    resp = put(args.channel, args.kind, args.slug, payload)
    print(resp.get("uri", "ok"))
    return 0


def _cli_delete(args: argparse.Namespace) -> int:
    try:
        delete(args.channel, args.kind, args.slug)
    except StateNotFoundError:
        print(f"not found: {args.channel}/{args.kind}/{args.slug}", file=sys.stderr)
        return 4
    print("ok")
    return 0


def _cli_list(args: argparse.Namespace) -> int:
    for slug in list_slugs(args.channel, args.kind):
        print(slug)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.utils.state_client",
        description="Read/write channel state JSON via the ytFactory website.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_get = sub.add_parser("get", help="fetch <channel>/<kind>/<slug>.json")
    p_get.add_argument("channel")
    p_get.add_argument("kind")
    p_get.add_argument("slug")
    p_get.set_defaults(fn=_cli_get)

    p_put = sub.add_parser("put", help="write <channel>/<kind>/<slug>.json")
    p_put.add_argument("channel")
    p_put.add_argument("kind")
    p_put.add_argument("slug")
    p_put.add_argument(
        "--file", "-f",
        help="read body from file (default: stdin)",
    )
    p_put.set_defaults(fn=_cli_put)

    p_del = sub.add_parser("delete", help="delete <channel>/<kind>/<slug>.json")
    p_del.add_argument("channel")
    p_del.add_argument("kind")
    p_del.add_argument("slug")
    p_del.set_defaults(fn=_cli_delete)

    p_list = sub.add_parser("list", help="list slugs under <channel>/<kind>/")
    p_list.add_argument("channel")
    p_list.add_argument("kind")
    p_list.set_defaults(fn=_cli_list)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
