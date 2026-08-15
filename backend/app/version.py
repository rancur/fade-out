"""Single source of truth for the running build's identity.

``/api/upgrade/status`` used to report ``current_version: 0.0.0`` because
``_get_current_version()`` read ``/app/VERSION``, a file the image never
contained. A version of 0.0.0 compares older than every release, and the
status endpoint separately hard-coded ``update_available`` off until someone
POSTed a check — so a container built on 2026-07-21 sat three releases behind
for weeks while the upgrade checker reported nothing to do.

The version now lives in code (it ships with the code by construction), and
the build stamp is injected at image build time so a running container can be
matched against the commit it was actually built from.
"""

import os
import subprocess
from functools import lru_cache
from typing import Dict, Optional

__version__ = "2.4.0"


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name, "").strip()
    return value or None


@lru_cache(maxsize=1)
def build_info() -> Dict[str, Optional[str]]:
    """Identity of the running build: version, commit, build timestamp.

    ``BUILD_COMMIT``/``BUILD_TIME`` are baked in by the Dockerfile. Outside a
    built image (dev checkouts) the commit is read from git so the value is
    never silently absent.
    """
    commit = _env("BUILD_COMMIT")
    if not commit:
        try:
            commit = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip() or None
        except Exception:  # pragma: no cover - no git in the image
            commit = None
    return {
        "version": __version__,
        "commit": commit,
        "built_at": _env("BUILD_TIME"),
        "image_source": _env("BUILD_SOURCE"),
    }
