#!/usr/bin/env python3
"""
Central repository path resolution helper for LLM Telemetry Proxy.
Works seamlessly across standard Python execution and Nuitka standalone binary executions.
"""

import os
import sys
from pathlib import Path
from typing import Optional


def resolve_repo_root(start_file: Optional[Path] = None) -> Path:
    """Accurately locate the repository root under Python and Nuitka standalone binary execution."""
    # 1. Environment variable override from dashboard.sh / start.sh
    for env_key in ("LLM_PROXY_REPO_ROOT", "REPO_ROOT"):
        val = os.environ.get(env_key)
        if val and Path(val).is_dir():
            return Path(val).resolve()

    # 2. Check parents of start_file for repository indicators
    start = (start_file or Path(__file__)).resolve()
    for p in [start.parent] + list(start.parents):
        if (p / "proxy" / "llm_telemetry_proxy.py").is_file():
            return p
        if (p / "proxy").is_dir() and ((p / "dashboard").is_dir() or (p / "data").is_dir()):
            return p

    # 3. Check current working directory
    try:
        cwd = Path.cwd().resolve()
        for p in [cwd] + list(cwd.parents):
            if (p / "proxy" / "llm_telemetry_proxy.py").is_file():
                return p
            if (p / "proxy").is_dir() and ((p / "dashboard").is_dir() or (p / "data").is_dir()):
                return p
    except Exception:
        pass

    # 4. Fallback for Nuitka standalone directories (dist/<name>.dist/<name>.bin or dist/<name>.bin)
    p = start.parent
    if p.name.endswith(".dist"):
        return p.parent.parent
    if p.name == "dist":
        return p.parent
    return p.parent


REPO_ROOT = resolve_repo_root()
DIST_DIR = REPO_ROOT / "dist"
DIST_MODULES_DIR = DIST_DIR / "modules"


def setup_native_modules_path() -> bool:
    """
    Conditionally configure sys.path to load pre-compiled native C-extension modules
    from dist/modules if they exist and native execution is not explicitly disabled.

    Respects USE_NATIVE_BINARY=0 or USE_NATIVE_MODULES=0 to guarantee pure Python execution.
    Returns True if dist/modules was added to sys.path, False otherwise.
    """
    for env_key in ("USE_NATIVE_BINARY", "USE_NATIVE_MODULES", "LLM_PROXY_NATIVE"):
        if os.environ.get(env_key, "").strip() == "0":
            return False

    if DIST_MODULES_DIR.is_dir():
        dist_mod_str = str(DIST_MODULES_DIR)
        if dist_mod_str not in sys.path:
            sys.path.insert(0, dist_mod_str)
        return True
    return False
