"""Run the demo's renderers for real, via node, and check what they drew.

`test_countries_and_demo.py` proves the demo *handles* every chart type; this proves the drawing
is real: one rect per row, one line per edge, nothing dropped silently. The assertions live in
`test_demo_renders.js` because running the page's own code is the only way to test the page's own
code; this module is the pytest entry point, and it skips rather than fails where node is absent
so the suite stays runnable on a machine that has only Python.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
DEMO = HERE.parents[1] / "demo" / "index.html"
HARNESS = HERE / "test_demo_renders.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_demo_renderers_draw_what_the_spec_describes() -> None:
    result = subprocess.run(
        [shutil.which("node") or "node", str(HARNESS), str(DEMO)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "all assertions passed" in result.stdout
