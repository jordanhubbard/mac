"""Compatibility entrypoint for the autonomous task executor.

The sandbox and coding-agent runner implementation lives in
``mac.executor_sandbox``.  This module aliases that implementation instead of
copying its public names so existing imports and monkeypatches continue to
operate on the canonical module.
"""

from __future__ import annotations

import sys

from mac import executor_sandbox as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())


sys.modules[__name__] = _implementation
