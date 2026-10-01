"""The static page's classic scripts must not redeclare each other's globals.

``index.html`` loads ``search.js`` and ``calendar.js`` as classic (non-module)
scripts and then runs its own inline script, so every top-level ``const`` /
``let`` / ``class`` lands in one shared global scope. Declaring the same name in
two of them throws ``SyntaxError: Identifier ... has already been declared`` and
the inline script (which renders the table) never runs, leaving the page empty.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "web" / "static"

# (file, indentation of its top-level statements). ``nl_query.js`` is wrapped in
# an IIFE, so it declares no globals and is not listed.
_SCRIPTS = [("search.js", ""), ("calendar.js", ""), ("index.html", "    ")]


def _top_level_names(path: Path, indent: str) -> set[str]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".html":
        # Only the inline script; skip the external <script src=...> tags.
        text = re.search(r"<script>(.*?)</script>", text, re.S).group(1)
    pattern = rf"^{indent}(?:const|let|class|function)\s+([A-Za-z_$][\w$]*)"
    return set(re.findall(pattern, text, re.M))


def test_static_scripts_do_not_redeclare_globals():
    seen: dict[str, str] = {}
    clashes = []
    for name, indent in _SCRIPTS:
        for ident in _top_level_names(_STATIC / name, indent):
            if ident in seen:
                clashes.append(f"{ident} ({seen[ident]} and {name})")
            seen[ident] = name
    assert not clashes, "duplicate global declarations: " + ", ".join(clashes)


def test_scan_finds_declarations():
    # Guard against the scan silently matching nothing (e.g. an indent change).
    for name, indent in _SCRIPTS:
        assert len(_top_level_names(_STATIC / name, indent)) > 5, name
