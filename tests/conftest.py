import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo" / "order-service"
sys.path.insert(0, str(DEMO))

from server import OrderService  # noqa: E402


@pytest.fixture
def service():
    s = OrderService(0)
    yield s
    s.close()


@pytest.fixture
def project(tmp_path):
    """A copy of the demo provider project with its consumers alongside."""
    dest = tmp_path / "proj"
    shutil.copytree(DEMO, dest, ignore=shutil.ignore_patterns("__pycache__", "server.py"))
    shutil.copytree(ROOT / "demo" / "consumers", dest / "consumers")
    return dest
