"""Install all required libraries for the Retailer Location Migration pipeline.

Run this file once on a fresh machine. It pip-installs every dependency and
shows a progress bar for download + install.

    python Install_Library.py
"""
import subprocess
import sys
import time

PACKAGES = [
    ("pandas", "pandas>=2.0"),
    ("psycopg2", "psycopg2-binary>=2.9"),
    ("dotenv", "python-dotenv>=1.0"),
]
BAR_WIDTH = 40
SPINNER = ["|", "/", "-", "\\"]


def clear_line():
    sys.stdout.write("\033[2K\r")
    sys.stdout.flush()


def progress_bar(done, total, status=""):
    """Draw a determinate progress bar with a trailing status message."""
    pct = done / total if total else 0
    filled = int(BAR_WIDTH * pct)
    bar = "#" * filled + "-" * (BAR_WIDTH - filled)
    line = f"[{bar}] {int(pct * 100):3d}%  {done}/{total}  {status}"
    sys.stdout.write("\r" + line)
    sys.stdout.flush()


def install_package(spec, status_prefix):
    """pip-install one package, running a spinner and surfacing its last line."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "pip", "install", "--progress-bar", "off", spec],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    last = ""
    frame = 0
    while True:
        line = proc.stdout.readline()
        if line == "" and proc.poll() is not None:
            break
        if line:
            stripped = line.rstrip()
            if stripped:
                last = stripped
        clear_line()
        progress_bar(0, 1, f"   {SPINNER[frame % 4]} {status_prefix} {spec} ...")
        frame += 1
        time.sleep(0.05)
    proc.wait()
    if last:
        clear_line()
        print(f"      {last}")
    return proc.returncode


def main():
    total = len(PACKAGES)
    print("=" * 60)
    print(" Retailer Location Migration - Library Installer")
    print("=" * 60)
    print("Packages to install:")
    for _, spec in PACKAGES:
        print(f"   - {spec}")
    print()

    failed = []
    for idx, (module, spec) in enumerate(PACKAGES):
        print(f"[{idx + 1}/{total}] Installing {spec} ...")
        code = install_package(spec, "Downloading/installing")
        if code != 0:
            failed.append(spec)
            print(f"   FAILED: {spec}")
        clear_line()
        progress_bar(idx + 1, total, f"   Installed {module}")
        time.sleep(0.2)

    clear_line()
    print()
    print("=" * 60)
    if failed:
        print("Some packages FAILED to install:")
        for spec in failed:
            print(f"   - {spec}   (try: python -m pip install --upgrade pip)")
    else:
        print(f"All {total} libraries installed successfully.")
    print("=" * 60)
    print("\nNext step:  python run_pipeline.py --dry-run")
    print("            (or double-click gui_pipeline.py)")
    try:
        input("\nPress Enter to close ...")
    except EOFError:
        pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
