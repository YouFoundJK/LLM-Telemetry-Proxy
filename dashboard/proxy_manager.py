#!/usr/bin/env python3
"""
Proxy Manager — Process lifecycle and maintenance supervisor for LLM Telemetry Proxy.

Provides cross-platform management to start, stop, restart, monitor health,
stream logs, and trigger database maintenance.
"""

import asyncio
import ctypes
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

import aiohttp

DASHBOARD_DIR = Path(__file__).resolve().parent
REPO_ROOT = DASHBOARD_DIR.parent

DEFAULT_PROXY_PORT = 9090
DEFAULT_UPSTREAM = "https://llm.ai.e-infra.cz/v1"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_TOKEN_LIMIT = 480_000_000


def format_time_remaining(seconds: int) -> str:
    if seconds <= 0:
        return "0m"
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    secs = seconds % 60
    if hrs > 0:
        return f"{hrs}h {mins}m" if mins > 0 else f"{hrs}h"
    if mins > 0:
        return f"{mins}m"
    if secs > 0:
        return "< 1m"
    return "0m"


def get_data_dir() -> Path:
    candidates = [
        REPO_ROOT / "data",
        DASHBOARD_DIR / "data",
    ]
    for c in candidates:
        if c.exists():
            return c
    data_dir = REPO_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_pid_file() -> Path:
    return get_data_dir() / ".proxy.pid"


def get_log_file() -> Path:
    return get_data_dir() / "proxy.log"


