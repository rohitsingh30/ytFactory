# Pydantic flat→nested model refactors need symmetric compat

> **Established 2026-05-11** during the NicheDoc schema canonicalisation
> follow-up. The flat→nested refactor shipped with write-side compat
> only; the resulting `AttributeError` on the read path 502'd
> `/api/discover/{channel}` for every cloud user.

## TL;DR

When a pydantic model moves previously-top-level fields into nested
sub-specs, you need **two** forms of backward compat:

| direction | mechanism | purpose |
|---|---|---|
| WRITE (input → model)   | `@model_validator(mode="before")` that hoists flat keys into sub-spec dicts | old-shape JSONs / API requests still load |
| READ (model → consumers) | `@property` shims on the parent that delegate to the nested location | old-shape attribute access still resolves |

Both must ship in the **same commit**. Asymmetric compat is the trap
this doc exists to prevent.

## Worked example — `pipeline/niche_specs.py::NicheDoc`

The 2026-05-11 refactor moved 11 fields from flat to nested:

```python
_LEGACY_FLAT_FIELDS: dict[str, tuple[str, str]] = {
    "source_kind":        ("source",    "kind"),
    "source_ref":         ("source",    "ref"),
    "channel":            ("routing",   "channel"),
    "state_subdir":       ("routing",   "state_subdir"),
    "variant_yaml":       ("routing",   "variant_yaml"),
    "prompt_style_guide": ("templates", "prompt_style_guide"),
    "hook_template":      ("templates", "hook"),
    "closer_template":    ("templates", "closer"),
    "image_style":        ("aesthetic", "image_style"),
    "music_bed":          ("aesthetic", "music_bed"),
    "thumbnail_style_key":("aesthetic", "thumbnail_style_key"),
}
```

The shipped `@model_validator(mode="before")` reshapes incoming
old-shape JSONs by walking that dict and moving each flat key into
its sub-spec.

**What was missing pre-fix:** the symmetric read shims. Existing code
like `control/routes/discover_routes.py:329` was still doing
`niche_doc.prompt_style_guide` — that attribute lookup raised
`AttributeError` because the field had moved to
`niche_doc.templates.prompt_style_guide`.

**The fix** (added 2026-05-11): one `@property` per flat name, each
delegating to the nested location:

```python
@property
def prompt_style_guide(self) -> str:
    return getattr(self.templates, "prompt_style_guide", "") or ""

@property
def hook_template(self) -> str:
    return getattr(self.templates, "hook", "") or ""

# … 9 more, paired 1:1 with _LEGACY_FLAT_FIELDS
```

Each shim returns a safe default (`""` or `None`) so an absent
sub-spec never raises.

## Why "just update the callers" isn't enough

It's tempting to grep for `niche_doc.prompt_style_guide` and edit each
call site to use the nested name instead. Three reasons not to:

1. **You'll miss callers.** Especially callers in skill SKILL.md
   examples, channel learnings, or vendored docs that don't show up
   in the standard grep.
2. **Existing on-disk JSONs need a stable read surface.** If a
   channel's `niches/<key>.json` was hand-edited last month under the
   flat shape, the validator will hoist it on read — but if any
   script reads `doc.prompt_style_guide` afterwards, it still
   crashes without the shim.
3. **The next refactor recurs.** The shim list is mechanical (one per
   `_LEGACY_FLAT_FIELDS` entry); skipping it means every future schema
   restructure replays the same 502.

## Why the unit tests didn't catch it

The pre-fix tests asserted that `NicheDoc(**data)` *could be
constructed* under both shapes. That's necessary but not sufficient:
the validator runs at construction time, but attribute reads happen
**later**, in code paths that the test fixture never reached.

The discover-route's `prompt_style_guide` access ran ONLY when:
- the user clicked "Auto-generate topic",
- a niche was selected,
- the LLM brainstorm fallback fired.

A handler-level test with a mocked `niche_doc` object had the mock
provide the attribute, so the test passed. Production didn't.

## Add this test for every flat→nested refactor

```python
def test_legacy_flat_attribute_reads_resolve_via_shims():
    """Legacy code that still does ``doc.<flat_name>`` must continue
    working after the refactor. One assertion per flat field name."""
    doc = NicheDoc.model_validate({
        "key": "k", "label": "l", "description": "x" * 20,
        "length_kind": "short",
        "routing": {"channel": "ch"},
        "templates": {"prompt_style_guide": "guide", "hook": "h"},
        "aesthetic": {"image_style": "style"},
        "source": {"kind": "manual"},
        "created_by": "user",
    })
    # Each flat name must read through to the nested location.
    assert doc.prompt_style_guide == "guide"
    assert doc.hook_template == "h"
    assert doc.image_style == "style"
    assert doc.source_kind == "manual"
    assert doc.channel == "ch"
    # Empty templates must return "" or None, never raise.
    bare = NicheDoc.model_validate({
        "key": "k", "label": "l", "description": "x" * 20,
        "length_kind": "short",
        "routing": {"channel": "ch"}, "created_by": "user",
    })
    assert bare.prompt_style_guide == ""
    assert bare.music_bed is None
```

## Cross-references

- `pipeline/niche_specs.py` — the canonical implementation (`_LEGACY_FLAT_FIELDS` + write validator + 11 read shims)
- `docs/niche_schema.md` — documents the symmetric compat surface
- `feedback_dual_deploy_after_niche_schema_change.md` — companion rule (deploy ordering)
- `feedback_post_deploy_live_smoke_m2m.md` — every cloud deploy should hit the affected endpoint with auth so this class of bug surfaces in seconds, not days
