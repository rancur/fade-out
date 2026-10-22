"""The version string must move with every release.

v2.5.0 and v2.6.1 were tagged without bumping ``app.version.__version__``, so
every build — including ones newer than v2.6.1 — reported 2.4.1, and the
deployment-freshness check flagged a current deploy as "stale: running 2.4.1,
latest v2.6.1" forever. A permanent false red hides a real one.

The CHANGELOG's newest heading is what a release adds first, so pin every
place the version is written to it. Cutting a release without bumping the
code now fails here, before the tag exists.
"""

import json
import re
from pathlib import Path

from app.version import __version__

ROOT = Path(__file__).resolve().parents[2]


def _changelog_version() -> str:
    for line in (ROOT / "CHANGELOG.md").read_text().splitlines():
        m = re.match(r"^## v(\d+\.\d+\.\d+)\b", line)
        if m:
            return m.group(1)
    raise AssertionError("no '## vX.Y.Z' heading in CHANGELOG.md")


def test_backend_version_matches_latest_changelog_release():
    assert __version__ == _changelog_version()


def test_frontend_package_version_matches_backend():
    pkg = json.loads((ROOT / "frontend" / "package.json").read_text())
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text())
    assert pkg["version"] == __version__
    assert lock["version"] == __version__
    assert lock["packages"][""]["version"] == __version__


def test_sidebar_shows_backend_version():
    layout = (ROOT / "frontend" / "src" / "components" / "Layout.tsx").read_text()
    shown = re.findall(r">v(\d+\.\d+\.\d+)<", layout)
    assert shown == [__version__]
