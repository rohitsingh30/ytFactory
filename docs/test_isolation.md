# Test isolation — never silently attach to live dev infrastructure

**Established 2026-05-09** after `tests/test_e2e_happy_path.py` polluted the
developer's live in-memory dashboard with 20 `"E2E test render"` rows in
~35 minutes.

## The rule

Tests MUST NOT default to attaching to whatever happens to be listening
on a well-known dev port (8765, 8766, etc.). Default behavior must be
**always boot an ephemeral instance on a random free port**.

If the operator wants to point a test at a live or remote target (smoke
test against staging, manual debugging against the local dev server),
that is opt-in only — via an explicit env var like `YTFACTORY_E2E_BASE`.

## What went wrong (2026-05-09)

`tests/test_e2e_happy_path.py::HappyPathE2ETest.setUpClass` did this:

```python
host = "127.0.0.1"
port = 8766                      # the canonical live-control-plane port
if _is_listening(host, port):
    cls._spawned = False         # → reuse whatever is there
    return
# else: boot our own
```

Combined with the **auto-test rule** (`docs/auto_test_rule.md` /
`CLAUDE.md` § "Auto-test rule") that fires the suite after every code
edit to `pipeline/`, `web/`, `tests/`, etc., the result was:

1. Developer had `uvicorn control.server_dev:app --port 8766` running
   (the canonical website-first production substrate per CLAUDE.md
   § "Website-first production").
2. Every code edit kicked the suite.
3. Each suite run found 8766 listening, attached, and `POST /api/render`
   with `topic="E2E test render"` `channel="historyrecapped"`.
4. After ~20 edits in ~35 min, the dashboard showed 20 pollution rows
   the developer had to ask "what is this bullshit filled with?".

The in-memory backend made this hard to clean up — no DELETE endpoint,
restart wiped all jobs (including 1 legit AITA render the dev wanted).

## The fix

`tests/test_e2e_happy_path.py` now:

- `BASE = os.environ.get("YTFACTORY_E2E_BASE", "")` — empty by default.
- `setUpClass` always allocates a free port via `_free_port()` (binds
  to `127.0.0.1:0`, returns OS-assigned port), boots its own uvicorn,
  sets `BASE` to that URL.
- Calls `control.jobs.reset_jobs()` before booting to force a fresh
  in-memory backend.
- Only skips the boot if `YTFACTORY_E2E_BASE` is explicitly set in env.

## Audit checklist for any new test that talks HTTP

When adding a test that hits any HTTP endpoint, verify ALL of:

- [ ] Default boots its own server on a random free port (not a fixed port).
- [ ] Never probes well-known dev ports (8765 web, 8766 control plane,
      anything in `.env` `*_URL` vars).
- [ ] Resets module-global singletons (jobs backend, queue backend,
      cache) before/after to avoid cross-test bleed.
- [ ] Opt-in env var for remote/live targeting, **not** auto-detection.
- [ ] Test data is recognizable — `topic="<test_name> render"` with the
      test name in it, not a generic string — so accidental pollution
      is identifiable in 2 seconds, not 5 minutes.

## Why this rule is permanent

The auto-test rule (`rule_auto_test_after_edit.md`) is good and stays
— but it amplifies any "attach if available" pattern in tests by an
order of magnitude (10-30 runs/hour during active dev). Any test that
auto-attaches to live infra becomes a destructive write multiplier,
not a single-incident bug. Fix the multiplier (test isolation), not
the trigger (auto-test rule).

## See also

- `tests/test_e2e_happy_path.py` — fixed implementation (the docstring
  links back here).
- `CLAUDE.md` § "Auto-test rule" — the trigger.
- `CLAUDE.md` § "Website-first production" — establishes 8765 (web)
  and 8766 (control plane) as canonical dev ports tests must avoid.

---

## sys.modules + parent-package-attribute pollution (2026-05-10)

Same theme — silent state leaking between tests — but on a different
axis. After the test-suite recovery on 2026-05-10 (149 → 0 failures),
a class of pollution surfaced that wasn't covered by the port-isolation
rule above: tests that monkey-patch `sys.modules["googleapiclient.*"]`
or `sys.modules["control.storage"]` etc. without restoring it leak the
fake into every subsequent test in the same process. Worse — Python's
`from package import submodule` idiom binds the submodule onto the
*package object* as an attribute, so even
`patch.dict("sys.modules", {"<pkg>.<sub>": fake})` is a no-op once the
real submodule has been imported anywhere in the suite.

### The two leak classes

**Class A — bare sys.modules install:**

```python
def _install_fake_googleapiclient(youtube):
    sys.modules["googleapiclient"] = types.ModuleType(...)
    sys.modules["googleapiclient.discovery"] = ...
    # No restore. Every later test that imports googleapiclient
    # gets the fake; .build(credentials=...) explodes with
    # AttributeError.
```

