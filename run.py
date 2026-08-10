#!/usr/bin/env python3
"""Start the fantasy assistant.

    python3 run.py                 # http://127.0.0.1:8777
    python3 run.py --port 9000
    python3 run.py --host 0.0.0.0  # reachable from your phone on the same wifi
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from threading import Timer

if sys.version_info < (3, 9):
    sys.exit("This needs Python 3.9 or newer. You have %s." % sys.version.split()[0])

from ff.web.server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Fantasy football draft assistant")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not args.no_browser:
        url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}"
        Timer(0.6, lambda: webbrowser.open(url)).start()

    serve(args.host, args.port)


if __name__ == "__main__":
    main()
