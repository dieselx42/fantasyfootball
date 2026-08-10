"""Vercel serverless entry point.

Vercel's Python runtime looks for a WSGI/ASGI callable named ``app``. All the
logic lives in :mod:`ff.wsgi`; this file only makes the package importable
from the function's working directory.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ff.wsgi import app  # noqa: E402

__all__ = ["app"]
