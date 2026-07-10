#!/usr/bin/env python3
"""Compatibility entry point; use mirror.py for new automation."""

from mirror import main


if __name__ == "__main__":
    raise SystemExit(main())
