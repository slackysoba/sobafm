"""Explicit owned-synthetic entry; shares the existing local intake owner."""

from .owner import main

if __name__ == "__main__":
    raise SystemExit(main(qualification=True))
