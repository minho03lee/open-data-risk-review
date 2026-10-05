import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main

ROOT = Path(__file__).parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node가 없으면 건너뜀")
def test_render_js():
    r = subprocess.run(["node", str(ROOT / "render.test.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "render ok" in r.stdout


def test_static_render_js_is_served():
    r = TestClient(main.app).get("/static/render.js")
    assert r.status_code == 200 and "ODR" in r.text
