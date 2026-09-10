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
