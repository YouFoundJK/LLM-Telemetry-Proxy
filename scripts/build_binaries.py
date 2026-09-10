#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Native Binary Compilation Pipeline for LLM Telemetry Proxy & Dashboard.

Uses Nuitka to compile the Python source code into high-performance,
standalone native machine-code binaries (ELF on Linux, PE on Windows).

Usage:
    python scripts/build_binaries.py [options]
    ./dashboard.sh build [options]

Options:
    --target {proxy,dashboard,all}   Target component to compile (default: proxy)
    --output-dir DIR                Output directory for compiled binaries (default: dist)
    --lto {auto,yes,no}             Enable Link-Time Optimization (default: auto)
    --jobs N                        Number of parallel compilation jobs (default: auto CPU count)
    --clean                         Clean previous build artifacts before compilation

Example:
- python3 scripts/benchmark_monitor.py record --output python_run.csv --duration 1800
"""

import argparse
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"
_print_lock = threading.Lock()


def log(msg: str):
    print(f"\033[1;34m[BUILD]\033[0m {msg}", flush=True)


def log_success(msg: str):
    print(f"\033[1;32m[SUCCESS]\033[0m {msg}", flush=True)


def log_warn(msg: str):
    print(f"\033[1;33m[WARNING]\033[0m {msg}", flush=True)


def log_error(msg: str):
    print(f"\033[1;31m[ERROR]\033[0m {msg}", file=sys.stderr, flush=True)


def check_toolchain() -> bool:
    """Verify C compiler, Python development headers, and Nuitka."""
    ok = True

    # 1. Check for Nuitka
    try:
        import nuitka  # noqa: F401
        log(f"Detected Nuitka version {getattr(nuitka, '__version__', 'installed')}")
    except ImportError:
        log_error("Nuitka is not installed in the current Python environment.")
        print("\nTo install Nuitka and high-performance accelerators, run:\n")
        print("    pip install -r requirements.txt\n    # or: pip install nuitka orjson uvloop\n")
        ok = False

    # 2. Check for C Compiler
    c_compiler = None
    if sys.platform == "win32":
        if shutil.which("cl.exe"):
            c_compiler = "MSVC (cl.exe)"
        elif shutil.which("gcc"):
            c_compiler = f"GCC ({shutil.which('gcc')})"
        elif shutil.which("clang"):
            c_compiler = f"Clang ({shutil.which('clang')})"
    else:
        if shutil.which("gcc"):
            c_compiler = f"GCC ({shutil.which('gcc')})"
        elif shutil.which("clang"):
            c_compiler = f"Clang ({shutil.which('clang')})"

    if c_compiler:
        log(f"Detected C compiler: {c_compiler}")
    else:
        log_error("No C/C++ compiler detected on this system.")
        if sys.platform != "win32":
            print("\nTo install GCC on Ubuntu/Debian, run:\n")
            print("    sudo apt-get update && sudo apt-get install -y gcc g++ python3-dev\n")
        else:
            print("\nPlease install Microsoft C++ Build Tools or MinGW GCC.\n")
        ok = False

    # 3. Check for Python Development Headers on Linux
    if sys.platform != "win32":
        import sysconfig
        include_dir = sysconfig.get_path("include")
        python_h = Path(include_dir) / "Python.h" if include_dir else None
        if not python_h or not python_h.exists():
            # Check default system paths
            ver = f"{sys.version_info.major}.{sys.version_info.minor}"
            system_candidates = [
                Path(f"/usr/include/python{ver}/Python.h"),
                Path(f"/usr/local/include/python{ver}/Python.h"),
                Path(f"/usr/include/python{ver}d/Python.h"),
            ]
            if not any(c.exists() for c in system_candidates):
                log_warn(f"Python.h header not found in {include_dir}. Nuitka may fail without python3-dev.")
                print(f"If build fails, run: sudo apt-get install -y python3-dev (or python{ver}-dev)\n")

    # 4. Check for patchelf on Linux (required for Nuitka standalone ELF packaging)
    if sys.platform.startswith("linux"):
        if not shutil.which("patchelf"):
            log_error("Standalone binary compilation on Linux requires 'patchelf' to bundle shared libraries.")
            print("\nTo install patchelf, run either:\n")
            print("    sudo apt-get update && sudo apt-get install -y patchelf")
            print("    # or inside your venv: pip install patchelf\n")
            ok = False
        else:
            log(f"Detected patchelf: {shutil.which('patchelf')}")

    # 5. Check for ccache (accelerates rebuilds)
    ccache_path = shutil.which("ccache")
    if ccache_path:
        log(f"Detected ccache: {ccache_path} (fast incremental compilation active)")
    else:
        log_warn("ccache not installed. First build is fine, but rebuilds will be slower.")
        if sys.platform != "win32":
            print("    TIP: Run: sudo apt-get install -y ccache\n")

    return ok


def log_target(target_name: str, msg: str, color_code: str = "36"):
    with _print_lock:
        print(f"\033[1;{color_code}m[{target_name}]\033[0m {msg}", flush=True)


def compile_target(
    target_name: str,
    entry_script: Path,
    output_dir: Path,
    lto: str = "auto",
    jobs: int = 0,
    color_code: str = "36",
) -> bool:
    """Compile a Python entry point into a native binary using Nuitka."""
    if not entry_script.exists():
        log_error(f"Entry point not found: {entry_script}")
        return False

    output_dir.mkdir(parents=True, exist_ok=True)
    bin_name = target_name
    exe_suffix = ".exe" if sys.platform == "win32" else ".bin"
    output_bin_name = f"{bin_name}{exe_suffix}"
    build_log_path = output_dir / f"{target_name}_build.log"

    log_target(target_name, f"Starting compilation of {entry_script.name} -> {output_bin_name}", color_code)
    log_target(target_name, f"Verbose build log: {build_log_path}", color_code)

    if jobs <= 0:
        jobs = max(1, os.cpu_count() or 1)

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        f"--output-dir={output_dir}",
        f"--jobs={jobs}",
        "--include-package=proxy",
        "--include-module=_json",
        "--assume-yes-for-downloads",
        "--remove-output",
        f"--output-filename={output_bin_name}",
    ]

    # Enable ccache for fast incremental builds if installed
    if shutil.which("ccache"):
        cmd.append("--enable-ccache")

    # Include high-performance packages in binary if present
    for pkg in ("orjson", "uvloop"):
        try:
            __import__(pkg)
            cmd.append(f"--include-package={pkg}")
        except ImportError:
            pass

    # Configure LTO (Link Time Optimization)
    if lto == "yes":
        cmd.append("--lto=yes")
    elif lto == "no":
        cmd.append("--lto=no")
    else:  # auto
        if sys.platform != "win32":
            cmd.append("--lto=auto")

    # Anti-bloat exclusions to keep binary lean
    cmd.extend([
        "--noinclude-unittest-mode=allow",
        "--noinclude-pytest-mode=allow",
        "--noinclude-setuptools-mode=allow",
    ])

    cmd.append(str(entry_script))

    log_target(target_name, f"Running: {' '.join(cmd[:6])} ... {cmd[-1]}", color_code)
    try:
        with open(build_log_path, "w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in iter(proc.stdout.readline, ""):
                log_file.write(line)
                log_file.flush()
                stripped = line.strip()
                if not stripped:
                    continue
                # Highlight meaningful milestone messages
                if any(k in stripped for k in ("PASS ", "Compiling", "Linking", "Total time", "WARNING:", "ERROR:", "Nuitka: Starting")):
                    log_target(target_name, stripped, color_code)

            proc.stdout.close()
            returncode = proc.wait()

        if returncode != 0:
            log_error(f"Compilation of {target_name} failed with exit code {returncode}. See {build_log_path}")
            return False

        # Locate the compiled executable
        dist_folder = output_dir / f"{entry_script.stem}.dist"
        candidate_bins = [
            dist_folder / output_bin_name,
            dist_folder / bin_name,
            dist_folder / f"{bin_name}.exe",
            output_dir / output_bin_name,
        ]

        compiled_bin = None
        for c in candidate_bins:
            if c.is_file():
                compiled_bin = c
                break

        if not compiled_bin:
            log_error(f"Compiled binary not found in expected paths under {dist_folder}. See {build_log_path}")
            return False

        # Create root-level convenience launcher / symlink in output_dir
        root_bin = output_dir / output_bin_name
        if root_bin != compiled_bin:
            try:
                if root_bin.exists():
                    root_bin.unlink()
                if sys.platform != "win32":
                    try:
                        os.symlink(compiled_bin.relative_to(output_dir), root_bin)
                    except OSError:
                        shutil.copy2(compiled_bin, root_bin)
                else:
                    shutil.copy2(compiled_bin, root_bin)
            except Exception as e:
                log_warn(f"Could not link convenience binary {root_bin}: {e}")

        # Ensure executable permissions on POSIX
        if sys.platform != "win32":
            try:
                os.chmod(compiled_bin, 0o755)
                if root_bin.exists():
                    os.chmod(root_bin, 0o755)
            except Exception:
                pass

        size_mb = compiled_bin.stat().st_size / (1024 * 1024)
        log_success(f"Successfully compiled {target_name}! ({compiled_bin.name}: {size_mb:.1f} MB)")
        return True

    except Exception as e:
        log_error(f"Unexpected error during compilation of {target_name}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Compile LLM Telemetry Proxy and Dashboard to native standalone binaries."
    )
    parser.add_argument(
        "--target",
        choices=["proxy", "dashboard", "all"],
        default="all",
        help="Component to compile (default: all)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(DIST_DIR),
        help="Target directory for compiled outputs (default: dist)",
    )
    parser.add_argument(
        "--lto",
        choices=["auto", "yes", "no"],
        default="auto",
        help="Link-Time Optimization setting (default: auto)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="Number of compilation threads (default: CPU count)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean dist directory before compilation",
    )
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="Compile targets one after another instead of in parallel",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only check toolchain requirements without compiling",
    )

    args = parser.parse_args()

    print("=" * 65)
    print("  LLM Telemetry Proxy — Native Binary Compilation Engine")
    print("=" * 65)

    if not check_toolchain():
        sys.exit(1)

    if args.check_only:
        log_success("Toolchain check passed! Ready to compile.")
        sys.exit(0)

    out_dir = Path(args.output_dir)
    if args.clean and out_dir.exists():
        log(f"Cleaning existing directory: {out_dir}")
        shutil.rmtree(out_dir, ignore_errors=True)

    out_dir.mkdir(parents=True, exist_ok=True)

    targets = []
    colors = ["36", "35", "33", "32"]  # cyan, magenta, yellow, green
    if args.target in ("proxy", "all"):
        targets.append(("llm_telemetry_proxy", REPO_ROOT / "proxy" / "llm_telemetry_proxy.py"))
    if args.target in ("dashboard", "all"):
        targets.append(("dashboard_server", REPO_ROOT / "dashboard" / "server.py"))

    total_cpus = os.cpu_count() or 1
    if args.jobs > 0:
        jobs_per_target = args.jobs
    else:
        # Distribute available CPU cores evenly across concurrent compilation jobs
        jobs_per_target = max(1, total_cpus // len(targets)) if not args.sequential else total_cpus

    success_count = 0
    if len(targets) > 1 and not args.sequential:
        log(f"Parallel compilation active: compiling {len(targets)} targets simultaneously")
        log(f"Allocating {jobs_per_target} C-compiler jobs per target (total system cores: {total_cpus})")

        with ThreadPoolExecutor(max_workers=len(targets)) as executor:
            future_to_target = {}
            for idx, (name, script) in enumerate(targets):
                color = colors[idx % len(colors)]
                future = executor.submit(
                    compile_target,
                    target_name=name,
                    entry_script=script,
                    output_dir=out_dir,
                    lto=args.lto,
                    jobs=jobs_per_target,
                    color_code=color,
                )
                future_to_target[future] = name

            for future in as_completed(future_to_target):
                name = future_to_target[future]
                try:
                    ok = future.result()
                    if ok:
                        success_count += 1
                except Exception as e:
                    log_error(f"Target {name} crashed: {e}")
    else:
        for idx, (name, script) in enumerate(targets):
            color = colors[idx % len(colors)]
            ok = compile_target(
                target_name=name,
                entry_script=script,
                output_dir=out_dir,
                lto=args.lto,
                jobs=jobs_per_target,
                color_code=color,
            )
            if ok:
                success_count += 1

    print("=" * 65)
    if success_count == len(targets):
        log_success("All requested targets compiled successfully!")
        print("\nTo start your pre-compiled proxy, simply run:\n")
        print("    ./dashboard.sh proxy restart")
        print("    # or: ./dashboard.sh start --with-proxy\n")
        print("The control script will automatically prioritize the native binary.\n")
        sys.exit(0)
    else:
        log_error(f"Compilation finished with errors ({success_count}/{len(targets)} succeeded).")
        sys.exit(1)


if __name__ == "__main__":
    main()
