# Prompt-validator drift invariant (cross-channel)

> **Established 2026-05-10** after the cake-story smoke run
> (`cake-orch-v1`) failed `script_check.missing_cta` even though the
> rewrite prompt's GOOD example phrasing was perfectly idiomatic
> English. Class of bug, not a one-off — every prompt that ships
> hand-typed examples next to a regex validator is one drift event
> away from killing the same way.

## The bug

Two independently-written sources of truth for the same rule:

- `pipeline/llm/rewrite.py::_closer_block()` taught the LLM:
  > "Tell me what you would have done."

- `pipeline/llm/script_check.py::_CTA_PATTERNS` accepted only:
  > `r"\bwhat would you (have )?(do|done)\b"` — word order
  > "what *would* you ... done", NOT "what you *would* have done".

Azure faithfully copied the prompt's GOOD example and got rejected by
the validator. The render aborted minutes later — after rewrite, cast,
TTS, ASR, beat split, and prompt authoring had already burned cost.

## The class of bug

Anywhere a prompt names a "GOOD example" of an output the validator
later regex-matches, the example and the regex MUST share one source
of truth. Hand-typing them in two places GUARANTEES drift the moment
either side ships a tweak.

This is the same shape as:

- the `closer_format` ↔ `_required_closer_tokens` drift the channel
  YAML had pre-2026-05 (fixed by the relaxed token-anchor rule)
- the cast `_lint_self_consistency` hair-token list ↔ the prompt's
  hard rules block (still drift-prone — flagged for the same fix)
- ANY future per-beat prompt that says "say X" while the validator
  regex requires `Y`

## The fix (the pattern, not the cake-story patch)

Validators expose their constraints **as data with paired examples**.
Prompts pull the examples FROM that data. By construction the two
can never disagree.

Concrete pattern, applied to CTA in `script_check.py`:

```python
@dataclass(frozen=True)
class CTARule:
    pattern: str        # the regex
    example: str        # GUARANTEED to match — invariant tested

_CTA_RULES: tuple[CTARule, ...] = (
    CTARule(r"\bam i (the )?wrong\b",                "Am I wrong here?"),
    CTARule(r"\bwhat would you (have )?(do|done)\b", "What would you have done?"),
    CTARule(r"\bwhat (you|do you) would (have )?(do|done)\b",
                                                     "Tell me what you would have done."),
    ...
)

def cta_examples(*, cliffhanger: bool = False) -> list[str]:
    """Return the GOOD-example strings for the requested CTA flavour.
    Used by RewriteContract — every returned string is guaranteed
    to satisfy the validator regex."""
    ...
```

The prompt builder calls `script_check.cta_examples()`. The validator
calls `_CTA_RE.search(text)`. Same registry, same invariant.

The invariant is **mechanically tested**:

```python
class CTARuleInvariantTest(unittest.TestCase):
    def test_every_cta_rule_example_matches_its_pattern(self):
        for r in script_check._CTA_RULES:
            pat = re.compile(r.pattern, re.IGNORECASE)
            self.assertTrue(pat.search(r.example))
```

## How to apply this rule going forward

Whenever you add a new constraint where:

1. The prompt teaches the model an "OK example" / "GOOD example" of
   some output, AND
2. A downstream validator (regex, parser, schema check) accepts /
   rejects that output

put the rule into a `(pattern, example)` registry exposed by the
validator module. Prompts pull from there. Add a unit test that
asserts `pattern.search(example)` for every entry.

Examples on the candidate list:

| Validator | Today | Next |
|---|---|---|
| `script_check._CTA_RULES` | ✅ paired | done |
| `script_check._CLIFFHANGER_CTA_RULES` | ✅ paired | done |
| `cast_lint` hair-token conflict pairs | hand-typed in prompt + lint module separately | refactor to paired registry |
| `prompt_lint` text-bait tokens | hand-typed in prompt + lint module separately | refactor to paired registry |
| `script_lint` banned-acronym list | hand-typed in 2 places | refactor |

## What surfaces this rule

When a render fails on a validator that the prompt was supposed to
prevent, the post-mortem question is always:

> "Did the prompt tell the model what the validator requires, in
> phrasing the validator will accept?"

If the answer is "yes, but the example used a different phrasing",
that's this drift.

## Related

- Orchestrator that catches this when it does happen and auto-retries:
  `docs/llm_orchestrator.md`.
- Backend dispatcher that lets the cloud worker make the LLM call at
  all: `docs/llm_backend_dispatcher.md`.
- Memory: `feedback_prompt_validator_drift_invariant.md`.
