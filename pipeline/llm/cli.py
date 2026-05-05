"""Single subprocess wrapper around the `claude` CLI for in-pipeline LLM calls.

The pipeline is local-first; we don't want an Anthropic SDK dependency or
an API key in the environment. The user already has Claude Code installed
and authenticated, so every LLM-authored stage (rewrite, cast, per-beat
prompts, post-render critique) shells out to `claude -p`.

Defaults aim for utility-call shape: fast, no tool use, no session
persistence, structured JSON envelope, Haiku unless overridden.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .. import telemetry as _tlm


CLAUDE_BIN = "claude"

# Conservative budget cap per call. Pipeline now defaults to opus
# everywhere (see _DEFAULT_MODEL_BY_STAGE), where output tokens are
# pricier; the cap is bumped accordingly. The CLI bills via OAuth on
# Pro/Max plans, so this is mostly a runaway-protection guard rather
# than a true cost lever.
DEFAULT_BUDGET_USD = 2.00

# Bumped from 300s — opus on long narrations + the rewrite prompt's
# big SPICY/PROSODY block can take 5-7 min. Per-call timeouts can
# still be set lower at the call site for fast stages (cast, prompts).
DEFAULT_TIMEOUT_S = 600

# Strip ANSI colour codes that might leak into stderr-mingled output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class ClaudeCLIError(RuntimeError):
    """The `claude` CLI exited non-zero or returned an error envelope."""


# Per-stage model defaults — single source of truth so tuning the
# cost/quality balance is one edit.
#
# All stages default to OPUS. Earlier mixed-tier configs (haiku for
# cast, sonnet for rewrite/prompts) produced enough validator-tripping
# narrations and shallow image prompts that the time saved on the
# small models was paid back in retries and class-of-bug critic
# corrections downstream. With OAuth-billed Pro/Max plans, switching
# to opus everywhere is effectively a flat-rate upgrade — the time
# cost is real (~30-60% slower per stage) but quality per click is
# materially better and rules adherence on the rewrite SPICY/PROSODY
# directives is much more reliable.
#
# Override at runtime per-stage via the env var YTFACTORY_MODEL_<STAGE>
# (e.g. ``YTFACTORY_MODEL_REWRITE=sonnet`` for fast iteration on
# everything except the stage you're tuning, or ``=haiku`` to dial
# down a stage that doesn't need top-tier judgment for your use case).
_DEFAULT_MODEL_BY_STAGE: dict[str, str] = {
    "cast": "opus",
    "rewrite": "opus",
    "prompts": "opus",
    "critic": "opus",
    "audio_critic": "opus",
    "imitate_analyze": "opus",
    "imitate_apply": "opus",
}


def model_for(stage: str) -> str:
    """Return the claude CLI model alias for a pipeline stage."""
    env_key = f"YTFACTORY_MODEL_{stage.upper()}"
    return os.environ.get(env_key) or _DEFAULT_MODEL_BY_STAGE.get(stage, "opus")


def call_claude_cli(
    prompt: str,
    *,
    output_json: bool = True,
    json_schema: dict | None = None,
    add_dirs: list[Path] | None = None,
    allowed_tools: list[str] | None = None,
    model: str = "haiku",
    timeout_s: int = DEFAULT_TIMEOUT_S,
    budget_usd: float = DEFAULT_BUDGET_USD,
) -> str | dict:
    """Run `claude -p <prompt>` and return either the raw text response
    or a parsed JSON object (if ``output_json``).

    By default we run in ``--bare`` mode (no hooks, no CLAUDE.md auto-load,
    no skills) with all tools disabled — these are pure text-in/text-out
    utility calls. Pass ``allowed_tools=["Read", "Write"]`` if a specific
    stage genuinely needs filesystem access (e.g. critic reading frames).

    Args:
        prompt: The user message.
        output_json: Parse the response as JSON. The CLI's outer envelope
            (``--output-format json``) is always parsed; ``output_json``
            controls whether the inner ``result`` field is then JSON-decoded.
        json_schema: Optional JSON Schema passed to ``--json-schema`` for
            structured-output validation. Only meaningful with output_json.
        add_dirs: Directories to expose to file-aware tools. Implies tool
            use; pair with ``allowed_tools``.
        allowed_tools: Whitelist of built-in tool names (e.g. ["Read"]).
            ``None`` disables all tools (default for pure text calls).
        model: ``haiku`` (default — cheap, fast) or ``sonnet`` / ``opus``.
        timeout_s: Subprocess timeout.
        budget_usd: Hard cost cap.

    Returns:
        ``dict`` if ``output_json`` else ``str`` — the model's response,
        with the CLI envelope already stripped.

    Raises:
        ClaudeCLIError on non-zero exit or envelope ``is_error: true``.
    """
    cmd: list[str] = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--output-format", "json",
        "--model", model,
        "--no-session-persistence",
        "--max-budget-usd", str(budget_usd),
    ]

    # We can't use --bare here: it bypasses the OAuth keychain and
    # requires ANTHROPIC_API_KEY, which the user doesn't have set
    # (Claude Pro/Max OAuth login). The --setting-sources user-only
    # combo still skips project CLAUDE.md / hooks while letting auth
    # work via the keychain.
    cmd.extend(["--setting-sources", "user"])
    cmd.append("--disable-slash-commands")

    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
    else:
        # Disable ALL tools when the caller doesn't explicitly opt in.
        cmd.extend(["--tools", ""])

    if add_dirs:
        cmd.append("--add-dir")
        cmd.extend(str(p) for p in add_dirs)

    if json_schema is not None:
        cmd.extend(["--json-schema", json.dumps(json_schema)])

    job_id = os.environ.get("YTFACTORY_JOB_ID") or None
    tlm_meta = {
        "model": model,
        "prompt_chars": len(prompt),
        "schema": json_schema is not None,
        "tools": list(allowed_tools or []),
    }
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        _tlm.track("llm_call", category="llm", success=False,
                   duration_ms=int((time.time() - t0) * 1000),
                   job_id=job_id,
                   metadata={**tlm_meta, "error": f"timeout {timeout_s}s"})
        raise ClaudeCLIError(f"claude CLI timed out after {timeout_s}s") from e

    stdout = _ANSI_RE.sub("", proc.stdout or "")
    stderr = _ANSI_RE.sub("", proc.stderr or "")

    if proc.returncode != 0:
        _tlm.track("llm_call", category="llm", success=False,
                   duration_ms=int((time.time() - t0) * 1000),
                   job_id=job_id,
                   metadata={**tlm_meta, "error": f"rc={proc.returncode}",
                             "stderr_head": stderr[:200]})
        raise ClaudeCLIError(
            f"claude CLI exited {proc.returncode}\n"
            f"stderr: {stderr[:500]}\nstdout: {stdout[:500]}"
        )

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ClaudeCLIError(
            f"claude CLI returned non-JSON output:\n{stdout[:1000]}"
        ) from e

    if envelope.get("is_error"):
        _tlm.track("llm_call", category="llm", success=False,
                   duration_ms=int((time.time() - t0) * 1000),
                   job_id=job_id,
                   metadata={**tlm_meta,
                             "error": str(envelope.get("result") or "envelope")[:200]})
        raise ClaudeCLIError(
            f"claude CLI error envelope: {envelope.get('result') or envelope}"
        )

    # Record success once we know the call returned cleanly. Token + cost
    # fields come from the CLI envelope when present (it surfaces them as
    # `usage.input_tokens` / `usage.output_tokens` / `total_cost_usd`).
    usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    _tlm.track(
        "llm_call",
        category="llm",
        success=True,
        duration_ms=int((time.time() - t0) * 1000),
        job_id=job_id,
        metadata={
            **tlm_meta,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cost_usd": envelope.get("total_cost_usd"),
        },
    )

    # When `--json-schema` is passed, the CLI parses + validates the model's
    # output and puts the resulting object in `structured_output`; `result`
    # stays empty in that mode. Prefer it when present.
    if output_json and json_schema is not None:
        structured = envelope.get("structured_output")
        if isinstance(structured, (dict, list)):
            return structured
        # Fall through to result parsing if the CLI didn't populate it.

    result_text = envelope.get("result")
    if result_text is None:
        raise ClaudeCLIError(f"claude CLI envelope missing 'result': {envelope}")

    if not output_json:
        return result_text

    # Parse the inner JSON. Tolerate models that wrap output in ```json fences.
    return _parse_inner_json(result_text)


def _parse_inner_json(text: str) -> dict | list:
    """Extract a JSON object/array from a model response, tolerant of
    markdown fences and surrounding chatter.
    """
    s = text.strip()

    # Strip ```json ... ``` fences if present.
    if s.startswith("```"):
        # remove leading fence
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        # remove trailing fence
        s = re.sub(r"\s*```\s*$", "", s)

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Last resort: find the first {...} or [...] block.
    for opener, closer in (("{", "}"), ("[", "]")):
        i = s.find(opener)
        if i == -1:
            continue
        depth = 0
        for j in range(i, len(s)):
            if s[j] == opener:
                depth += 1
            elif s[j] == closer:
                depth -= 1
                if depth == 0:
                    candidate = s[i : j + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break

    raise ClaudeCLIError(
        f"could not parse JSON from model output:\n{text[:1000]}"
    )
