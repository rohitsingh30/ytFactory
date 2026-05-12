# Fail-loud fallback paths (cross-cutting CLASS-OF-BUG)

> **Established 2026-05-12** after the discover-route 502 audit
> revealed that the LLM brainstorm fallback had been silently broken
> on prod for an unknown duration — `_llm_topic_items` had FOUR
> distinct return-empty paths with zero log output, so the actual
> root cause (`gpt-5.3-chat` rejecting `max_tokens`) was invisible.
> The user's "this always fucking fails" complaint surfaced both the
> primary bug AND the silent-fallback masking pattern in the same
> debugging session.

## The rule

**Every fallback path that swallows an upstream failure MUST log at
WARNING level**, even if the caller treats the empty return as "no
data, just skip me". Returning `[]` / `None` / a default without a
log is how a fully-broken upstream hides as "well-behaved degrade"
for weeks.

This applies to:

- `try: foo() except Exception: return []` patterns
- "If the response doesn't look right, return empty" client-side
  validators
- `if not result.get("items"): return []` short-circuits
- "All items got filtered, but no items remain" silent drops
- Any `_safe_<source>_<verb>(...)` wrapper that catches a failure
  and degrades

The 4 failure modes that motivated this rule (all in
`control/routes/discover_routes.py::_llm_topic_items`):

1. `call_llm` raised an exception → was logged at INFO (silent in
   most prod log queries, which filter at WARNING+).
2. Result was a string that didn't parse as JSON → silently returned `[]`.
3. Result was a dict but `items` was missing / not a list → silently `[]`.
4. `items` parsed but every entry was malformed (no `topic` field) → silently `[]`.

Every one of these fired in the wild during the 2026-05-12 outage, and
every one was invisible until the rule landed. After the fix, the FIRST
warning line in the next log scan named the actual cause.

## How to apply

When writing or reviewing a fallback path:

| code shape | bad | good |
|---|---|---|
| Catch + degrade | `except Exception: return []` | `except Exception as e: logger.warning("X failed: %s", e, exc_info=True); return []` |
| Defensive empty | `if not result: return []` | `if not result: logger.warning("X returned empty result"); return []` |
| Schema mismatch | `if "items" not in r: return []` | `if "items" not in r: logger.warning("X missing 'items' field, got keys=%s", list(r.keys())); return []` |
| All-filtered drop | `if all_dropped: return []` | `if all_dropped: logger.warning("X returned %d items but all were dropped (sample: %r)", len(raw), raw[0]); return []` |
| Bumped from INFO → WARNING | (was INFO) | WARNING. Cloud Logging dashboards filter at WARNING+ by default; INFO is invisible to operations. |

**Include enough context in the log line to diagnose without re-running.**
Bad: `"LLM brainstorm failed"`. Good: `"LLM brainstorm raised for
mystoriesanimated (variant=None niche=None): <Azure error string>"`.
The variant + niche tell the next reader which call path to bisect;
the error string tells them what Azure actually said.

## Why returning `[]` silently is so dangerous

Cascading fallbacks compound this. The discover route was:

```
native_items = _safe_native_items_for(channel)   # catches Reddit 403, returns []
llm_items    = _llm_topic_items(channel)         # catches Azure error, returns []
if not native_items and not llm_items:
    raise HTTPException(502, "no auto-generate source produced topics")
```

When BOTH legs returned `[]` silently, the user got a 502 with no
upstream cause in the logs. The Reddit failure was loud (the
`_safe_native_items_for` wrapper logged at WARNING with
`exc_info=True`); the LLM failure was silent. With one quiet leg, the
operator can't tell whether the 502 is "Reddit was down + LLM is up
(intermittent)" or "Reddit was down + LLM is broken (chronic)" —
which is exactly the difference between "wait 5 min" and "open an
incident".

## Pin

`tests/test_routes_discover.py::TestLlmTopicItems` —
`test_failure_path_logs_at_warning_level`,
`test_non_json_string_logs_warning`,
`test_non_dict_result_logs_warning`,
`test_items_field_not_a_list_logs_warning`,
`test_empty_items_array_logs_warning`,
`test_all_items_discarded_logs_warning`. Each test uses
`assertLogs(... level="WARNING")` so the regression is caught the
moment a future refactor downgrades the warning back to INFO.

Reference fix: commit `e600e22` (2026-05-12) bumped 4 silent paths
in `_llm_topic_items` and added 6 regression tests.

## Sweep recipe

When adding a new fallback path or reviewing an existing one:

```bash
# 1. Find every "catch + return empty" in pipeline / control code:
grep -rnE "except [A-Za-z_]+( as [a-z]+)?:\s*$" pipeline/ control/ --include='*.py' -A 1 \
  | grep -B 1 -E "return \[\]|return None|return \"\"|return \{\}"
#
# Each hit is a candidate — does the except block log? If not,
# upgrade to WARNING with context.

# 2. Find every silent short-circuit on a missing/empty field:
grep -rnE "if not [a-z_.()\[\]\"']+:\s*$" pipeline/ control/ --include='*.py' -A 1 \
  | grep -B 1 -E "return \[\]|return None"
```

Each match is a candidate for the rule.

## See also

- `pipeline/sources/reddit_api.py::_safe_native_items_for` (the
  reference catch-and-degrade wrapper that DOES log at WARNING with
  `exc_info=True` — the pattern to copy).
- `control/routes/discover_routes.py::_llm_topic_items` (post-fix —
  the 4 added warnings).
- `feedback_doc_aspirational_claims.md` — sibling meta-pattern (a
  silent fallback can hide an aspirational doc claim from being
  caught by smoke tests).
- `feedback_fail_loud_fallback_paths.md` — terse memory pointer.
