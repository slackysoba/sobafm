"""Explicit owned-synthetic entry; shares the existing controller and gates."""

from .controller import main

if __name__ == "__main__":
    raise SystemExit(main(qualification=True))
