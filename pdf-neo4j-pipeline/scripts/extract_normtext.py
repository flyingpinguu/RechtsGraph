#!/usr/bin/env python3
"""Compatibility wrapper for the normtext extractor CLI."""

import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from normtext_extractor.cli import main  # noqa: E402


if __name__ == "__main__":
    main()
