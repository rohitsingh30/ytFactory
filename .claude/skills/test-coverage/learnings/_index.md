# test-coverage learnings index

Living index of CLASS-OF-BUG findings about the gate itself,
captured by `/update-docs` when the gate's behaviour misled the
operator. New rows go above; oldest at the bottom.

| date       | topic                                                  | file                                  |
|------------|--------------------------------------------------------|---------------------------------------|
| 2026-05-12 | Test discovery missed `from pkg import stem` imports   | (this entry)                          |
| earlier    | Backlog: 50k LoC of pre-existing untested production   | [backlog.md](backlog.md)              |
| earlier    | Explicit-skip comment audit                            | [explicit-skips.md](explicit-skips.md)|
| earlier    | Frontend test infra (web-next, node --test)            | [frontend-test-infra.md](frontend-test-infra.md) |

## 2026-05-12 — Test discovery missed `from pkg import stem` imports

**Symptom.** `/test-coverage` reported `pipeline/llm/cli.py 0/71
executable (0%) (no related test file found)` after a max_tokens
fix, even though `tests/test_llm_dispatcher.py` and
`tests/test_pipeline_llm.py` had 80+ pre-existing tests covering
the module (and 12 new ones added in the same change).

**Root cause.** `scripts/coverage_gate.py::find_related_tests` greps
for two import patterns:

```
(from <dotted> import|import <dotted>\b)
```

For a leaf module `pipeline/llm/cli.py`, `dotted` evaluates to
`pipeline.llm.cli`. That regex matches:

- `from pipeline.llm.cli import …`
- `import pipeline.llm.cli`

But NOT the far more common `from <parent> import <leaf>` form:

- `from pipeline.llm import cli as llm_cli`  ← MISSED

Both `tests/test_llm_dispatcher.py` and `tests/test_pipeline_llm.py`
use the parent-package form, so neither was discovered → gate
declared the file untested → user wasted an iteration trying to
figure out why.

Compounding gotcha: git-grep's ERE doesn't support `\b` after a
`*` quantifier. The naive fix `from pipeline\.llm import .*\bcli\b`
matched zero files; had to use POSIX-portable
`(^|[^A-Za-z0-9_])` / `([^A-Za-z0-9_]|$)` word-boundary character
classes instead.

**Fix.** `scripts/coverage_gate.py::find_related_tests` now also
generates a parent-package pattern for leaf modules:

```python
if "." in dotted and fd.path.name != "__init__.py":
    parent_dotted, _, stem = dotted.rpartition(".")
    patterns.append(
        rf"from {re.escape(parent_dotted)} import "
        rf".*[^A-Za-z0-9_]?{re.escape(stem)}([^A-Za-z0-9_]|$)"
    )
```

**Pin.** `tests/test_coverage_gate.py::FindRelatedTestsTests::test_python_leaf_module_imported_via_parent_package`.

**How to verify on your own changes.** If `/test-coverage` reports
`(no related test file found)` for a file you KNOW is tested:

```bash
# What pattern would the gate try?
.venv/bin/python -c "
from pathlib import Path
import re
fd_path = Path('pipeline/llm/cli.py')   # ← your file
dotted = '.'.join((*fd_path.parent.parts, fd_path.stem))
parent_dotted, _, stem = dotted.rpartition('.')
print('exact:', f'(from {dotted} import|import {dotted}\\b)')
print('parent:', f'from {parent_dotted} import .*[^A-Za-z0-9_]?{stem}([^A-Za-z0-9_]|\$)')
"

# Now grep with each — at least one should match the test file:
git grep -E '<pattern>' tests/
```

If neither matches, the test file uses some THIRD form not yet
covered by the heuristic (e.g. `importlib.util.spec_from_file_location`
for non-package modules in hyphenated dirs — already handled via
the path-based + parent-dir-name patterns). Add a new pattern in
`find_related_tests` and a regression test mirroring the one above.

**Commit.** `bfbbec1` (gate discovery fix, 2026-05-12).
