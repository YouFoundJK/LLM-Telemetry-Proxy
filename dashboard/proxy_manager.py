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
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

import aiohttp

DASHBOARD_DIR = Path(__file__).resolve().parent
REPO_ROOT = DASHBOARD_DIR.parent

DEFAULT_PROXY_PORT = 9090
DEFAULT_UPSTREAM = "https://llm.ai.e-infra.cz/v1"
DEFAULT_HOST = "0.0.0.0"


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
    """Terminate or kill a process by PID."""
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


class ProxyManager:
    """Manages the lifecycle and health of the LLM Telemetry Proxy."""

    _last_known_port = DEFAULT_PROXY_PORT
    _last_known_upstream = DEFAULT_UPSTREAM

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

        return {
            "running": running,
            "pid": pid,
            "port": active_port,
            "upstream": (health_data.get("upstream") if health_data else cls._last_known_upstream),
            "health_ok": health_ok,
            "health": health_data,
            "log_file": str(get_log_file()),
            "pid_file": str(get_pid_file()),
        }

    @classmethod
    async def start(
        cls,
        port: int = DEFAULT_PROXY_PORT,
        host: str = DEFAULT_HOST,
        upstream: str = DEFAULT_UPSTREAM,
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

        log_file = get_log_file()
        log_file.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable,
            str(script_path),
            "--port", str(port),
            "--host", host,
            "--upstream", upstream,
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
        db_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Restart the proxy server."""
        await cls.stop(force=True)
        await asyncio.sleep(0.8)
        return await cls.start(port=port, host=host, upstream=upstream, db_path=db_path)

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
