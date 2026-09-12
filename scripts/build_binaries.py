#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Native Compilation Pipeline for LLM Telemetry Proxy & Dashboard.

By default, compiles performance-critical Python modules into high-performance,
native C-extension shared libraries (.so on Linux, .pyd on Windows) using Nuitka.
Python automatically imports these compiled C extensions via ExtensionFileLoader,
giving maximum CPU execution speed and minimum RAM without standalone ELF bloat.

Usage:
    python scripts/build_binaries.py [options]
    ./dashboard.sh build [options]

Options:
    --mode {modules,standalone}      Compilation mode (default: modules - recommended)
    --target {proxy,dashboard,all}   Target component to compile (default: proxy)
    --output-dir DIR                Output directory for standalone binaries (default: dist)
    --lto {auto,yes,no}             Enable Link-Time Optimization (default: auto)
    --jobs N                        Number of parallel compilation jobs (default: auto CPU count)
    --clean                         Clean previous build artifacts before compilation
    --check-only                    Only verify toolchain requirements without compiling
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

PROXY_MODULES = [
    REPO_ROOT / "proxy" / "model_router.py",
    REPO_ROOT / "proxy" / "proxy_forwarder.py",
    REPO_ROOT / "proxy" / "proxy_stream.py",
    REPO_ROOT / "proxy" / "fast_json.py",
    REPO_ROOT / "proxy" / "telemetry_db.py",
    REPO_ROOT / "proxy" / "payload_inspector.py",
]

DASHBOARD_MODULES = [
    REPO_ROOT / "dashboard" / "proxy_manager.py",
]


def log(msg: str):
    print(f"\033[1;34m[BUILD]\033[0m {msg}", flush=True)


def log_success(msg: str):
    print(f"\033[1;32m[SUCCESS]\033[0m {msg}", flush=True)


def log_warn(msg: str):
    print(f"\033[1;33m[WARNING]\033[0m {msg}", flush=True)


def log_error(msg: str):
    print(f"\033[1;31m[ERROR]\033[0m {msg}", file=sys.stderr, flush=True)


def log_target(target_name: str, msg: str, color_code: str = "36"):
    with _print_lock:
        print(f"\033[1;{color_code}m[{target_name}]\033[0m {msg}", flush=True)


def clean_artifacts():
    """Remove all compiled .so, .pyd, .build directories and dist/ artifacts."""
    log("Cleaning build artifacts...")
    cleaned_count = 0

    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR, ignore_errors=True)
        log(f"Removed {DIST_DIR}")
        cleaned_count += 1

    for search_dir in (REPO_ROOT / "proxy", REPO_ROOT / "dashboard"):
        if not search_dir.exists():
            continue
        for ext in ("*.so", "*.pyd"):
            for f in search_dir.glob(ext):
                try:
                    f.unlink(missing_ok=True)
                    log(f"Removed compiled module: {f.relative_to(REPO_ROOT)}")
                    cleaned_count += 1
                except Exception:
                    pass
        for build_dir in search_dir.glob("*.build"):
            try:
                shutil.rmtree(build_dir, ignore_errors=True)
                log(f"Removed build cache: {build_dir.relative_to(REPO_ROOT)}")
                cleaned_count += 1
            except Exception:
                pass

    log_success(f"Cleaned {cleaned_count} artifact(s). Ready for fresh build.")


def check_toolchain(mode: str = "modules") -> bool:
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
            ver = f"{sys.version_info.major}.{sys.version_info.minor}"
            system_candidates = [
                Path(f"/usr/include/python{ver}/Python.h"),
                Path(f"/usr/local/include/python{ver}/Python.h"),
                Path(f"/usr/include/python{ver}d/Python.h"),
            ]
            if not any(c.exists() for c in system_candidates):
                log_warn(f"Python.h header not found in {include_dir}. Nuitka may fail without python3-dev.")
                print(f"If build fails, run: sudo apt-get install -y python3-dev (or python{ver}-dev)\n")

    # 4. Check for patchelf on Linux (only strictly required for standalone ELF packaging)
    if mode == "standalone" and sys.platform.startswith("linux"):
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


