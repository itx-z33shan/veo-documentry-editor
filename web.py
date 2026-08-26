#!/usr/bin/env python3
"""Launch the local Veo Documentary finishing dashboard.

Normal private use:
    python web.py

For a sandbox/remote preview only:
    python web.py --host 0.0.0.0 --port 8000
"""

import argparse
import os
import sys

from src.dashboard import DashboardError, serve


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Launch the local Veo Documentary finishing dashboard.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind host (default: 127.0.0.1, local only).")
    parser.add_argument("--port", default=8765, type=int,
                        help="Bind port (default: 8765).")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not 1 <= args.port <= 65535:
        print("ERROR: --port must be between 1 and 65535.", file=sys.stderr)
        return 2
    root = os.path.abspath(os.path.dirname(__file__))
    try:
        serve(root, host=args.host, port=args.port)
    except OSError as exc:
        print("ERROR: Could not start dashboard on %s:%s: %s" %
              (args.host, args.port, exc), file=sys.stderr)
        return 1
    except DashboardError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
