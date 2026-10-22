"""Pytest configuration shared across the backend test suite.

Adds ``backend/src`` to ``sys.path`` so tests can import domain packages
(``diagnostics``, ``shared``, ``handlers``) by their top-level names, matching how
Lambda packaging lays the code out at the deployment root.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = Path(__file__).parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
