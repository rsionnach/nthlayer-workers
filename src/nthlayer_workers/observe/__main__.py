"""Allow running as python -m nthlayer_observe."""

from nthlayer_workers.observe.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
