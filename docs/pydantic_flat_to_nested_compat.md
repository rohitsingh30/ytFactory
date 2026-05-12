# Pydantic flat→nested model refactors need symmetric compat

> **Established 2026-05-11** during the NicheDoc schema canonicalisation
> follow-up. The flat→nested refactor shipped with write-side compat
> only; the resulting `AttributeError` on the read path 502'd
> `/api/discover/{channel}` for every cloud user.
>
> **2026-05-12 update:** the `source` sub-spec was actually shipped on
> NicheDoc this date (commit `c4bf4fc`) — see "How far this is shipped"
> below. The other sub-specs documented as the canonical example
> (`routing`, `templates`, `aesthetic`) are still planned but NOT on
> the model. The whole-doc-aspirational claim was stale; this page
> now distinguishes shipped from planned.

## TL;DR

When a pydantic model moves previously-top-level fields into nested
sub-specs, you need **two** forms of backward compat:

| direction | mechanism | purpose |
|---|---|---|
| WRITE (input → model)   | `@model_validator(mode="before")` that hoists flat keys into sub-spec dicts | old-shape JSONs / API requests still load |
| READ (model → consumers) | `@property` shims on the parent that delegate to the nested location, OR keep the flat fields on the model and mirror nested ↔ flat in the validator | old-shape attribute access still resolves |

Both must ship in the **same commit**. Asymmetric compat is the trap
this doc exists to prevent.

The 2026-05-12 `source` ship chose the **mirror** path rather than
read-side `@property` shims: the flat fields stay on the model and
the before-validator copies values both ways. This avoids the
`AttributeError` failure mode entirely (the flat attribute always
exists) at the cost of two storage locations for the same data.

## How far this is shipped (2026-05-12)

Despite this doc's earlier wording, only ONE of the four nested
sub-specs is actually live on the `NicheDoc` model:

| sub-spec     | shipped? | mechanism                             | notes |
|--------------|----------|---------------------------------------|-------|
| `source`     | ✓ 2026-05-12 (`c4bf4fc`) | nested `NicheSource` + `_migrate_source_shape` validator that mirrors nested ↔ flat | flat `source_kind` / `source_ref` kept on the model; both populated regardless of input shape |
| `routing`    | ✗ planned | the GCS JSONs already ship `routing: {channel, state_subdir, variant_yaml}` but the model drops the key via `extra: ignore`. `routing.channel` is currently inferred from the file path by `_parse_doc_with_channel_inject` |
| `templates`  | ✗ planned | the GCS JSONs ship `templates: {prompt_style_guide, hook, closer}` but the model only carries flat `prompt_style_guide` / `hook_template` / `closer_template`; nested keys silently dropped on load |
| `aesthetic`  | ✗ planned | the GCS JSONs ship `aesthetic: {image_style, music_bed, thumbnail_style_key}` but the model only carries flat `image_style` / `music_bed`; `thumbnail_style_key` silently dropped on load |

When the next sub-spec ships, replicate the `source` pattern:
1. Add a nested Pydantic model (`NicheRouting` / `NicheTemplates` /
   `NicheAesthetic`).
2. Add the field to NicheDoc as `Optional[NestedModel] = None`.
3. Extend the `_migrate_source_shape` validator (or write a sibling
   `_migrate_routing_shape` etc.) to mirror nested ↔ flat in both
   directions.
4. Pin the round-trip with a test: `model_validate({nested...})` →
   `model_dump_json` → `model_validate_json` should preserve every
   field.

## Worked example — `pipeline/niche_specs.py::NicheDoc::source`

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

That dict was the **plan**. As of 2026-05-12 only the first two rows
(`source_kind` / `source_ref` → `source.{kind,ref}`) are live on the
model. The other 9 rows are still aspirational — see "How far this
is shipped" above.

The shipped path for `source` is the **mirror approach**, not the
property-shim approach this doc originally described. The
`_migrate_source_shape` validator (`pipeline/niche_specs.py:153`):

```python
@model_validator(mode="before")
@classmethod
def _migrate_source_shape(cls, data):
    nested = data.get("source")
    if isinstance(nested, dict):
        # mirror nested → flat (only when kind is a known SourceKind
        # so we don't poison the strict Literal flat field)
        ...
        return data
    if data.get("source_kind") or data.get("source_ref"):
        # synthesise nested from flat for callers using the legacy
        # constructor (e.g. tests/test_niche_specs.py:54)
        data["source"] = {"kind": ..., "ref": ...}
    return data
```

Both `niche_doc.source.kind` AND `niche_doc.source_kind` resolve
correctly post-fix, regardless of which shape the input used. Round-
trip (`model_dump_json → model_validate_json`) preserves the nested
field. Verified by `tests/test_niche_specs.py::TestNicheSourceShape`.

**The trap this doc still warns about** — asymmetric write-only
compat — bit the planned-but-unshipped sub-specs. If
`routing/templates/aesthetic` ever land via property shims (the
original plan), every existing reader of `niche_doc.image_style` /
etc. continues to work; if they land via the mirror approach (the
2026-05-12 path), the flat fields stay and consumers don't change.
Pick one approach for the whole sub-spec; don't mix.

**What was missing pre-2026-05-12 for `source`:** the nested field
wasn't on the model at all. Existing code reading
`niche_doc.source_kind` got back the default `"manual"` — no
`AttributeError`, just silent wrong data. That's the failure mode
the per-sub-spec round-trip test pins; without it, the bug is
visible only end-to-end (e.g. via the user-reported
"auto-generate ignores my niche" symptom).

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
