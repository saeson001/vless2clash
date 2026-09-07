#!/usr/bin/env python3
"""v2c pre-release gate — run this BEFORE every push.

Each rule below exists because it once broke a live subscription:

  R1 version sync      deploy.sh had an old VERSION -> update installed "new" code
                       that still reported the old version.
  R2 CRLF              Windows-written .sh files run through bash -> "\r: command
                       not found" and a bricked install.
  R3 ASCII headers     gunicorn latin-1 encodes response headers
                       (gunicorn/http/wsgi.py:180). A Chinese 3x-ui inbound remark
                       in X-Traffic-Detail raised UnicodeEncodeError, the worker
                       died mid-response and EVERY client got
                       "Empty reply from server" (curl 52) => import failure.
  R4 compile/smoke     Syntax errors and missing imports must never reach main.
  R5 global config     A new key read via gcfg["x"] without a DEFAULT_GLOBAL_CONFIG
                       entry crashes on upgrade (KeyError) — v1.6.22 bug.
  R6 cache busting     Static assets without ?v={{ version }} leave browsers on
                       stale JS after an upgrade (v1.6.20).

Usage:
    python scripts/pre_release_check.py [--fix-crlf]

Exit code 0 = all good, 1 = at least one blocking problem.
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

ERRORS: list[str] = []
WARNINGS: list[str] = []

# Headers whose value is ASCII by construction (percent-encoded / token).
HEADER_ALLOWLIST = {
    "Content-Disposition",
    "Content-Type",
    "Cache-Control",
    "X-Accel-Buffering",
}


def err(rule: str, msg: str) -> None:
    ERRORS.append(f"[{rule}] {msg}")


def warn(rule: str, msg: str) -> None:
    WARNINGS.append(f"[{rule}] {msg}")


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


# --- R1: version sync -------------------------------------------------------
def check_version_sync() -> None:
    app_src = read("app.py")
    m_app = re.search(r'^APP_VERSION\s*=\s*"(v[^"]+)"', app_src, re.M)
    dep_src = open("deploy.sh", "rb").read().decode("utf-8", "replace")
    m_dep = re.search(r'^VERSION="(v[^"]+)"', dep_src, re.M)
    if not m_app:
        err("R1", "app.py: APP_VERSION not found")
        return
    if not m_dep:
        err("R1", "deploy.sh: VERSION not found")
        return
    if m_app.group(1) != m_dep.group(1):
        err("R1", f"version mismatch: app.py={m_app.group(1)} deploy.sh={m_dep.group(1)}")
    else:
        print(f"  R1 version sync: OK ({m_app.group(1)})")


# --- R2: CRLF ---------------------------------------------------------------
def check_crlf(fix: bool = False) -> None:
    targets = ["app.py", "deploy.sh", "install.sh", "requirements.txt"]
    targets += [os.path.join("scripts", f) for f in os.listdir("scripts")
                if f.endswith((".py", ".sh"))] if os.path.isdir("scripts") else []
    for rel in targets:
        if not os.path.exists(rel):
            continue
        raw = open(rel, "rb").read()
        if b"\r\n" in raw:
            if fix:
                open(rel, "wb").write(raw.replace(b"\r\n", b"\n"))
                warn("R2", f"{rel}: CRLF -> LF (fixed)")
            else:
                err("R2", f"{rel}: contains CRLF (bash will break); run with --fix-crlf")
    if not any("R2" in e for e in ERRORS):
        print("  R2 line endings: OK (no CRLF)")


# --- R3: non-ASCII response headers ----------------------------------------
def check_headers_ascii() -> None:
    src = read("app.py")
    # response.headers["X"] = <something>
    pattern = re.compile(r'response\.headers\[\s*"([^"]+)"\s*\]\s*=\s*(.+)')
    for lineno, line in enumerate(src.splitlines(), 1):
        m = pattern.search(line)
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        if name in HEADER_ALLOWLIST:
            continue
        # plain ASCII string literal (e.g. "upload=0; download=0; ...") is safe
        lit = value.strip()
        if lit[:1] in ("'", '"') and all(ord(c) < 128 for c in lit):
            continue
        if "_ascii_header" not in value:
            # multi-line assignment: check a window around the line, e.g.
            #   detail = _ascii_header(_format_traffic_detail(traffic))
            #   response.headers["X-Traffic-Detail"] = detail
            lines = src.splitlines()
            window = "\n".join(lines[max(0, lineno - 4):lineno + 3])
            if "_ascii_header" not in window:
                err("R3", f"app.py:{lineno} header '{name}' not wrapped in _ascii_header() "
                          f"— gunicorn latin-1 encodes headers and will crash the worker")
    if not any("R3" in e for e in ERRORS):
        print("  R3 header ASCII safety: OK")


# --- R4: compile + import smoke --------------------------------------------
def check_compile() -> None:
    try:
        # compile() in memory — writing a .pyc breaks on Windows (rename EBUSY)
        compile(read("app.py"), "app.py", "exec")
    except Exception as exc:  # noqa: BLE001
        err("R4", f"app.py fails to compile: {exc}")
        return
    print("  R4 compile: OK")


# --- R5: global config defaults --------------------------------------------
def check_global_config_keys() -> None:
    src = read("app.py")
    m = re.search(r"DEFAULT_GLOBAL_CONFIG\s*=\s*\{(.*?)\n\}", src, re.S)
    if not m:
        warn("R5", "DEFAULT_GLOBAL_CONFIG block not found (skipped)")
        return
    defaults = set(re.findall(r'"([a-z_]+)"\s*:', m.group(1)))
    # gcfg["key"] / gcfg.get("key") usages outside the defaults block
    used = set(re.findall(r'gcfg(?:\.get\(\s*)?\[\s*"([a-z_]+)"\s*\]', src))
    missing = {k for k in used - defaults if k not in defaults}
    if missing:
        err("R5", f"keys read via gcfg but missing from DEFAULT_GLOBAL_CONFIG: "
                  f"{sorted(missing)} (upgrade would KeyError)")
    else:
        print(f"  R5 global config keys: OK ({len(defaults)} defaults)")


# --- R6: static asset cache busting ----------------------------------------
def check_cache_busting() -> None:
    ver = re.search(r'^APP_VERSION\s*=\s*"(v[^"]+)"', read("app.py"), re.M)
    tpl_dir = "templates"
    if not os.path.isdir(tpl_dir):
        return
    for name in os.listdir(tpl_dir):
        if not name.endswith(".html"):
            continue
        src = read(os.path.join(tpl_dir, name))
        for asset in re.findall(r'(?:href|src)="([^"]*\.(?:css|js))"', src):
            if "?" not in asset and "{{" not in asset:
                warn("R6", f"{name}: static asset '{asset}' has no cache-busting query")
    print("  R6 static cache busting: checked")


def main() -> int:
    fix = "--fix-crlf" in sys.argv
    print("v2c pre-release check")
    check_version_sync()
    check_crlf(fix=fix)
    check_headers_ascii()
    check_compile()
    check_global_config_keys()
    check_cache_busting()

    for w in WARNINGS:
        print("  WARN", w)
    if ERRORS:
        print("\nBLOCKED — fix these before pushing:")
        for e in ERRORS:
            print("  ✗", e)
        return 1
    print("\nAll checks passed. Safe to push.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