def compile_native_module(
    module_path: Path,
    lto: str = "auto",
    jobs: int = 1,
    color_code: str = "36",
) -> bool:
    """
    Compile a single Python module into a native C-extension shared library (.so / .pyd)
    using Nuitka --module mode. Python automatically imports the .so over .py.
    """
    if not module_path.exists():
        log_error(f"Module file not found: {module_path}")
        return False

    module_name = module_path.stem
    output_dir = module_path.parent
    log_target(module_name, f"Compiling native C-extension: {module_path.relative_to(REPO_ROOT)}", color_code)

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--mode=module",
        f"--output-dir={output_dir}",
        f"--jobs={max(1, jobs)}",
        "--remove-output",
        "--no-pyi-file",
        "--python-flag=-OO",
        "--assume-yes-for-downloads",
    ]

    # Link-Time Optimization
    if lto == "yes" or (lto == "auto" and sys.platform != "win32"):
        cmd.append("--lto=yes")
    elif lto == "no":
        cmd.append("--lto=no")

    # ccache support
    env = dict(os.environ)
    ccache_path = shutil.which("ccache")
    if ccache_path:
        env["NUITKA_CCACHE_BINARY"] = ccache_path

    cmd.append(str(module_path))

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        for line in iter(proc.stdout.readline, ""):
            stripped = line.strip()
            if any(k in stripped for k in ("Compiling", "Linking", "Total time", "WARNING:", "ERROR:")):
                log_target(module_name, stripped, color_code)
        proc.stdout.close()
        returncode = proc.wait()

        if returncode != 0:
            log_error(f"Compilation of {module_name} failed with exit code {returncode}.")
            return False

        # Locate the compiled shared library (.so or .pyd)
        compiled_files = list(output_dir.glob(f"{module_name}.*.so")) + \
                         list(output_dir.glob(f"{module_name}.so")) + \
                         list(output_dir.glob(f"{module_name}.*.pyd")) + \
                         list(output_dir.glob(f"{module_name}.pyd"))

        if compiled_files:
            so_file = compiled_files[0]
            size_kb = so_file.stat().st_size / 1024
            log_success(f"Compiled native C-extension: {so_file.name} ({size_kb:.1f} KB)")
            return True
        else:
            log_warn(f"Build succeeded but compiled shared object for {module_name} not found in {output_dir}")
            return True

    except Exception as e:
        log_error(f"Unexpected error compiling {module_name}: {e}")
        return False


