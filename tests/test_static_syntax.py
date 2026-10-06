"""The static pages' JavaScript parses.

A syntax error in a page's inline script stops the whole script, so the table
renders nothing; this catches one before a deploy. Skipped automatically when
``node`` is unavailable (e.g. on the Python-only CI).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
_INLINE = re.compile(r"<script(?![^>]*\bsrc=)([^>]*)>(.*?)</script>", re.S)


def _sources():
    for page in sorted(_STATIC.glob("*.html")):
        for i, (attrs, body) in enumerate(_INLINE.findall(page.read_text(encoding="utf-8"))):
            if "json" in attrs or not body.strip():
                continue  # JSON-LD and empty tags are not scripts
            suffix = ".mjs" if 'type="module"' in attrs else ".js"
            yield pytest.param(body, suffix, id=f"{page.name}#{i}")
    for script in sorted(_STATIC.glob("*.js")):
        source = script.read_text(encoding="utf-8")
        yield pytest.param(source, ".mjs" if re.search(r"^(export|import) ", source, re.M) else ".js", id=script.name)


@pytest.mark.skipif(_NODE is None, reason="node is required for the syntax check")
@pytest.mark.parametrize("source, suffix", list(_sources()))
def test_static_javascript_parses(tmp_path, source, suffix):
    path = tmp_path / f"script{suffix}"
    path.write_text(source, encoding="utf-8")
    proc = subprocess.run([_NODE, "--check", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
