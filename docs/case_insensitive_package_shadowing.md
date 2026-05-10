# Case-insensitive package vs flat-module shadowing (APFS pitfall)

**Established 2026-05-10** after `pipeline/audio.py` (the canonical
TTS dispatcher facade) was silently shadowed by an empty
`pipeline/audio/` package on macOS APFS, breaking every renderer that
did `from pipeline.audio import audio` (or any other public symbol).

## The rule

A flat module `pipeline/X.py` and a package `pipeline/X/` MUST NOT
coexist in the repo. On case-insensitive filesystems (default macOS
APFS, default NTFS) Python's import machinery picks the **package**
deterministically and the flat module's contents become invisible —
even though both files are committed and pass `git ls-files`.

The bug is silent: imports succeed (the empty package's `__init__.py`
is non-empty in this case, just doesn't re-export the symbol), and
the failure mode is `ImportError: cannot import name 'X' from
'pipeline.audio'` at the call site, which looks like a code-level
typo, not a layout-level shadowing.

## What went wrong (2026-05-10)

`pipeline/audio.py` (1873 lines, the actual TTS dispatcher facade)
existed alongside `pipeline/audio/__init__.py` (a single empty file
left over from a refactor that never finished). Test logs showed:

```
E   ImportError: cannot import name 'audio' from 'pipeline.audio'
    (/Users/rohit/ytFactory/pipeline/audio/__init__.py)
```

— note the path: Python resolved `pipeline.audio` to the **package**
(`pipeline/audio/__init__.py`), never the flat module. Same pattern
hit:

- `pipeline/images.py` (1339 lines) shadowed by `pipeline/images/`
- `pipeline/footage.py` originally — caught and fixed in earlier work
- `pipeline/upload.py` originally — same
- `pipeline/x_upload.py` originally — same

The cleanup commit `a6c4fe1` deleted the flat `footage.py`,
`upload.py`, `x_upload.py` files but left `audio.py` and `images.py`
behind because the package versions were empty placeholders awaiting
the move.

## The fix

Three patterns; pick by complexity of the flat file:

1. **Move the content into the package's `__init__.py`** — for the
   audio facade case (the flat file IS the public re-export surface,
   no submodules yet):

   ```bash
   mv pipeline/audio.py pipeline/audio/__init__.py
   ```

   And add submodule re-export shims for any tests/callers that
   import the package-style path:

   ```python
   # pipeline/audio/asr.py — shim onto pipeline.asr
   import pipeline.asr as _mod
   import sys as _sys
   _sys.modules[__name__] = _mod
   ```

2. **Delete the flat file** — when the package version already
   contains the canonical implementation. Confirm with
   `wc -l` on both first.

3. **Promote the flat file to a package** — when the flat file has
   accumulated enough surface area that it should be split. Use
   `git mv` so per-file history is preserved. Re-export public
   symbols from `__init__.py` so the public import surface is
   unchanged (see `feedback_pipeline_subpackages_split.md` for the
   established split pattern; PEP 562 `__getattr__` re-exports for
   `patch.object` mockability).

## Audit recipes

**Find every coexisting pair right now:**

```bash
cd /Users/rohit/ytFactory
find pipeline/ -maxdepth 2 -name "*.py" -not -path "*/__pycache__/*" \
  | sed 's|\.py$||' | sort -u > /tmp/files.txt
find pipeline/ -maxdepth 2 -mindepth 2 -type d -not -path "*/__pycache__/*" \
  | sort -u > /tmp/dirs.txt
comm -12 /tmp/files.txt /tmp/dirs.txt
# any output is a shadowing pair — fix immediately
```

Also worth running across `control/`, `web/`, `cloud/<svc>/`:

```bash
for root in control web cloud; do
  find $root -maxdepth 3 -name "*.py" -not -path "*/__pycache__/*" \
    | sed 's|\.py$||' | sort -u > /tmp/files.txt
  find $root -maxdepth 3 -mindepth 2 -type d -not -path "*/__pycache__/*" \
    | sort -u > /tmp/dirs.txt
  echo "=== $root ==="
  comm -12 /tmp/files.txt /tmp/dirs.txt
done
```

**Catch the regression at PR time** — add to a CI lint or pre-commit
hook:

```bash
duplicates=$(find pipeline control web cloud -maxdepth 3 \
  \( -name "*.py" -o -type d \) -not -path "*/__pycache__/*" \
  | grep -v __init__.py | sed -E 's|\.py$||;s|/$||' | sort | uniq -d)
if [ -n "$duplicates" ]; then
  echo "❌ flat-module/package shadowing pairs detected:"
  echo "$duplicates"
  exit 1
fi
```

## Why APFS makes this worse than Linux

On a case-sensitive Linux filesystem, `pipeline/Audio.py` and
`pipeline/audio/` would be distinct paths and `import pipeline.audio`
deterministically resolves to the package on both OSes — no surprise.
On APFS (case-insensitive default), `pipeline/audio.py` and
`pipeline/audio/` *appear* distinct in `git ls-files` (which preserves
case) but resolve to the **same path** at the OS layer, so Python's
finder sees both candidates and picks the package by spec
precedence. The mac dev sees a silent shadow; the Linux Cloud Run
worker sees the same precedence rule but the layout was rare enough
that the bug never surfaced there until cross-platform divergence.

If you're on Linux, the bug looks like "Cloud Run worker imports
package version, my laptop imports flat version, behaviour diverges
with no diff in git" — same root cause, different surface.

## See also

- `feedback_apfs_package_shadowing.md` — memory entry mirroring this.
- `feedback_pipeline_subpackages_split.md` — the canonical split
  pattern when promoting a flat file to a package.
- `pipeline/audio/__init__.py` — example of "moved flat content into
  package" with submodule shims.
- `docs/structural_cleanup_2026_05_05.md` — the broader pipeline/
  layout cleanup that left the half-finished audio + images
  shadowing pairs behind.