def compile_standalone_target(
    target_name: str,
    entry_script: Path,
    output_dir: Path,
    lto: str = "auto",
    jobs: int = 0,
    color_code: str = "36",
) -> bool:
    """Compile a Python entry point into a standalone ELF/PE binary using Nuitka."""
    if not entry_script.exists():
        log_error(f"Entry point not found: {entry_script}")
        return False

    output_dir.mkdir(parents=True, exist_ok=True)
    bin_name = target_name
    exe_suffix = ".exe" if sys.platform == "win32" else ".bin"
    output_bin_name = f"{bin_name}{exe_suffix}"
    build_log_path = output_dir / f"{target_name}_build.log"

    log_target(target_name, f"Starting standalone build of {entry_script.name} -> {output_bin_name}", color_code)

    if jobs <= 0:
        jobs = max(1, os.cpu_count() or 1)

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--deployment",
        f"--output-dir={output_dir}",
        f"--jobs={jobs}",
        "--follow-import-to=proxy",
        "--include-module=_json",
        "--include-package=uvloop",
        "--include-package=orjson",
        "--nofollow-import-to=proxy.db_compress",
        "--nofollow-import-to=proxy.llm_telemetry_query",
        "--python-flag=-OO",
        "--python-flag=no_site",
        "--assume-yes-for-downloads",
        "--remove-output",
        "--no-prefer-source-code",
        f"--output-filename={output_bin_name}",
        f"--report={output_dir / f'{target_name}_compilation_report.xml'}",
    ]

    env = dict(os.environ)
    ccache_path = shutil.which("ccache")
    if ccache_path:
        env["NUITKA_CCACHE_BINARY"] = ccache_path

    if lto == "yes":
        cmd.append("--lto=yes")
    elif lto == "no":
        cmd.append("--lto=no")
    else:
        if sys.platform != "win32":
            cmd.append("--lto=auto")

    cmd.extend([
        "--noinclude-unittest-mode=nofollow",
        "--noinclude-pytest-mode=nofollow",
        "--noinclude-setuptools-mode=nofollow",
        "--noinclude-IPython-mode=nofollow",
        "--nofollow-import-to=doctest",
        "--nofollow-import-to=pydoc",
        "--nofollow-import-to=tkinter",
        "--nofollow-import-to=distutils",
        "--nofollow-import-to=ensurepip",
    ])

    cmd.append(str(entry_script))

    try:
        with open(build_log_path, "w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            for line in iter(proc.stdout.readline, ""):
                log_file.write(line)
                log_file.flush()
                stripped = line.strip()
                if any(k in stripped for k in ("Compiling", "Linking", "Total time", "WARNING:", "ERROR:", "Nuitka: Starting")):
                    log_target(target_name, stripped, color_code)
            proc.stdout.close()
            returncode = proc.wait()

        if returncode != 0:
            log_error(f"Standalone compilation of {target_name} failed. See {build_log_path}")
            return False

        dist_folder = output_dir / f"{entry_script.stem}.dist"
        candidate_bins = [
            dist_folder / output_bin_name,
            dist_folder / bin_name,
            output_dir / output_bin_name,
        ]
        compiled_bin = next((c for c in candidate_bins if c.is_file()), None)

        if compiled_bin:
            size_mb = compiled_bin.stat().st_size / (1024 * 1024)
            log_success(f"Successfully compiled {target_name}! ({compiled_bin.name}: {size_mb:.1f} MB)")
            return True
        return False

    except Exception as e:
        log_error(f"Unexpected error during standalone build of {target_name}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Compile LLM Telemetry Proxy modules to native C-extensions or standalone binaries."
    )
    parser.add_argument(
        "--mode",
        choices=["modules", "standalone"],
        default="modules",
        help="Compilation mode: 'modules' compiles CPU-hot proxy files into native .so C-extensions (recommended: lowest RAM, maximum speed); 'standalone' builds a self-contained ELF executable.",
    )
    parser.add_argument(
        "--target",
        choices=["proxy", "dashboard", "all"],
        default="proxy",
        help="Component to compile (default: proxy)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(DIST_DIR),
        help="Target directory for standalone outputs (default: dist)",
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
        help="Clean previous build artifacts (.so, .pyd, dist/) before compilation",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only check toolchain requirements without compiling",
    )

    args = parser.parse_args()

    print("=" * 68)
    print("  LLM Telemetry Proxy — High-Performance Native Compilation Engine")
    print(f"  Mode: {args.mode.upper()} {'(Native C-Extension Modules)' if args.mode == 'modules' else '(Standalone ELF Binary)'}")
    print("=" * 68)

    if args.clean:
        clean_artifacts()

    if not check_toolchain(mode=args.mode):
        sys.exit(1)

    if args.check_only:
        log_success("Toolchain check passed! Ready to compile.")
        sys.exit(0)

    total_cpus = os.cpu_count() or 1
    jobs = args.jobs if args.jobs > 0 else total_cpus
    colors = ["36", "35", "33", "32", "34", "31"]

    # ── MODE 1: NATIVE C-EXTENSION MODULES (RECOMMENDED) ────────────────────
    if args.mode == "modules":
        modules_to_build = []
        if args.target in ("proxy", "all"):
            modules_to_build.extend(PROXY_MODULES)
        if args.target in ("dashboard", "all"):
            modules_to_build.extend(DASHBOARD_MODULES)

        log(f"Compiling {len(modules_to_build)} modules into native C-extensions with Link-Time Optimization...")
        log(f"Allocating {jobs} compilation threads across {len(modules_to_build)} modules")

        success_count = 0
        with ThreadPoolExecutor(max_workers=min(len(modules_to_build), max(1, total_cpus // 2))) as executor:
            future_to_mod = {}
            for idx, mod in enumerate(modules_to_build):
                c_code = colors[idx % len(colors)]
                f = executor.submit(
                    compile_native_module,
                    module_path=mod,
                    lto=args.lto,
                    jobs=max(1, jobs // len(modules_to_build)),
                    color_code=c_code,
                )
                future_to_mod[f] = mod.stem

            for f in as_completed(future_to_mod):
                name = future_to_mod[f]
                try:
                    if f.result():
                        success_count += 1
                except Exception as e:
                    log_error(f"Module {name} compilation crashed: {e}")

        print("=" * 68)
        if success_count == len(modules_to_build):
            log_success(f"All {success_count} modules compiled to native C-extensions successfully!")
            print("\nYour proxy is now accelerated by native C-extensions.")
            print("Python automatically prioritizes the compiled .so/.pyd modules over .py source.")
            print("\nTo start your accelerated proxy, simply run:\n")
            print("    ./start.sh proxy restart")
            print("    # or: ./dashboard.sh start --with-proxy\n")
            sys.exit(0)
        else:
            log_error(f"Module build finished with errors ({success_count}/{len(modules_to_build)} succeeded).")
            sys.exit(1)

    # ── MODE 2: STANDALONE ELF BINARY ─────────────────────────────────────────
    else:
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        targets = []
        if args.target in ("proxy", "all"):
            targets.append(("llm_telemetry_proxy", REPO_ROOT / "proxy" / "llm_telemetry_proxy.py"))
        if args.target in ("dashboard", "all"):
            targets.append(("dashboard_server", REPO_ROOT / "dashboard" / "server.py"))

        success_count = 0
        for idx, (name, script) in enumerate(targets):
            color = colors[idx % len(colors)]
            ok = compile_standalone_target(
                target_name=name,
                entry_script=script,
                output_dir=out_dir,
                lto=args.lto,
                jobs=jobs,
                color_code=color,
            )
            if ok:
                success_count += 1

        print("=" * 68)
        if success_count == len(targets):
            log_success("Standalone binary compilation completed successfully!")
            sys.exit(0)
        else:
            log_error(f"Standalone compilation failed ({success_count}/{len(targets)} succeeded).")
            sys.exit(1)


if __name__ == "__main__":
    main()
