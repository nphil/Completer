#!/usr/bin/env python3
"""Entry point that NZBGet starts. All the work is in the ``completer`` package next to this file."""

import os
import sys

sys.dont_write_bytecode = True  # NZBGet's scripts folder is not a place for cache files
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from completer.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
