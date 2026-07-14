"""Shared test setup.

Point the app at a throwaway SQLite file in the OS temp dir BEFORE any
``app.*`` module is imported, so importing the app under test never touches the
production ``/data`` volume (and never needs it to be writable).
"""

import os
import tempfile

os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite:///{os.path.join(tempfile.gettempdir(), 'fadeout_test.db')}",
)
