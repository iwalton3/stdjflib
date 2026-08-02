#!/usr/bin/env python3
"""Entry point: build a standard Jellyfin QA library."""
import sys

from stdjflib.cli import main

if __name__ == "__main__":
    sys.exit(main())
