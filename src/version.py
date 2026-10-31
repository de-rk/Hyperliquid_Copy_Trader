"""Identify the running build so a stale deployment is obvious from chat.

Twice now a code change looked "missing" simply because the process that
answered /start was started before the change. Nothing in Telegram or in the
logs said which revision was live, so the only way to tell was to guess.

Docker images here do not ship ``.git`` (see ``.dockerignore``), so the commit
is baked in as the ``BUILD_ID`` build arg. A plain checkout falls back to
reading git directly, and a build with neither still reports the app version
instead of failing.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

# Bump when shipping a behaviour change; the commit hash carries the detail.
APP_VERSION = "1.1.0"

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _git_short_sha() -> str:
    """Short HEAD hash of the checkout, or "" outside a git work tree."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


@lru_cache(maxsize=1)
def build_stamp() -> str:
    """Short, chat-safe build label such as ``v1.1.0 +6957695``."""
    stamp = os.getenv("BUILD_ID", "").strip() or _git_short_sha()
    return f"v{APP_VERSION} +{stamp}" if stamp else f"v{APP_VERSION}"
