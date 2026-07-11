#!/usr/bin/env python3
"""Complete structural audit of the ytFactory repo (read-only, re-runnable).

Classifies every module into structural-slop categories so a restructure is driven
by evidence, not vibes. Path-precise import graph (not basename matching), so it
does NOT have the blind spots of a quick grep.

Run:  .venv/bin/python de-slop/structure_audit.py
"""
from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIRS = ["pipeline", "control", "cloud", "web", "scripts", "tests"]
PROD_DIRS = ["pipeline", "control", "cloud", "web", "scripts"]
ENTRYPOINT_STEMS = {
    "__main__", "entrypoint", "server", "server_dev", "app", "conftest",
    "wsgi", "asgi", "manage", "deploy", "__init__",
}


def iter_py():
    out = []
    for d in SRC_DIRS:
        base = ROOT / d
        if base.exists():
            out += [p for p in base.rglob("*.py") if "__pycache__" not in p.parts]
    out += list(ROOT.glob("*.py"))
    return out


def mod_name(p: Path) -> str:
    rel = p.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


FILES = iter_py()
MOD_OF = {p: mod_name(p) for p in FILES}
KNOWN = set(MOD_OF.values())
SRC = {p: p.read_text(errors="ignore") for p in FILES}


def is_pkg(p: Path) -> bool:
    return p.name == "__init__.py"


def refs_of(p: Path, src: str) -> set[str]:
    refs: set[str] = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return refs
    m = MOD_OF[p]
    pkg = m if is_pkg(p) else (m.rsplit(".", 1)[0] if "." in m else "")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                refs.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                parts = pkg.split(".") if pkg else []
                up = node.level - 1
                if up:
                    parts = parts[: len(parts) - up] if up <= len(parts) else []
                base = ".".join([x for x in parts if x] + ([node.module] if node.module else []))
            if base:
                refs.add(base)
            for a in node.names:
                if a.name != "*" and base:
                    refs.add(f"{base}.{a.name}")
    return refs


# inbound[module] = set of importer modules (internal only)
inbound: dict[str, set[str]] = defaultdict(set)
canonical_of_shim: dict[Path, str] = {}
for p in FILES:
    me = MOD_OF[p]
    for r in refs_of(p, SRC[p]):
        if r in KNOWN and r != me:
            inbound[r].add(me)


def all_assign(n) -> bool:
    return isinstance(n, ast.Assign) and all(
        isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets
    )


def shim_kind(p: Path, src: str):
    """'alias' = sys.modules trick; 'reexport' = non-init file that only re-imports."""
    if "sys.modules[__name__]" in src:
        return "alias"
    if is_pkg(p):
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    body = [
        n for n in tree.body
        if not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant))
    ]
    if not body:
        return None
    if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for n in body):
        return None
    if all(isinstance(n, (ast.Import, ast.ImportFrom)) or all_assign(n) for n in body):
        return "reexport"
    return None


def in_prod(p: Path) -> bool:
    return any(str(p.relative_to(ROOT)).startswith(d + "/") for d in PROD_DIRS)


# ---- classify ----
shims_alias, shims_reexport = [], []
for p in FILES:
    if not in_prod(p):
        continue
    k = shim_kind(p, SRC[p])
    if k == "alias":
        shims_alias.append(p)
        for r in refs_of(p, SRC[p]):
            if r in KNOWN and r != MOD_OF[p]:
                canonical_of_shim[p] = r
                break
    elif k == "reexport":
        shims_reexport.append(p)

# duplicate basenames (prod, excluding __init__)
byname: dict[str, list[Path]] = defaultdict(list)
for p in FILES:
    if in_prod(p) and p.name != "__init__.py":
        byname[p.name].append(p)
dup_names = {n: ps for n, ps in byname.items() if len(ps) > 1}

# thin non-shim modules
thin = []
for p in FILES:
    if not in_prod(p) or is_pkg(p) or shim_kind(p, SRC[p]):
        continue
    nl = len([l for l in SRC[p].splitlines() if l.strip()])
    if nl <= 8:
        thin.append((p, nl))

# single-module packages (dir with __init__ + exactly one other .py, no subpkgs)
pkgdirs = {p.parent for p in FILES if is_pkg(p) and in_prod(p)}
single_mod_pkgs = []
for d in sorted(pkgdirs):
    mods = [p for p in d.glob("*.py") if p.name != "__init__.py"]
    subpkgs = [c for c in d.iterdir() if c.is_dir() and (c / "__init__.py").exists()]
    if len(mods) == 1 and not subpkgs:
        single_mod_pkgs.append((d, mods[0]))

# genuinely dead modules (0 inbound, not entry-point, not a shim target)
dead = []
for p in FILES:
    if not in_prod(p):
        continue
    m = MOD_OF[p]
    stem = p.stem
    if stem in ENTRYPOINT_STEMS:
        continue
    if str(p.relative_to(ROOT)).startswith(("scripts/", "cloud/")):
        continue  # entry points run directly; verify separately
    if not inbound.get(m):
        dead.append(p)


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


def section(title, items):
    print(f"\n{'='*70}\n{title}  [{len(items)}]\n{'='*70}")


section("1. ALIAS SHIMS (sys.modules trick — pure structural slop, delete + repoint)", shims_alias)
for p in sorted(shims_alias):
    imp = len(inbound.get(MOD_OF[p], set()))
    print(f"  {rel(p):45s}  -> {canonical_of_shim.get(p,'?'):28s}  importers={imp}")

section("2. RE-EXPORT MODULES (non-__init__ files that only re-import)", shims_reexport)
for p in sorted(shims_reexport):
    print(f"  {rel(p):45s}  importers={len(inbound.get(MOD_OF[p], set()))}")

section("3. DUPLICATE BASENAMES (same filename in >1 location)", dup_names)
for n, ps in sorted(dup_names.items()):
    print(f"  {n}")
    for p in sorted(ps):
        print(f"       {rel(p):45s} importers={len(inbound.get(MOD_OF[p], set()))}")

section("4. THIN MODULES (<=8 non-blank lines, not shims)", thin)
for p, nl in sorted(thin):
    print(f"  {rel(p):45s}  lines={nl}")

section("5. SINGLE-MODULE PACKAGES (dir wraps exactly one module)", single_mod_pkgs)
for d, m in single_mod_pkgs:
    print(f"  {rel(d)+'/':40s} -> only module: {m.name}")

section("6. DEAD MODULES (0 internal importers, path-precise, excl scripts/cloud)", dead)
for p in sorted(dead):
    print(f"  {rel(p)}")

print(f"\n{'='*70}\nSUMMARY\n{'='*70}")
print(f"  prod .py files scanned : {sum(1 for p in FILES if in_prod(p))}")
print(f"  alias shims            : {len(shims_alias)}")
print(f"  re-export modules      : {len(shims_reexport)}")
print(f"  duplicate basenames    : {len(dup_names)}")
print(f"  thin modules           : {len(thin)}")
print(f"  single-module packages : {len(single_mod_pkgs)}")
print(f"  dead modules           : {len(dead)}")
print(f"  => shim files removable: {len(shims_alias)+len(shims_reexport)} "
      f"(after repointing their importers to the canonical module)")
