#!/usr/bin/env python3
"""Developer entry point: ``python run.py --dry-run``.

On the live ISO the equivalent is ``python3 -m raidforge``; this wrapper exists
so you can drive the app straight from a checkout without installing it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from raidforge.main import main  # noqa: E402  (path bootstrap must run first)

if __name__ == "__main__":
    sys.exit(main())