3 test files in this repo had this pattern as of 2026-05-10
(`test_research_cross_engage`, `test_pipeline_youtube_stats`,
`test_research_youtube`). They polluted 5+ unrelated test files
(`test_routes_oauth`, `test_routes_script_jobs`, `test_state_routes`,
`test_upload_youtube`, `test_e2e_happy_path`).

**Class B — parent-package-attribute precedence:**

```python
# In an earlier test:
from control import storage         # binds control.storage = real_module

# In the failing test:
with patch.dict("sys.modules", {"control.storage": mock_storage}):
    # CPython's IMPORT_FROM bytecode prefers control.storage attr
    # over sys.modules["control.storage"] — the mock is NEVER seen.
    from control import storage as _gcs
```

Same hazard for `from google.cloud import storage`,
`from google.cloud import firestore`,
`from pipeline.footage import yt_dlp_cloudrun`,
`from pipeline.audio import asr`, etc.

### The fix — three-layer

**1. Suite-level conftest fixture (recommended):**

`conftest.py` at the repo root snapshots + restores both `sys.modules`
keys AND the package-attribute bindings around every test. Cheap (a
small dict comprehension) and catches all pollution at the suite
boundary, no per-test changes required. See `conftest.py` —
``_restore_googleapiclient_modules`` autouse fixture.

```python
@pytest.fixture(autouse=True)
def _restore_googleapiclient_modules():
    saved = {k: sys.modules.get(k) for k in _GOOGLE_KEYS}
    google_cloud_pkg = sys.modules.get("google.cloud")
    saved_storage_attr = getattr(google_cloud_pkg, "storage", None) \
        if google_cloud_pkg is not None else None
    yield
    # Restore sys.modules + delattr / setattr the package attribute
    # so the next test re-resolves through sys.modules cleanly.
    ...
```

**2. Per-helper save+restore (when helper is hot-path):**

If a test helper installs fakes from inside multiple test classes,
make the helper return the saved-snapshot dict so the caller's
`tearDown` can pass it to a sibling `_uninstall_*` helper. See
`tests/test_pipeline_youtube_stats.py::_install_fake_googleapiclient`
for the canonical pattern.

**3. Pipeline-side lazy resolve (for legitimate test mockability):**

When pipeline code needs to be mockable via `patch.dict("sys.modules",
...)` in tests, resolve the submodule via `sys.modules` directly so
the patch wins regardless of attribute-binding state:

```python
# Instead of:
from pipeline.footage import yt_dlp_cloudrun
yt_dlp_cloudrun.download(...)

# Use:
import importlib, sys
yt_dlp_cloudrun = sys.modules.get("pipeline.footage.yt_dlp_cloudrun") \
    or importlib.import_module("pipeline.footage.yt_dlp_cloudrun")
yt_dlp_cloudrun.download(...)
```

This pattern is in `pipeline/voice_clone.py::_download_audio`,
`pipeline/upload/upload.py::_mirror_record_to_gcs`, and
`pipeline/research/youtube.py::_build_youtube` (via the package
re-export); see `pipeline/{images,upload}/__init__.py` for the
package-level PEP 562 lazy `__getattr__` form (which fixes
`patch.object(pkg, attr)` too).

### Audit recipes

```bash
# Tests that install fake sys.modules entries without restore:
grep -rnE "sys\.modules\[\"(googleapiclient|google\.cloud|pipeline\.|control\.)" tests/ --include='*.py' | grep -v "saved\|restore\|patch.dict"

# Pipeline call sites that should lazy-resolve via sys.modules:
grep -rnE "^from (pipeline|control|google\.cloud)\.[a-z_]+ import [a-z]" pipeline/ control/ --include='*.py' | grep -v "noqa: lazy-import"

# Package __init__.py files using static re-exports (susceptible to
# patch.object miss):
grep -rnE "^from pipeline\.[a-z_]+\.[a-z_]+ import" pipeline/*/__init__.py
```

### Why this is structural, not advisory

In a 4000-test suite, any one test that installs a fake without
restore creates O(N) pollution opportunities for the rest of the
suite. The 2026-05-10 incident bloomed from 3 leak sources into 35
test failures across 8 test files; the root-cause fix (conftest +
PEP 562 re-exports + lazy resolve in pipeline) eliminated all of
them in one pass. Per-test workarounds were considered and rejected
— they don't compose.

### Memory + sweep

- `feedback_test_isolation_control_storage_attr.md` — original
  observation (2026-05-10 perf pass) + the recommended workaround.
  This doc supersedes the "real fix when prioritized" section in
  that memory file with the conftest-level pattern.
- `feedback_sys_modules_test_pollution.md` — 2026-05-10 cleanup
  audit + grep recipes.