def get_proxy_script_path() -> Path:
    candidates = [
        REPO_ROOT / "proxy" / "llm_telemetry_proxy.py",
        DASHBOARD_DIR.parent / "proxy" / "llm_telemetry_proxy.py",
        DASHBOARD_DIR / "proxy" / "llm_telemetry_proxy.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def get_db_compress_script_path() -> Path:
    candidates = [
        REPO_ROOT / "proxy" / "db_compress.py",
        DASHBOARD_DIR.parent / "proxy" / "db_compress.py",
        DASHBOARD_DIR / "proxy" / "db_compress.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def is_pid_alive(pid: int) -> bool:
    """Check if process with given PID is currently active."""
    if not pid or pid <= 0:
        return False

    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
            if handle:
                exit_code = ctypes.c_ulong()
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                kernel32.CloseHandle(handle)
                STILL_ACTIVE = 259
                return exit_code.value == STILL_ACTIVE
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except Exception:
            return False


def kill_pid(pid: int, force: bool = False) -> bool:
    """Terminate process tree with given PID."""
    if not is_pid_alive(pid):
        return True

    if sys.platform == "win32":
        try:
            cmd = ["taskkill", "/PID", str(pid), "/T"]
            if force:
                cmd.append("/F")
            subprocess.run(cmd, capture_output=True, timeout=5)
            time.sleep(0.5)
            if not is_pid_alive(pid):
                return True
            # Force if still alive
            subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True, timeout=5)
            return not is_pid_alive(pid)
        except Exception:
            return not is_pid_alive(pid)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if not is_pid_alive(pid):
                    return True
                time.sleep(0.1)
            os.kill(pid, signal.SIGKILL)
            return not is_pid_alive(pid)
        except Exception:
            return not is_pid_alive(pid)


def get_persisted_token_budget() -> Dict[str, Any]:
    """Retrieve 24H token usage from data/token_budget.json or SQLite DB as fallback."""
    now = time.time()
    now_utc = datetime.now(timezone.utc)
    start_of_today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_ts = start_of_today.timestamp()
    cutoff_iso = start_of_today.isoformat()

    tomorrow_midnight = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_reset_seconds = max(0, int((tomorrow_midnight - now_utc).total_seconds()))
    daily_reset_formatted = format_time_remaining(daily_reset_seconds)

    # 1. Check data/token_budget.json
    budget_file = get_data_dir() / "token_budget.json"
    if budget_file.exists():
        try:
            with open(budget_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                records = [r for r in data.get("recent_usage", []) if r.get("ts", 0) >= cutoff_ts]
                daily_limit = data.get("daily_limit", DEFAULT_TOKEN_LIMIT)
                total_used = sum(r.get("tokens", 0) for r in records) if records else (data.get("total_used", 0) if data.get("updated_at", "") >= cutoff_iso else 0)
                remaining = max(0, daily_limit - total_used)
                percentage_used = round((total_used / daily_limit) * 100, 2) if daily_limit > 0 else 0.0

                oldest_ts = records[0]["ts"] if records else None
                newest_ts = records[-1]["ts"] if records else None
                next_reset_seconds = max(0, int((oldest_ts + 86400) - now)) if oldest_ts else 0
                full_reset_seconds = max(0, int((newest_ts + 86400) - now)) if newest_ts else 0

                return {
                    "total_used": total_used,
                    "remaining": remaining,
                    "percentage_used": percentage_used,
                    "daily_limit": daily_limit,
                    "daily_reset_seconds": daily_reset_seconds,
                    "daily_reset_formatted": daily_reset_formatted,
                    "next_reset_seconds": next_reset_seconds,
                    "full_reset_seconds": full_reset_seconds,
                    "next_reset_formatted": format_time_remaining(next_reset_seconds) if oldest_ts else None,
                    "full_reset_formatted": format_time_remaining(full_reset_seconds) if newest_ts else None,
                    "server_time": now_utc.isoformat(),
                    "reset_time_utc": tomorrow_midnight.isoformat(),
                }
        except Exception:
            pass

    # 2. Check SQLite DB
    db_file = get_data_dir() / "llm_telemetry.db"
    if db_file.exists():
        try:
            conn = sqlite3.connect(str(db_file), timeout=2.0)
            cur = conn.cursor()
            cur.execute("""
                SELECT timestamp, (COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) * COALESCE(calls_count, 1)
                FROM api_calls
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
            """, (cutoff_iso,))
            rows = cur.fetchall()
            conn.close()

            total_used = 0
            valid_ts = []
            for r in rows:
                ts_str, tok = r
                if tok and tok > 0:
                    total_used += tok
                    try:
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        t_float = dt.timestamp()
                        if t_float >= cutoff_ts:
                            valid_ts.append(t_float)
                    except Exception:
                        pass

            daily_limit = DEFAULT_TOKEN_LIMIT
            remaining = max(0, daily_limit - total_used)
            perc = round((total_used / daily_limit) * 100, 2) if daily_limit > 0 else 0.0

            oldest_ts = valid_ts[0] if valid_ts else None
            newest_ts = valid_ts[-1] if valid_ts else None
            next_reset_seconds = max(0, int((oldest_ts + 86400) - now)) if oldest_ts else 0
            full_reset_seconds = max(0, int((newest_ts + 86400) - now)) if newest_ts else 0

            return {
                "total_used": total_used,
                "remaining": remaining,
                "percentage_used": perc,
                "daily_limit": daily_limit,
                "daily_reset_seconds": daily_reset_seconds,
                "daily_reset_formatted": daily_reset_formatted,
                "next_reset_seconds": next_reset_seconds,
                "full_reset_seconds": full_reset_seconds,
                "next_reset_formatted": format_time_remaining(next_reset_seconds) if oldest_ts else None,
                "full_reset_formatted": format_time_remaining(full_reset_seconds) if newest_ts else None,
                "server_time": now_utc.isoformat(),
                "reset_time_utc": tomorrow_midnight.isoformat(),
            }
        except Exception:
            pass

    return {
        "total_used": 0,
        "remaining": DEFAULT_TOKEN_LIMIT,
        "percentage_used": 0.0,
        "daily_limit": DEFAULT_TOKEN_LIMIT,
        "daily_reset_seconds": daily_reset_seconds,
        "daily_reset_formatted": daily_reset_formatted,
        "next_reset_seconds": 0,
        "full_reset_seconds": 0,
        "next_reset_formatted": None,
        "full_reset_formatted": None,
        "server_time": now_utc.isoformat(),
        "reset_time_utc": tomorrow_midnight.isoformat(),
    }


class ProxyManager:
    """Manages the lifecycle and health of the LLM Telemetry Proxy."""

    _last_known_port = DEFAULT_PROXY_PORT
    _last_known_upstream = DEFAULT_UPSTREAM
    _last_known_token_limit = DEFAULT_TOKEN_LIMIT

    @classmethod
    def read_pid_file(cls) -> Optional[int]:
        pid_file = get_pid_file()
        if pid_file.exists():
            try:
                content = pid_file.read_text(encoding="utf-8").strip()
                if content:
                    pid = int(content)
                    if is_pid_alive(pid):
                        return pid
                    else:
                        # Stale PID file
                        try:
                            pid_file.unlink(missing_ok=True)
                        except Exception:
                            pass
            except Exception:
                pass
        return None

    @classmethod
    def write_pid_file(cls, pid: int):
        pid_file = get_pid_file()
        try:
            pid_file.write_text(str(pid), encoding="utf-8")
        except Exception as e:
            print(f"[ProxyManager] Failed to write PID file: {e}", file=sys.stderr)

    @classmethod
    def clear_pid_file(cls):
        pid_file = get_pid_file()
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass

    @classmethod
    async def get_status(cls, port: Optional[int] = None) -> Dict[str, Any]:
        """Fetch real-time status and health check from the proxy gateway."""
        pid = cls.read_pid_file()
        running = pid is not None
        active_port = port or cls._last_known_port

        health_data = None
        health_ok = False

        # Attempt to ping /health endpoint if running or port provided
        health_url = f"http://127.0.0.1:{active_port}/health"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.5)) as session:
                async with session.get(health_url) as resp:
                    if resp.status == 200:
                        health_data = await resp.json()
                        health_ok = True
                        running = True  # Verified alive via HTTP
                        if "upstream" in health_data:
                            cls._last_known_upstream = health_data["upstream"]
        except Exception:
            pass

        fallback_tb = get_persisted_token_budget()
        token_budget = (health_data.get("token_budget") if health_data and "token_budget" in health_data else fallback_tb)
        token_limit = token_budget.get("daily_limit", cls._last_known_token_limit)

        return {
            "running": running,
            "pid": pid,
            "port": active_port,
            "host": DEFAULT_HOST,
            "upstream": (health_data.get("upstream") if health_data else cls._last_known_upstream),
            "token_limit": token_limit,
            "health_ok": health_ok,
            "health": health_data,
            "token_budget": token_budget,
            "log_file": str(get_log_file()),
            "pid_file": str(get_pid_file()),
        }

    @classmethod
    async def start(
        cls,
        port: int = DEFAULT_PROXY_PORT,
        host: str = DEFAULT_HOST,
        upstream: str = DEFAULT_UPSTREAM,
        token_limit: int = DEFAULT_TOKEN_LIMIT,
        db_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Start the proxy server as a background process."""
        # 1. Check if already running
        current_pid = cls.read_pid_file()
        if current_pid:
            # Check if reachable
            status = await cls.get_status(port=port)
            if status["health_ok"] or status["running"]:
                return {
                    "success": True,
                    "message": f"Proxy is already running (PID {current_pid}).",
                    "status": status,
                }

        script_path = get_proxy_script_path()
        if not script_path.exists():
            return {
                "success": False,
                "error": f"Proxy script not found at {script_path}",
            }

        cls._last_known_port = port
        cls._last_known_upstream = upstream
        cls._last_known_token_limit = token_limit

        log_file = get_log_file()
        log_file.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable,
            str(script_path),
            "--port", str(port),
            "--host", host,
            "--upstream", upstream,
            "--token-limit", str(token_limit),
        ]
        if db_path:
            cmd.extend(["--db", str(db_path)])

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as lf:
            lf.write(f"\n\n=== [LLM Telemetry Proxy Launch at {ts}] ===\n")
            lf.write(f"Command: {' '.join(cmd)}\n")
            lf.flush()

        log_out = open(log_file, "a", encoding="utf-8")

        try:
            kwargs: Dict[str, Any] = {
                "stdout": log_out,
                "stderr": subprocess.STDOUT,
                "cwd": str(REPO_ROOT),
            }

            if sys.platform == "win32":
                # Create process detached from console
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                DETACHED_PROCESS = 0x00000008
                kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
            else:
                kwargs["start_new_session"] = True

            proc = subprocess.Popen(cmd, **kwargs)
            cls.write_pid_file(proc.pid)

            # Wait briefly and verify startup
            await asyncio.sleep(1.0)

            # Poll for health check up to 3 seconds
            for _ in range(6):
                status = await cls.get_status(port=port)
                if status["health_ok"]:
                    return {
                        "success": True,
                        "message": f"Proxy successfully started on port {port} (PID {proc.pid}).",
                        "status": status,
                    }
                if not is_pid_alive(proc.pid):
                    break
                await asyncio.sleep(0.5)

            if is_pid_alive(proc.pid):
                status = await cls.get_status(port=port)
                return {
                    "success": True,
                    "message": f"Proxy process launched (PID {proc.pid}), initial status check in progress.",
                    "status": status,
                }
            else:
                cls.clear_pid_file()
                tail = cls.get_logs(lines=15)
                return {
                    "success": False,
                    "error": "Proxy process terminated immediately after launch.",
                    "logs": tail.get("logs", ""),
                }

        except Exception as e:
            cls.clear_pid_file()
            return {
                "success": False,
                "error": f"Failed to spawn proxy process: {str(e)}",
            }
        finally:
            try:
                log_out.close()
            except Exception:
                pass

    @classmethod
    async def stop(cls, force: bool = False) -> Dict[str, Any]:
        """Stop / kill the proxy process."""
        pid = cls.read_pid_file()
        if not pid:
            cls.clear_pid_file()
            return {
                "success": True,
                "message": "Proxy is not currently running.",
            }

        success = kill_pid(pid, force=force)
        cls.clear_pid_file()

        if success:
            return {
                "success": True,
                "message": f"Proxy (PID {pid}) stopped successfully.",
            }
        else:
            return {
                "success": False,
                "error": f"Could not terminate proxy process (PID {pid}).",
            }

    @classmethod
    async def restart(
        cls,
        port: int = DEFAULT_PROXY_PORT,
        host: str = DEFAULT_HOST,
        upstream: str = DEFAULT_UPSTREAM,
        token_limit: int = DEFAULT_TOKEN_LIMIT,
        db_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Restart the proxy server."""
        await cls.stop(force=True)
        await asyncio.sleep(0.8)
        return await cls.start(port=port, host=host, upstream=upstream, token_limit=token_limit, db_path=db_path)

    @classmethod
    def get_logs(cls, lines: int = 150) -> Dict[str, Any]:
        """Read the last N lines of proxy log output."""
        log_file = get_log_file()
        if not log_file.exists():
            return {
                "exists": False,
                "path": str(log_file),
                "logs": "No proxy log file found yet.",
                "total_lines": 0,
            }

        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            tail_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
            return {
                "exists": True,
                "path": str(log_file),
                "logs": "".join(tail_lines),
                "total_lines": len(all_lines),
                "returned_lines": len(tail_lines),
            }
        except Exception as e:
            return {
                "exists": True,
                "path": str(log_file),
                "error": f"Failed to read log file: {e}",
                "logs": "",
            }

    @classmethod
    def clear_logs(cls) -> Dict[str, Any]:
        """Clear proxy logs."""
        log_file = get_log_file()
        try:
            if log_file.exists():
                with open(log_file, "w", encoding="utf-8") as f:
                    f.write(f"=== Log cleared at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            return {"success": True, "message": "Proxy logs cleared."}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @classmethod
    async def run_db_compress(cls) -> Dict[str, Any]:
        """Trigger database compression script in a subprocess."""
        compress_script = get_db_compress_script_path()
        if not compress_script.exists():
            return {
                "success": False,
                "error": f"Compress script not found at {compress_script}",
            }

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(compress_script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(REPO_ROOT),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
            out_str = stdout.decode("utf-8", errors="replace")
            err_str = stderr.decode("utf-8", errors="replace")

            return {
                "success": proc.returncode == 0,
                "exit_code": proc.returncode,
                "output": out_str + ("\nErrors:\n" + err_str if err_str else ""),
            }
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": "Database compression timed out after 120 seconds.",
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to run database compressor: {str(e)}",
            }
