#!/usr/bin/env python3
"""
MacBook Optimizer
=================

An all-in-one Mac care app (Tkinter) in the spirit of CleanMyMac:

  Smart Scan     One-click health check with a 0-100 score: junk, disk space,
                 memory, startup load, battery, temperature, uptime.
  Performance    Live CPU / memory / disk / battery, heaviest processes,
                 login items and launch agents, RAM purge.
  System Junk    Caches, logs, Mail downloads, saved window state, Xcode
                 caches, Trash - pick what to clear.
  Large & Old    Find big / old files in your folders. Removal goes to the
                 Trash (recoverable).
  Duplicates     Finds identical files (size + content hash). Removal goes to
                 the Trash, and at least one copy of each file is always kept.
  Space Lens     Folder-by-folder view of what is using your storage.
  Uninstaller    Removes an app plus the leftover files it scatters around
                 ~/Library. Everything goes to the Trash.
  Maintenance    Flush DNS, rebuild Spotlight, thin Time Machine local
                 snapshots, verify the startup disk.
  Benchmark      CPU + disk speed vs. an approximate 2015 MacBook Air.

REQUIREMENTS
------------
    macOS, Python 3.9+ with Tk 8.6 (python.org installer), and:
        pip install psutil

RUN
---
    python3 macbook_optimizer.py

PERMISSIONS YOU MAY SEE (all normal)
------------------------------------
    * "Python wants to control Finder / System Events" - used to move files
      to the Trash and read your Login Items. Click OK.
    * Access prompts for Desktop / Documents / Downloads - used by the file
      scanners.
    * A macOS password dialog for Maintenance tasks. That dialog is macOS's
      own; this app never sees or stores your password.
    * Optional: Full Disk Access (System Settings > Privacy & Security) lets
      the cleaner reach protected folders such as Mail Downloads and Trash.

SAFETY MODEL
------------
    * Nothing is deleted without a confirmation dialog showing what and how much.
    * File finders / uninstaller send items to the TRASH, not permanent delete.
    * System Junk only touches specific cache/log/trash folders in ~/Library.
    * Never touches Documents, Photos, Mail messages, or system files.
    * Cleaning RAM and "optimizing" are honest: macOS manages memory itself;
      the real speed wins are free disk space, fewer startup apps, no thermal
      throttling, and the occasional restart. This app measures those.
"""

import hashlib
import os
import platform
import plistlib
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

try:
    import psutil
except ImportError:  # the UI shows a clear message in this case
    psutil = None

APP_NAME = "MacBook Optimizer"
__version__ = "1.0.0"
IS_MAC = platform.system() == "Darwin"
HOME = os.path.expanduser("~")


def resource_path(rel):
    """Path to a bundled file, both when run from source and inside a PyInstaller .app."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


# --------------------------------------------------------------------------
# 2015 MacBook Air reference baseline (approximate ballpark, not an official
# spec sheet - used purely as a "how much faster is this Mac" ruler)
# --------------------------------------------------------------------------
BASELINE = {
    "cpu_model": "Intel Core i5-5250U @ 1.6GHz (dual-core, Broadwell)",
    "ram_gb": 8,
    "ssd_read_mbs": 450.0,
    "ssd_write_mbs": 330.0,
    "hash_ops_per_sec": 550_000,
}

BENCH_SECONDS = 2.0
BENCH_FILE_MB = 128
TICK_MS = 2000
LOW_DISK_WARNING_PCT = 15

# Folders that are really single "packages" - never look inside them, because
# removing a piece of a package (e.g. a Photos library) can corrupt it.
PACKAGE_EXTS = (
    ".app", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".imovielibrary",
    ".fcpbundle", ".sparsebundle", ".bundle", ".framework", ".logicx", ".band",
    ".xcodeproj", ".xcworkspace", ".playground", ".vmwarevm", ".pvm",
)
SKIP_DIR_NAMES = {"node_modules", "__pycache__", "site-packages"}
EXCLUDED_PATHS = {os.path.join(HOME, "Library"), os.path.join(HOME, ".Trash")}

PROTECTED_BUNDLE_IDS = {"com.apple.Safari"}


# ==========================================================================
# Backend helpers (no Tkinter in this section)
# ==========================================================================
def human_bytes(n, binary=False):
    """Format bytes. Decimal (like Finder) by default; binary for RAM."""
    base = 1024.0 if binary else 1000.0
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < base:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= base
    return f"{n:.1f} PB"


def bytes_to_gb(n):
    """Binary GB, which is how RAM sizes are normally quoted (8 GB, 16 GB)."""
    return n / (1024 ** 3)


class CancelToken:
    def __init__(self):
        self._ev = threading.Event()

    def cancel(self):
        self._ev.set()

    @property
    def cancelled(self):
        return self._ev.is_set()


def applescript_quote(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def run_admin_shell(cmd, timeout=900):
    """Run a shell command via macOS's own admin-password dialog. Returns (ok, text)."""
    if not IS_MAC:
        return False, "Only available on macOS."
    script = f'do shell script "{applescript_quote(cmd)}" with administrator privileges'
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return True, r.stdout.strip() or "Done."
        err = r.stderr.strip()
        if "User canceled" in err or "(-128)" in err:
            return False, "Cancelled."
        return False, err or "Command failed."
    except subprocess.TimeoutExpired:
        return False, "Timed out."
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def trash_paths(paths):
    """Move paths to the Trash via Finder (recoverable, supports Put Back).
    Returns (moved_count, errors)."""
    if not paths:
        return 0, []
    if not IS_MAC:
        return 0, ["Moving to Trash is only supported on macOS."]

    def finder_delete(batch):
        items = ", ".join(f'POSIX file "{applescript_quote(p)}"' for p in batch)
        script = f'tell application "Finder" to delete {{{items}}}'
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=180)
        return r.returncode == 0, r.stderr.strip()

    moved, errors = 0, []
    for i in range(0, len(paths), 25):
        batch = paths[i:i + 25]
        try:
            ok, err = finder_delete(batch)
        except Exception as exc:  # noqa: BLE001
            ok, err = False, str(exc)
        if ok:
            moved += len(batch)
            continue
        for p in batch:  # one bad item shouldn't block the rest
            try:
                ok1, err1 = finder_delete([p])
            except Exception as exc:  # noqa: BLE001
                ok1, err1 = False, str(exc)
            if ok1:
                moved += 1
            else:
                errors.append(f"{os.path.basename(p)}: {err1 or err}")
    return moved, errors


def cpu_benchmark(duration=BENCH_SECONDS):
    """Single-core SHA-256 hashing throughput, in hashes/sec."""
    data = os.urandom(1024)
    count = 0
    start = time.perf_counter()
    end = start + duration
    while time.perf_counter() < end:
        hashlib.sha256(data).digest()
        count += 1
    elapsed = time.perf_counter() - start
    return count / elapsed if elapsed > 0 else 0.0


def disk_benchmark(size_mb=BENCH_FILE_MB):
    """Sequential write/read throughput to a temp file, in MB/s."""
    chunk = os.urandom(1024 * 1024)
    tmp_path = os.path.join(tempfile.gettempdir(), "mba_bench_tmp.bin")
    start = time.perf_counter()
    with open(tmp_path, "wb") as f:
        for _ in range(size_mb):
            f.write(chunk)
        f.flush()
        os.fsync(f.fileno())
    write_elapsed = time.perf_counter() - start
    start = time.perf_counter()
    with open(tmp_path, "rb") as f:
        while f.read(1024 * 1024):
            pass
    read_elapsed = time.perf_counter() - start
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    write_mbs = size_mb / write_elapsed if write_elapsed > 0 else 0.0
    read_mbs = size_mb / read_elapsed if read_elapsed > 0 else 0.0
    return write_mbs, read_mbs


def run_purge():
    """Run macOS's built-in `purge`. Returns (ok, message)."""
    try:
        r = subprocess.run(["purge"], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True, "Memory purge completed."
        return False, f"purge exited with code {r.returncode}: {r.stderr.strip()}"
    except FileNotFoundError:
        return False, "Couldn't find the `purge` command (are you on macOS?)."
    except subprocess.TimeoutExpired:
        return False, "purge timed out."
    except Exception as exc:  # noqa: BLE001
        return False, f"purge failed: {exc}"


def get_thermal_state():
    """Parse `pmset -g therm` for CPU thermal-throttling status."""
    try:
        r = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0 or not r.stdout.strip():
            return {"available": False, "throttled": False, "speed_limit": None}
        values = {}
        for line in r.stdout.splitlines():
            if "=" in line:
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                try:
                    values[key] = int(val)
                except ValueError:
                    values[key] = val
        limit = values.get("CPU_Speed_Limit")
        throttled = isinstance(limit, int) and limit < 100
        return {"available": "CPU_Speed_Limit" in values, "throttled": throttled, "speed_limit": limit}
    except Exception:  # noqa: BLE001
        return {"available": False, "throttled": False, "speed_limit": None}


def get_battery_health():
    """Cycle count / condition / max capacity from system_profiler (macOS). May be None."""
    if not IS_MAC:
        return None
    try:
        r = subprocess.run(["system_profiler", "SPPowerDataType"], capture_output=True, text=True, timeout=25)
    except Exception:  # noqa: BLE001
        return None
    info = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("Cycle Count:"):
            info["cycles"] = line.split(":", 1)[1].strip()
        elif line.startswith("Condition:"):
            info["condition"] = line.split(":", 1)[1].strip()
        elif line.startswith("Maximum Capacity:"):
            info["capacity"] = line.split(":", 1)[1].strip()
    return info or None


def get_login_items():
    """The user's visible Login Items (via System Events)."""
    if not IS_MAC:
        return []
    script = 'tell application "System Events" to get the name of every login item'
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return [n.strip() for n in r.stdout.strip().split(",") if n.strip()]
    except Exception:  # noqa: BLE001
        pass
    return []


def get_user_launch_agents():
    path = os.path.join(HOME, "Library", "LaunchAgents")
    if not os.path.isdir(path):
        return []
    return [os.path.join(path, fn) for fn in sorted(os.listdir(path)) if fn.endswith(".plist")]


def load_startup_info(progress=None):
    return get_login_items(), get_user_launch_agents()


def open_login_items_settings():
    for url in ("x-apple.systempreferences:com.apple.LoginItems-Settings.extension",
                "x-apple.systempreferences:com.apple.preference.users"):
        try:
            r = subprocess.run(["open", url], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def reveal_in_finder(path):
    try:
        subprocess.run(["open", "-R", path], timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


def get_uptime_days():
    if psutil is None:
        return None
    try:
        return (time.time() - psutil.boot_time()) / 86400.0
    except Exception:  # noqa: BLE001
        return None


# ---- sizes ---------------------------------------------------------------
def dir_size(path):
    """Slow-but-portable recursive size (fallback when `du` is unavailable)."""
    total = 0
    for dirpath, _dirs, files in os.walk(path, onerror=lambda e: None):
        for fn in files:
            fp = os.path.join(dirpath, fn)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                continue
    return total


def fast_size(path):
    """Disk usage of a file or folder in bytes. Uses `du` (fast) with a fallback."""
    try:
        if not os.path.lexists(path):
            return 0
        if os.path.islink(path) or os.path.isfile(path):
            st = os.lstat(path)
            return (getattr(st, "st_blocks", 0) * 512) or st.st_size
    except OSError:
        return 0
    try:
        r = subprocess.run(["du", "-sk", path], capture_output=True, text=True, timeout=900)
        first = r.stdout.strip().split(None, 1)[0] if r.stdout.strip() else ""
        if first.isdigit():
            return int(first) * 1024
    except Exception:  # noqa: BLE001
        pass
    return dir_size(path)


def clear_directory_contents(path):
    """Delete everything INSIDE `path` (keeps `path`). Returns (freed_bytes, errors)."""
    freed, errors = 0, []
    if not os.path.isdir(path):
        return freed, errors
    try:
        names = os.listdir(path)
    except OSError as exc:
        return freed, [f"{path}: {exc}"]
    for name in names:
        full = os.path.join(path, name)
        try:
            if os.path.islink(full):
                os.remove(full)
            elif os.path.isfile(full):
                size = fast_size(full)
                os.remove(full)
                freed += size
            elif os.path.isdir(full):
                size = fast_size(full)
                shutil.rmtree(full)
                freed += size
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    return freed, errors


# ---- System Junk -----------------------------------------------------------
def build_cleanup_categories():
    lib = os.path.join(HOME, "Library")
    dev_paths = [
        os.path.join(lib, "Developer", "Xcode", "DerivedData"),
        os.path.join(lib, "Developer", "Xcode", "iOS DeviceSupport"),
        os.path.join(lib, "Developer", "CoreSimulator", "Caches"),
    ]
    cats = [
        dict(id="caches", name="App caches", default=True,
             desc="Temporary files apps rebuild on their own (~/Library/Caches)",
             paths=[os.path.join(lib, "Caches")]),
        dict(id="logs", name="Logs & crash reports", default=True,
             desc="Diagnostic logs apps wrote (~/Library/Logs)",
             paths=[os.path.join(lib, "Logs")]),
        dict(id="mail", name="Mail downloads", default=True,
             desc="Local copies of attachments you opened in Mail",
             paths=[os.path.join(lib, "Containers", "com.apple.mail", "Data", "Library", "Mail Downloads")]),
        dict(id="state", name="Saved window state", default=True,
             desc="Window-restore data from apps (~/Library/Saved Application State)",
             paths=[os.path.join(lib, "Saved Application State")]),
        dict(id="dev", name="Xcode & simulator caches", default=True,
             desc="Derived data, device support files, simulator caches",
             paths=[p for p in dev_paths if os.path.isdir(p)]),
        dict(id="trash", name="Trash", default=False,
             desc="Items you already deleted - removing them is permanent",
             paths=[os.path.join(HOME, ".Trash")]),
    ]
    return [c for c in cats if any(os.path.isdir(p) for p in c["paths"])]


def scan_junk(progress):
    cats = build_cleanup_categories()
    sizes = {}
    for c in cats:
        progress(f"Measuring {c['name']}…")
        sizes[c["id"]] = sum(fast_size(p) for p in c["paths"] if os.path.exists(p))
    return cats, sizes


def clean_categories(cats, progress):
    freed, errors = 0, []
    for c in cats:
        progress(f"Cleaning {c['name']}…")
        for p in c["paths"]:
            f, e = clear_directory_contents(p)
            freed += f
            errors.extend(e)
    return freed, errors


# ---- File finders ----------------------------------------------------------
def _skip_dir(name, full):
    low = name.lower()
    return (low.endswith(PACKAGE_EXTS) or name in SKIP_DIR_NAMES
            or name.startswith(".") or full in EXCLUDED_PATHS)


def iter_files(roots, cancel=None):
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if cancel is not None and cancel.cancelled:
                return
            dirnames[:] = [d for d in dirnames if not _skip_dir(d, os.path.join(dirpath, d))]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                yield os.path.join(dirpath, fn)


def scan_large_files(roots, min_bytes, older_than_days, cancel, progress, limit=500):
    cutoff = time.time() - older_than_days * 86400 if older_than_days else None
    results, seen = [], 0
    for path in iter_files(roots, cancel):
        seen += 1
        if seen % 400 == 0:
            progress(f"Scanned {seen:,} files…")
        try:
            if os.path.islink(path):
                continue
            st = os.stat(path)
        except OSError:
            continue
        if st.st_size < min_bytes:
            continue
        if cutoff is not None and st.st_mtime > cutoff:
            continue
        results.append({"path": path, "size": st.st_size, "mtime": st.st_mtime})
    results.sort(key=lambda d: d["size"], reverse=True)
    return results[:limit]


def _hash_file(path, partial=False):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if partial:
            h.update(f.read(65536))
            try:
                f.seek(-65536, os.SEEK_END)
                h.update(f.read(65536))
            except OSError:
                pass  # file shorter than 64 KB
        else:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
    return h.hexdigest()


def scan_duplicates(roots, min_bytes, cancel, progress):
    """Groups of identical files: size match -> partial hash -> full hash."""
    by_size, seen_inodes, n = {}, set(), 0
    for path in iter_files(roots, cancel):
        n += 1
        if n % 400 == 0:
            progress(f"Scanned {n:,} files…")
        try:
            if os.path.islink(path):
                continue
            st = os.stat(path)
        except OSError:
            continue
        if st.st_size < min_bytes:
            continue
        key = (st.st_dev, st.st_ino)
        if key in seen_inodes:  # hard links are the same data, not duplicates
            continue
        seen_inodes.add(key)
        by_size.setdefault(st.st_size, []).append((path, st.st_mtime))

    candidates = [(s, v) for s, v in by_size.items() if len(v) > 1]
    total = sum(len(v) for _s, v in candidates)
    done, by_partial = 0, {}
    for size, items in candidates:
        for path, mtime in items:
            if cancel.cancelled:
                return []
            done += 1
            if done % 10 == 0:
                progress(f"Comparing files… {done:,}/{total:,}")
            try:
                by_partial.setdefault((size, _hash_file(path, partial=True)), []).append((path, mtime))
            except OSError:
                continue

    groups = []
    for (size, _ph), items in by_partial.items():
        if len(items) < 2:
            continue
        by_full = {}
        for path, mtime in items:
            if cancel.cancelled:
                return []
            try:
                by_full.setdefault(_hash_file(path), []).append(
                    {"path": path, "size": size, "mtime": mtime})
            except OSError:
                continue
        for files in by_full.values():
            if len(files) > 1:
                files.sort(key=lambda d: d["mtime"])
                groups.append(files)
    groups.sort(key=lambda g: g[0]["size"] * (len(g) - 1), reverse=True)
    return groups


def list_children_sizes(root, cancel, progress):
    """Immediate children of `root` with their disk usage (Space Lens)."""
    entries = []
    for name in sorted(os.listdir(root)):
        if cancel.cancelled:
            break
        full = os.path.join(root, name)
        progress(f"Measuring {name}…")
        try:
            is_dir = os.path.isdir(full) and not os.path.islink(full)
            size = fast_size(full)
        except OSError:
            continue
        entries.append({"name": name, "path": full, "size": size, "is_dir": is_dir})
    entries.sort(key=lambda e: e["size"], reverse=True)
    return entries


# ---- Uninstaller -----------------------------------------------------------
APP_DIRS = ["/Applications", os.path.join(HOME, "Applications")]


def _read_app_info(app_path):
    try:
        with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f)
    except Exception:  # noqa: BLE001
        return {}


def list_apps():
    apps = []
    for base in APP_DIRS:
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if not name.endswith(".app"):
                continue
            path = os.path.join(base, name)
            info = _read_app_info(path)
            bid = info.get("CFBundleIdentifier", "") or ""
            apps.append({
                "name": name[:-4], "path": path, "bundle_id": bid,
                "version": str(info.get("CFBundleShortVersionString", "") or ""),
                "protected": bid in PROTECTED_BUNDLE_IDS,
            })
    return apps


def compute_app_sizes(apps, progress):
    sizes = {}
    for i, a in enumerate(apps):
        if i % 5 == 0:
            progress(f"Measuring apps… {i}/{len(apps)}")
        sizes[a["path"]] = fast_size(a["path"])
    return sizes


def find_app_leftovers(app):
    """Files an app left in ~/Library. Matching is deliberately strict:
    exact bundle-id names (+ known suffixes) and exact app-name folders."""
    bid = (app.get("bundle_id") or "").lower()
    name = app["name"].lower()
    lib = os.path.join(HOME, "Library")
    found = []

    def add(p):
        if p not in found:
            found.append(p)

    suffixes = (".plist", ".savedstate", ".binarycookies", ".lockfile")
    for sub in ("Application Support", "Caches", "Containers", "Logs", "Preferences",
                "Saved Application State", "HTTPStorages", "WebKit", "Cookies",
                "Application Scripts", "LaunchAgents", os.path.join("Preferences", "ByHost")):
        d = os.path.join(lib, sub)
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for e in entries:
            el = e.lower()
            hit = False
            if bid:
                if el == bid or any(el == bid + s for s in suffixes):
                    hit = True
                elif sub.endswith("ByHost") and el.startswith(bid + ".") and el.endswith(".plist"):
                    hit = True
            if (not hit and sub in ("Application Support", "Caches", "Logs")
                    and len(name) >= 3 and el == name):
                hit = True
            if hit:
                add(os.path.join(d, e))
    if bid:
        gc = os.path.join(lib, "Group Containers")
        try:
            for e in os.listdir(gc):
                el = e.lower()
                if el == bid or el.endswith("." + bid) or el == "group." + bid:
                    add(os.path.join(gc, e))
        except OSError:
            pass
    return found


def leftovers_with_sizes(app, progress=None):
    return [(p, fast_size(p)) for p in find_app_leftovers(app)]


def is_app_running(app_path):
    if psutil is None:
        return False
    try:
        for p in psutil.process_iter(["exe"]):
            if (p.info.get("exe") or "").startswith(app_path + "/"):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


# ---- Smart Scan ------------------------------------------------------------
def gather_smart_scan(progress):
    m = {}
    progress("Checking disk space…")
    if psutil is not None:
        try:
            d = psutil.disk_usage("/")
            m.update(disk_total=d.total, disk_free=d.free, disk_free_pct=100 - d.percent)
        except Exception:  # noqa: BLE001
            pass
        progress("Checking memory…")
        try:
            vm, sw = psutil.virtual_memory(), psutil.swap_memory()
            m.update(ram_pct=vm.percent, ram_total=vm.total, ram_avail=vm.available, swap_used=sw.used)
        except Exception:  # noqa: BLE001
            pass
        try:
            m["battery"] = psutil.sensors_battery()
        except Exception:  # noqa: BLE001
            m["battery"] = None
    m["uptime_days"] = get_uptime_days()

    progress("Checking temperature…")
    therm = get_thermal_state()
    m.update(therm_available=therm["available"], throttled=therm["throttled"],
             speed_limit=therm["speed_limit"])

    progress("Reading startup items…")
    logins, agents = load_startup_info()
    m.update(login_items=logins, login_count=len(logins), agent_count=len(agents))

    progress("Checking battery health…")
    m["battery_health"] = get_battery_health()

    progress("Looking for junk files…")
    junk = trash = 0
    for c in build_cleanup_categories():
        size = sum(fast_size(p) for p in c["paths"] if os.path.exists(p))
        if c["id"] == "trash":
            trash = size
        elif c["default"]:
            junk += size
    m.update(junk=junk, trash=trash)
    return m


def compute_health_score(m):
    """Heuristic 0-100 score. Transparent on purpose - it is not magic."""
    score = 100
    free = m.get("disk_free_pct")
    if free is not None:
        score -= 30 if free < 10 else 20 if free < LOW_DISK_WARNING_PCT else 8 if free < 25 else 0
    swap_gb = (m.get("swap_used") or 0) / 1e9
    score -= 10 if swap_gb > 2 else 5 if swap_gb > 0.5 else 0
    ram = m.get("ram_pct")
    if ram is not None:
        score -= 8 if ram > 92 else 3 if ram > 85 else 0
    if m.get("throttled"):
        score -= 20
    up = m.get("uptime_days")
    if up is not None:
        score -= 10 if up > 30 else 5 if up > 14 else 0
    junk_gb = (m.get("junk") or 0) / 1e9
    score -= 8 if junk_gb > 5 else 4 if junk_gb > 1 else 0
    logins = m.get("login_count", 0)
    score -= 8 if logins > 10 else 4 if logins > 5 else 0
    bh = m.get("battery_health") or {}
    cond = bh.get("condition")
    if cond and cond.lower() not in ("normal", "good"):
        score -= 10
    return max(0, min(100, score))


# ==========================================================================
# Design system
# ==========================================================================
BG = "#f3f4fb"
CARD = "#ffffff"
BORDER = "#e1e3f0"
TEXT = "#1c1d2e"
MUTED = "#6c708a"
ACCENT = "#6c4cf5"
ACCENT_HOVER = "#5a3be0"
ACCENT_SOFT = "#e9e5ff"
GOOD = "#18a56b"
WARN = "#e8960c"
BAD = "#e0454b"
SIDEBAR = "#1d1a38"
SIDEBAR_HOVER = "#2b2750"
SIDEBAR_ACTIVE = "#3b3573"
SIDEBAR_TEXT = "#b9b6dc"

CHECK_ON = "☑"
CHECK_OFF = "☐"
STATUS_COLORS = {"muted": MUTED, "ok": GOOD, "warn": WARN, "err": BAD}

F = {}


def init_fonts():
    family = tkfont.nametofont("TkDefaultFont").actual("family")
    F.update(
        base=(family, 12), small=(family, 11), bold=(family, 12, "bold"),
        small_bold=(family, 11, "bold"), tiny=(family, 10, "bold"),
        title=(family, 24, "bold"), h2=(family, 15, "bold"),
        big=(family, 26, "bold"), huge=(family, 52, "bold"),
        mono=("Menlo" if IS_MAC else "Courier", 11),
    )


class FlatButton(tk.Label):
    """A flat, colourable button (native macOS buttons ignore colours)."""
    STYLES = {
        "primary": (ACCENT, ACCENT_HOVER, "#ffffff"),
        "secondary": ("#e8eaf6", "#dadef2", TEXT),
        "danger": (BAD, "#c8353b", "#ffffff"),
        "ghost": (CARD, "#eef0fa", ACCENT),
    }

    def __init__(self, master, text, command=None, style="primary", padx=16, pady=8, font=None):
        bg, _hover, fg = self.STYLES[style]
        self._cursor = "pointinghand" if IS_MAC else "hand2"
        super().__init__(master, text=text, bg=bg, fg=fg, font=font or F["bold"],
                         padx=padx, pady=pady, cursor=self._cursor)
        self._colors = self.STYLES[style]
        self._command = command
        self._enabled = True
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_enter(self, _e):
        if self._enabled:
            self.config(bg=self._colors[1])

    def _on_leave(self, _e):
        if self._enabled:
            self.config(bg=self._colors[0])

    def _on_release(self, e):
        if not self._enabled or self._command is None:
            return
        if 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self._command()

    def set_command(self, command):
        self._command = command

    def set_style(self, style):
        self._colors = self.STYLES[style]
        if self._enabled:
            self.config(bg=self._colors[0], fg=self._colors[2])

    def set_enabled(self, enabled):
        self._enabled = enabled
        if enabled:
            self.config(bg=self._colors[0], fg=self._colors[2], cursor=self._cursor)
        else:
            self.config(bg="#dcdee9", fg="#9a9db4", cursor="arrow")


class Card(tk.Frame):
    """White rounded-ish panel. Put content in `.inner`."""

    def __init__(self, master, padx=16, pady=14):
        super().__init__(master, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        self.inner = tk.Frame(self, bg=CARD)
        self.inner.pack(fill="both", expand=True, padx=padx, pady=pady)


class MiniBar(tk.Canvas):
    def __init__(self, master, height=8):
        super().__init__(master, height=height, width=10, bg=CARD, highlightthickness=0)
        self._frac = 0.0
        self._color = ACCENT
        self._h = height
        self.bind("<Configure>", lambda e: self._redraw())

    def set(self, frac, color=ACCENT):
        self._frac = max(0.0, min(1.0, frac))
        self._color = color
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        self.create_rectangle(0, 0, w, self._h, fill="#eceef8", outline="")
        if self._frac > 0:
            self.create_rectangle(0, 0, w * self._frac, self._h, fill=self._color, outline="")


class StatTile(Card):
    def __init__(self, master, title, bar=True):
        super().__init__(master, padx=14, pady=12)
        tk.Label(self.inner, text=title.upper(), font=F["tiny"], bg=CARD, fg=MUTED).pack(anchor="w")
        self.value = tk.Label(self.inner, text="—", font=F["big"], bg=CARD, fg=TEXT)
        self.value.pack(anchor="w")
        self.sub = tk.Label(self.inner, text=" ", font=F["small"], bg=CARD, fg=MUTED,
                            justify="left", wraplength=190)
        self.sub.pack(anchor="w", pady=(0, 6))
        self.bar = MiniBar(self.inner) if bar else None
        if self.bar is not None:
            self.bar.pack(fill="x")

    def set(self, value, sub="", frac=0.0, color=ACCENT):
        self.value.config(text=value)
        self.sub.config(text=sub or " ")
        if self.bar is not None:
            self.bar.set(frac, color)


class HealthRing(tk.Canvas):
    def __init__(self, master, size=230):
        super().__init__(master, width=size, height=size, bg=BG, highlightthickness=0)
        self.size = size
        self.show(None, "—", "Run Smart Scan")

    def show(self, score, center, sub, color=ACCENT):
        self.delete("all")
        s, pad = self.size, 18
        box = (pad, pad, s - pad, s - pad)
        self.create_oval(*box, outline="#dfe1f1", width=14)
        if score is not None:
            extent = -min(359.9, max(1.0, score * 3.6))
            self.create_arc(*box, start=90, extent=extent, style="arc", outline=color, width=14)
        self.create_text(s / 2, s / 2 - 12, text=center, font=F["huge"], fill=TEXT)
        self.create_text(s / 2, s / 2 + 38, text=sub, font=F["small"], fill=MUTED)


class CheckTree(tk.Frame):
    """Treeview with an optional clickable ☐/☑ first column."""

    def __init__(self, master, columns, height=10, checkable=True,
                 on_change=None, on_select=None, on_double=None):
        super().__init__(master, bg=CARD)
        self.checkable = checkable
        self.on_change, self.on_select, self.on_double = on_change, on_select, on_double
        self.checked = set()
        ids = (["sel"] if checkable else []) + [c[0] for c in columns]
        self.tree = ttk.Treeview(self, columns=ids, show="headings", height=height, selectmode="browse")
        if checkable:
            self.tree.heading("sel", text="")
            self.tree.column("sel", width=36, anchor="center", stretch=False)
        for cid, text, width, anchor, stretch in columns:
            self.tree.heading(cid, text=text, anchor=anchor)
            self.tree.column(cid, width=width, anchor=anchor, stretch=stretch)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("odd", background="#f7f8fd")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

    # -- content
    def clear(self):
        self.tree.delete(*self.tree.get_children())
        self.checked.clear()
        self._notify()

    def add(self, iid, values, checked=False, tags=()):
        vals = ((CHECK_ON if checked else CHECK_OFF,) if self.checkable else ()) + tuple(values)
        self.tree.insert("", "end", iid=iid, values=vals, tags=tags)
        if checked:
            self.checked.add(iid)

    def set_value(self, iid, column, value):
        self.tree.set(iid, column, value)

    def selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def select(self, iid):
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)

    def remove(self, iid):
        if self.tree.exists(iid):
            self.tree.delete(iid)
        self.checked.discard(iid)

    def children(self):
        return list(self.tree.get_children())

    # -- checking
    def get_checked(self):
        return [i for i in self.tree.get_children() if i in self.checked]

    def set_checked(self, iid, flag, notify=True):
        if flag:
            self.checked.add(iid)
        else:
            self.checked.discard(iid)
        if self.checkable and self.tree.exists(iid):
            self.tree.set(iid, "sel", CHECK_ON if flag else CHECK_OFF)
        if notify:
            self._notify()

    def set_all(self, flag):
        for iid in self.tree.get_children():
            self.set_checked(iid, flag, notify=False)
        self._notify()

    def _notify(self):
        if self.on_change:
            self.on_change()

    # -- events
    def _on_click(self, event):
        if not self.checkable:
            return None
        if self.tree.identify("region", event.x, event.y) != "cell":
            return None
        if self.tree.identify_column(event.x) != "#1":
            return None
        iid = self.tree.identify_row(event.y)
        if iid:
            self.set_checked(iid, iid not in self.checked)
        return "break"

    def _on_double(self, event):
        if not self.on_double:
            return
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        if self.checkable and self.tree.identify_column(event.x) == "#1":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self.on_double(iid)

    def _on_select(self, _event):
        if self.on_select:
            iid = self.selected()
            if iid:
                self.on_select(iid)


class LocationPicker(tk.Frame):
    OPTIONS = ["Common folders", "Downloads", "Documents", "Desktop", "Movies",
               "Pictures", "Entire home folder", "Choose folder…"]

    def __init__(self, master):
        super().__init__(master, bg=CARD)
        tk.Label(self, text="Where", font=F["small"], bg=CARD, fg=MUTED).pack(side="left")
        self.var = tk.StringVar(value=self.OPTIONS[0])
        self.combo = ttk.Combobox(self, textvariable=self.var, values=self.OPTIONS,
                                  state="readonly", width=19)
        self.combo.pack(side="left", padx=(6, 0))
        self.combo.bind("<<ComboboxSelected>>", self._on_select)
        self.mode = self.OPTIONS[0]
        self._last = self.OPTIONS[0]
        self.custom = None

    def _on_select(self, _e):
        choice = self.var.get()
        if choice == "Choose folder…":
            path = filedialog.askdirectory(initialdir=HOME)
            if path:
                self.custom, self.mode = path, "custom"
                short = f"📁 {os.path.basename(path.rstrip('/')) or path}"
                self.combo.config(values=self.OPTIONS + [short])
                self.var.set(short)
                self._last = short
            else:
                self.var.set(self._last)  # cancelled: restore previous choice
        else:
            self.mode = choice
            self._last = choice

    def roots(self):
        j = os.path.join
        if self.mode == "custom" and self.custom:
            return [self.custom]
        if self.mode == "Entire home folder":
            return [HOME]
        if self.mode in ("Downloads", "Documents", "Desktop", "Movies", "Pictures"):
            return [j(HOME, self.mode)]
        return [j(HOME, d) for d in ("Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures")]


class NavItem(tk.Frame):
    def __init__(self, master, icon, text, command):
        super().__init__(master, bg=SIDEBAR, cursor="pointinghand" if IS_MAC else "hand2")
        self.icon = tk.Label(self, text=icon, font=F["bold"], bg=SIDEBAR, fg=SIDEBAR_TEXT, width=2)
        self.label = tk.Label(self, text=text, font=F["base"], bg=SIDEBAR, fg=SIDEBAR_TEXT, anchor="w")
        self.icon.pack(side="left", padx=(14, 6), pady=9)
        self.label.pack(side="left", fill="x", expand=True)
        self.active = False
        self._command = command
        for w in (self, self.icon, self.label):
            w.bind("<Enter>", self._enter)
            w.bind("<Leave>", self._leave)
            w.bind("<Button-1>", lambda e: self._command())

    def _paint(self, bg, fg):
        for w in (self, self.icon, self.label):
            w.config(bg=bg)
        self.icon.config(fg=fg)
        self.label.config(fg=fg)

    def _refresh(self):
        if self.active:
            self._paint(SIDEBAR_ACTIVE, "#ffffff")
        else:
            self._paint(SIDEBAR, SIDEBAR_TEXT)

    def _enter(self, _e):
        if not self.active:
            self._paint(SIDEBAR_HOVER, "#ffffff")

    def _leave(self, _e):
        self._refresh()

    def set_active(self, flag):
        self.active = flag
        self._refresh()


class InsightCard(Card):
    COLORS = {"good": GOOD, "warn": WARN, "bad": BAD, "info": "#9a9db4"}

    def __init__(self, master, title):
        super().__init__(master, padx=14, pady=9)
        row = self.inner
        row.columnconfigure(1, weight=1)
        self.dot = tk.Label(row, text="●", font=F["h2"], bg=CARD, fg="#c9ccdf")
        self.dot.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        tk.Label(row, text=title, font=F["bold"], bg=CARD, fg=TEXT, anchor="w").grid(
            row=0, column=1, sticky="w")
        self.detail = tk.Label(row, text="Waiting for scan…", font=F["small"], bg=CARD,
                               fg=MUTED, anchor="w", justify="left", wraplength=430)
        self.detail.grid(row=1, column=1, sticky="w")
        self.action = FlatButton(row, "Review", None, style="ghost", padx=12, pady=5)
        self.action.grid(row=0, column=2, rowspan=2, padx=(10, 0))
        self.action.grid_remove()

    def set(self, status, detail, action_text=None, action_cmd=None):
        self.dot.config(fg=self.COLORS.get(status, MUTED))
        self.detail.config(text=detail)
        if action_text:
            self.action.config(text=action_text)
            self.action.set_command(action_cmd)
            self.action.grid()
        else:
            self.action.grid_remove()


class Page(tk.Frame):
    title = ""
    subtitle = ""

    def __init__(self, app):
        super().__init__(app.content, bg=BG)
        self.app = app
        self.status = None
        self.spinner = None
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=28, pady=(22, 2))
        tk.Label(head, text=self.title, font=F["title"], bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(head, text=self.subtitle, font=F["base"], bg=BG, fg=MUTED,
                 justify="left", wraplength=760).pack(anchor="w", pady=(2, 0))
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=28, pady=(10, 16))
        self.build()

    # hooks
    def build(self):
        pass

    def on_show(self):
        pass

    def refresh(self):
        pass

    # helpers
    def make_footer(self, parent):
        f = tk.Frame(parent, bg=BG)
        self.status = tk.Label(f, text="", font=F["small"], bg=BG, fg=MUTED, anchor="w",
                               justify="left", wraplength=640)
        self.status.pack(side="left")
        self.spinner = ttk.Progressbar(f, mode="indeterminate", length=110,
                                       style="Accent.Horizontal.TProgressbar")
        return f

    def say(self, text, kind="muted"):
        if self.status is not None:
            self.status.config(text=text, fg=STATUS_COLORS.get(kind, MUTED))

    def set_busy(self, busy):
        if self.spinner is None:
            return
        if busy:
            self.spinner.pack(side="right")
            self.spinner.start(12)
        else:
            self.spinner.stop()
            self.spinner.pack_forget()


# ==========================================================================
# Pages: Smart Scan, Performance, System Junk
# ==========================================================================
class SmartScanPage(Page):
    title = "Smart Scan"
    subtitle = "A quick health check: junk, disk space, memory, startup load, battery and temperature."

    def build(self):
        self._busy = False
        self._first = True
        self.body.columnconfigure(1, weight=1)
        left = tk.Frame(self.body, bg=BG)
        left.grid(row=0, column=0, sticky="n", padx=(0, 28))
        self.ring = HealthRing(left)
        self.ring.pack()
        self.scan_btn = FlatButton(left, "Run Smart Scan", self.start_scan, padx=30, pady=11)
        self.scan_btn.pack(pady=(14, 8))
        self.status = tk.Label(left, text="", font=F["small"], bg=BG, fg=MUTED,
                               wraplength=230, justify="center")
        self.status.pack()

        right = tk.Frame(self.body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        self.cards = {}
        for key, title in (("junk", "System junk"), ("disk", "Disk space"), ("memory", "Memory"),
                           ("startup", "Startup items"), ("battery", "Battery"),
                           ("thermal", "Temperature"), ("uptime", "Uptime")):
            card = InsightCard(right, title)
            card.pack(fill="x", pady=4)
            self.cards[key] = card

    def on_show(self):
        if self._first:
            self._first = False
            self.after(300, self.start_scan)

    def start_scan(self):
        if self._busy:
            return
        self._busy = True
        self.scan_btn.set_enabled(False)
        self.ring.show(None, "…", "Scanning")
        self.say("Starting…")
        self.app.run_bg(gather_smart_scan, self._done, lambda msg: self.say(msg))

    def _done(self, m, err):
        self._busy = False
        self.scan_btn.set_enabled(True)
        if err is not None:
            self.ring.show(None, "!", "Scan failed")
            self.say(f"Scan failed: {err}", "err")
            return
        score = compute_health_score(m)
        if score >= 90:
            label, color = "Excellent", GOOD
        elif score >= 75:
            label, color = "Good", GOOD
        elif score >= 55:
            label, color = "Fair", WARN
        else:
            label, color = "Needs attention", BAD
        self.ring.show(score, str(score), label, color)
        self.say("Finished at " + datetime.now().strftime("%H:%M"), "ok")
        self._fill_cards(m)

    def _fill_cards(self, m):
        go = self.app.show_page
        c = self.cards

        junk, trash = m.get("junk", 0), m.get("trash", 0)
        text = f"{human_bytes(junk)} of caches, logs and other safe-to-remove files"
        if trash > 50_000_000:
            text += f" · {human_bytes(trash)} in the Trash"
        c["junk"].set("good" if junk < 500_000_000 else "warn" if junk < 5_000_000_000 else "bad",
                      text, "Clean up", lambda: go("junk"))

        free = m.get("disk_free_pct")
        if free is None:
            c["disk"].set("info", "Unavailable (is psutil installed?)")
        else:
            st = "bad" if free < 10 else "warn" if free < LOW_DISK_WARNING_PCT else "good"
            c["disk"].set(st, f"{free:.0f}% free · {human_bytes(m.get('disk_free', 0))} of "
                              f"{human_bytes(m.get('disk_total', 0))}", "Space Lens", lambda: go("lens"))

        ram = m.get("ram_pct")
        if ram is None:
            c["memory"].set("info", "Unavailable (is psutil installed?)")
        else:
            swap = m.get("swap_used", 0)
            st = "warn" if (swap > 2_000_000_000 or ram > 92) else "good"
            c["memory"].set(st, f"{ram:.0f}% in use · {human_bytes(m.get('ram_avail', 0), True)} available "
                                f"· swap {human_bytes(swap)}", "Performance", lambda: go("perf"))

        n, a = m.get("login_count", 0), m.get("agent_count", 0)
        c["startup"].set("warn" if n > 8 else "good",
                         f"{n} login item{'s' if n != 1 else ''} · {a} background launch agent"
                         f"{'s' if a != 1 else ''}", "Review", lambda: go("perf"))

        b, bh = m.get("battery"), m.get("battery_health") or {}
        if b is None:
            c["battery"].set("info", "No battery detected")
        else:
            bits = [f"{b.percent:.0f}% charged"]
            if bh.get("condition"):
                bits.append(f"condition: {bh['condition']}")
            if bh.get("cycles"):
                bits.append(f"{bh['cycles']} cycles")
            if bh.get("capacity"):
                bits.append(f"max capacity {bh['capacity']}")
            cond = (bh.get("condition") or "normal").lower()
            c["battery"].set("good" if cond in ("normal", "good") else "warn", " · ".join(bits))

        if not m.get("therm_available"):
            c["thermal"].set("info", "Temperature status isn't available on this Mac")
        elif m.get("throttled"):
            c["thermal"].set("bad", f"CPU is being throttled to {m.get('speed_limit')}% - check vents, "
                                    "dust and heavy background load")
        else:
            c["thermal"].set("good", "Normal - no thermal throttling detected")

        up = m.get("uptime_days")
        if up is None:
            c["uptime"].set("info", "Unavailable")
        elif up > 14:
            c["uptime"].set("warn", f"Up for {up:.0f} days - a restart clears built-up memory and "
                                    "applies pending updates")
        else:
            c["uptime"].set("good", f"Up for {up:.1f} days")


class PerformancePage(Page):
    title = "Performance"
    subtitle = "Live stats, the heaviest processes, and what starts when you log in."

    def build(self):
        self._sort = "mem"
        self._ticks = 0
        self._therm = None
        self._bhealth = None
        self._loaded = False
        self._agent_paths = []
        self._ram_before = 0

        tiles = tk.Frame(self.body, bg=BG)
        tiles.pack(fill="x")
        self.tile = {}
        for i, (key, title) in enumerate((("cpu", "CPU"), ("mem", "Memory"),
                                          ("disk", "Disk"), ("batt", "Battery"))):
            tiles.columnconfigure(i, weight=1, uniform="tile")
            t = StatTile(tiles, title)
            t.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            self.tile[key] = t

        strip = Card(self.body, padx=14, pady=10)
        strip.pack(fill="x", pady=(10, 0))
        s = strip.inner
        self.thermal_lbl = tk.Label(s, text="Temperature: …", font=F["small"], bg=CARD, fg=MUTED)
        self.thermal_lbl.pack(side="left", padx=(0, 18))
        self.uptime_lbl = tk.Label(s, text="Uptime: …", font=F["small"], bg=CARD, fg=MUTED)
        self.uptime_lbl.pack(side="left", padx=(0, 18))
        self.swap_lbl = tk.Label(s, text="Swap: …", font=F["small"], bg=CARD, fg=MUTED)
        self.swap_lbl.pack(side="left")
        FlatButton(s, "Refresh", self.refresh, style="secondary", padx=12, pady=5).pack(side="right")
        FlatButton(s, "Free up RAM", self._on_free_ram, padx=12, pady=5).pack(side="right", padx=(0, 8))

        self.make_footer(self.body).pack(side="bottom", fill="x", pady=(8, 0))

        main = tk.Frame(self.body, bg=BG)
        main.pack(fill="both", expand=True, pady=(10, 0))
        main.columnconfigure(0, weight=3, uniform="m")
        main.columnconfigure(1, weight=2, uniform="m")
        main.rowconfigure(0, weight=1)

        left = Card(main, padx=12, pady=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        hdr = tk.Frame(left.inner, bg=CARD)
        hdr.pack(fill="x", pady=(0, 8))
        tk.Label(hdr, text="Top processes", font=F["h2"], bg=CARD, fg=TEXT).pack(side="left")
        self.sort_cpu = FlatButton(hdr, "CPU", lambda: self._set_sort("cpu"), style="secondary",
                                   padx=10, pady=3, font=F["small_bold"])
        self.sort_cpu.pack(side="right")
        self.sort_mem = FlatButton(hdr, "Memory", lambda: self._set_sort("mem"), style="primary",
                                   padx=10, pady=3, font=F["small_bold"])
        self.sort_mem.pack(side="right", padx=(0, 6))
        self.proc_tree = CheckTree(
            left.inner,
            [("pid", "PID", 56, "e", False), ("name", "Process", 190, "w", True),
             ("cpu", "CPU %", 62, "e", False), ("mem", "Memory", 84, "e", False)],
            checkable=False, height=8)
        self.proc_tree.pack(fill="both", expand=True)
        row = tk.Frame(left.inner, bg=CARD)
        row.pack(fill="x", pady=(8, 0))
        FlatButton(row, "Quit selected…", self._on_kill, style="secondary", padx=12, pady=5).pack(side="left")
        tk.Label(row, text="  Only quit things you recognise.", font=F["small"],
                 bg=CARD, fg=MUTED).pack(side="left")

        right = Card(main, padx=12, pady=12)
        right.grid(row=0, column=1, sticky="nsew")
        tk.Label(right.inner, text="Startup items", font=F["h2"], bg=CARD, fg=TEXT).pack(anchor="w")
        tk.Label(right.inner, text="Login items", font=F["small_bold"], bg=CARD, fg=MUTED).pack(
            anchor="w", pady=(8, 2))
        self.login_list = self._listbox(right.inner, 4)
        tk.Label(right.inner, text="Background launch agents (~/Library/LaunchAgents)",
                 font=F["small_bold"], bg=CARD, fg=MUTED).pack(anchor="w", pady=(8, 2))
        self.agent_list = self._listbox(right.inner, 5)
        btns = tk.Frame(right.inner, bg=CARD)
        btns.pack(fill="x", pady=(8, 0))
        FlatButton(btns, "Login Items settings", self._on_open_login_settings, style="secondary",
                   padx=10, pady=5, font=F["small_bold"]).pack(side="left")
        FlatButton(btns, "Reveal", self._on_reveal_agent, style="secondary", padx=10, pady=5,
                   font=F["small_bold"]).pack(side="left", padx=(6, 0))

    @staticmethod
    def _listbox(parent, height):
        lb = tk.Listbox(parent, height=height, bg=CARD, fg=TEXT, font=F["small"], relief="flat",
                        highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER,
                        selectbackground=ACCENT_SOFT, selectforeground=TEXT, activestyle="none",
                        exportselection=False)
        lb.pack(fill="x")
        return lb

    # ---- lifecycle
    def on_show(self):
        if not self._loaded:
            self._loaded = True
            self.app.run_bg(load_startup_info, self._startup_done)
            self.app.run_bg(lambda p: get_battery_health(), self._battery_done)
        self.refresh()

    def _battery_done(self, res, err):
        self._bhealth = res if err is None else None

    def _startup_done(self, res, err):
        self.login_list.delete(0, "end")
        self.agent_list.delete(0, "end")
        if err is not None:
            self.login_list.insert("end", "(couldn't read login items)")
            return
        logins, agents = res
        for item in logins or ["(none found, or macOS blocked access)"]:
            self.login_list.insert("end", item)
        self._agent_paths = agents
        for path in agents or []:
            self.agent_list.insert("end", os.path.basename(path))
        if not agents:
            self.agent_list.insert("end", "(none)")

    # ---- live data
    @staticmethod
    def _load_color(frac):
        return BAD if frac > 0.9 else WARN if frac > 0.75 else ACCENT

    def refresh(self):
        if psutil is None:
            for t in self.tile.values():
                t.set("n/a", "Run: pip install psutil", 0)
            return
        self._ticks += 1
        cpu = psutil.cpu_percent(interval=None)
        self.tile["cpu"].set(f"{cpu:.0f}%", f"{psutil.cpu_count(logical=True)} logical cores",
                             cpu / 100, self._load_color(cpu / 100))
        vm = psutil.virtual_memory()
        self.tile["mem"].set(f"{vm.percent:.0f}%",
                             f"{bytes_to_gb(vm.used):.1f} of {bytes_to_gb(vm.total):.0f} GB used",
                             vm.percent / 100, BAD if vm.percent > 92 else WARN if vm.percent > 85 else ACCENT)
        try:
            d = psutil.disk_usage("/")
            free = 100 - d.percent
            self.tile["disk"].set(f"{free:.0f}% free", f"{human_bytes(d.free)} of {human_bytes(d.total)} free",
                                  d.percent / 100,
                                  BAD if free < 10 else WARN if free < LOW_DISK_WARNING_PCT else ACCENT)
        except Exception:  # noqa: BLE001
            self.tile["disk"].set("n/a")
        try:
            b = psutil.sensors_battery()
        except Exception:  # noqa: BLE001
            b = None
        if b is None:
            self.tile["batt"].set("—", "No battery detected", 0)
        else:
            sub = "charging" if b.power_plugged else "on battery"
            bh = self._bhealth or {}
            extra = [x for x in (f"{bh['cycles']} cycles" if bh.get("cycles") else "", bh.get("condition", "")) if x]
            if extra:
                sub += " · " + ", ".join(extra)
            self.tile["batt"].set(f"{b.percent:.0f}%", sub, b.percent / 100,
                                  BAD if (b.percent < 20 and not b.power_plugged) else GOOD)

        if self._therm is None or self._ticks % 3 == 1:
            self._therm = get_thermal_state()
        t = self._therm
        if not t["available"]:
            self.thermal_lbl.config(text="Temperature: status unavailable", fg=MUTED)
        elif t["throttled"]:
            self.thermal_lbl.config(text=f"Temperature: THROTTLED to {t['speed_limit']}% - check vents/dust/load",
                                    fg=BAD)
        else:
            self.thermal_lbl.config(text="Temperature: normal, no throttling", fg=GOOD)
        up = get_uptime_days()
        if up is not None:
            self.uptime_lbl.config(text=f"Uptime: {up:.1f} days" + (" (restart recommended)" if up > 14 else ""),
                                   fg=WARN if up > 14 else MUTED)
        try:
            self.swap_lbl.config(text=f"Swap used: {human_bytes(psutil.swap_memory().used)}")
        except Exception:  # noqa: BLE001
            pass
        self._refresh_processes()

    def _set_sort(self, key):
        self._sort = key
        self.sort_mem.set_style("primary" if key == "mem" else "secondary")
        self.sort_cpu.set_style("primary" if key == "cpu" else "secondary")
        if psutil is not None:
            self._refresh_processes()

    def _refresh_processes(self):
        rows = []
        for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
            i = p.info
            mi = i.get("memory_info")
            rows.append((i["pid"], i.get("name") or "?", i.get("cpu_percent") or 0.0, mi.rss if mi else 0))
        rows.sort(key=lambda r: r[3] if self._sort == "mem" else r[2], reverse=True)
        keep = self.proc_tree.selected()
        self.proc_tree.clear()
        for n, (pid, name, cpu, mem) in enumerate(rows[:12]):
            self.proc_tree.add(str(pid), (pid, name, f"{cpu:.1f}", human_bytes(mem, True)),
                               tags=("odd",) if n % 2 else ())
        if keep:
            self.proc_tree.select(keep)

    # ---- actions
    def _on_kill(self):
        iid = self.proc_tree.selected()
        if not iid or psutil is None:
            messagebox.showinfo("Quit process", "Select a process in the list first.")
            return
        pid = int(iid)
        name = self.proc_tree.tree.set(iid, "name")
        if not messagebox.askyesno(
                "Quit process", f"Quit “{name}” (PID {pid})?\n\nOnly do this for apps you recognise - "
                                "quitting a system process can cause problems."):
            return
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                proc.kill()
            self.say(f"Quit {name} (PID {pid}).", "ok")
        except psutil.NoSuchProcess:
            self.say("That process had already quit.")
        except psutil.AccessDenied:
            messagebox.showerror("Quit process", "Permission denied - this looks like a system process.")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Quit process", f"Couldn't quit it: {exc}")
        self._refresh_processes()

    def _on_free_ram(self):
        if psutil is None:
            self.say("Install psutil first: pip install psutil", "err")
            return
        self._ram_before = psutil.virtual_memory().available
        self.say("Freeing memory…")
        self.set_busy(True)
        self.app.run_bg(lambda p: run_purge(), self._purge_done)

    def _purge_done(self, res, err):
        self.set_busy(False)
        if err is not None:
            self.say(str(err), "err")
            return
        ok, msg = res
        if ok:
            self._report_purge()
        elif "Couldn't find" in msg:
            self.say(msg, "err")
        elif messagebox.askyesno("Administrator needed",
                                 "macOS needs your password to free memory on this Mac.\n\n"
                                 "Try again with the password prompt?"):
            self.say("Waiting for password…")
            self.set_busy(True)
            self.app.run_bg(lambda p: run_admin_shell("/usr/sbin/purge"), self._admin_purge_done)
        else:
            self.say(msg, "warn")

    def _admin_purge_done(self, res, err):
        self.set_busy(False)
        if err is not None:
            self.say(str(err), "err")
        elif res[0]:
            self._report_purge()
        else:
            self.say(res[1], "err")

    def _report_purge(self):
        after = psutil.virtual_memory().available
        delta = after - self._ram_before
        sign = "+" if delta >= 0 else "-"
        self.say(f"Memory purged. Available: {human_bytes(self._ram_before, True)} → "
                 f"{human_bytes(after, True)} ({sign}{human_bytes(abs(delta), True)}). "
                 "macOS refills its caches on its own, so expect this to be temporary.", "ok")

    def _on_open_login_settings(self):
        if not open_login_items_settings():
            messagebox.showinfo("Login Items", "Open System Settings → General → Login Items.")

    def _on_reveal_agent(self):
        sel = self.agent_list.curselection()
        if sel and sel[0] < len(self._agent_paths):
            reveal_in_finder(self._agent_paths[sel[0]])
        else:
            messagebox.showinfo("Reveal", "Select a launch agent in the list first.")


class JunkPage(Page):
    title = "System Junk"
    subtitle = ("Caches, logs and leftovers that are safe to clear. "
                "Tick what you want gone, then clean.")

    def build(self):
        self.cats = {}
        self._sizes = {}
        self._first = True
        self._busy = False
        self._pending = None
        top = Card(self.body, padx=16, pady=12)
        top.pack(fill="x")
        t = top.inner
        t.columnconfigure(0, weight=1)
        self.total_lbl = tk.Label(t, text="Scanning…", font=F["h2"], bg=CARD, fg=TEXT, anchor="w")
        self.total_lbl.grid(row=0, column=0, sticky="w")
        self.scan_btn = FlatButton(t, "Rescan", self.scan, style="secondary")
        self.scan_btn.grid(row=0, column=1, padx=(8, 0))
        self.clean_btn = FlatButton(t, "Clean selected", self.clean)
        self.clean_btn.grid(row=0, column=2, padx=(8, 0))
        self.clean_btn.set_enabled(False)

        self.make_footer(self.body).pack(side="bottom", fill="x", pady=(8, 0))
        card = Card(self.body, padx=8, pady=8)
        card.pack(fill="both", expand=True, pady=(10, 0))
        self.tree = CheckTree(card.inner,
                              [("name", "Category", 190, "w", False), ("desc", "What it is", 380, "w", True),
                               ("size", "Size", 90, "e", False)],
                              on_change=self._update_total, height=8)
        self.tree.pack(fill="both", expand=True)

    def on_show(self):
        if self._first:
            self._first = False
            self.scan()

    def _lock(self, busy):
        self._busy = busy
        self.set_busy(busy)
        self.scan_btn.set_enabled(not busy)
        self.clean_btn.set_enabled(not busy and bool(self.cats))

    def scan(self):
        if self._busy:
            return
        self._lock(True)
        self.say("Scanning…")
        self.app.run_bg(scan_junk, self._scan_done, lambda m: self.say(m))

    def _scan_done(self, res, err):
        if err is not None:
            self._lock(False)
            self.say(f"Scan failed: {err}", "err")
            return
        cats, sizes = res
        self.cats = {c["id"]: c for c in cats}
        self._sizes = sizes
        self.tree.clear()
        for n, c in enumerate(cats):
            sz = sizes.get(c["id"], 0)
            self.tree.add(c["id"], (c["name"], c["desc"], human_bytes(sz)),
                          checked=bool(c["default"] and sz > 0), tags=("odd",) if n % 2 else ())
        self._update_total()
        self._lock(False)
        found = f"Found {human_bytes(sum(sizes.values()))} across {len(cats)} locations."
        if self._pending:
            msg, kind = self._pending
            self._pending = None
            self.say(f"{msg} {found}", kind)
        else:
            self.say(found + " Trash is never pre-selected.", "ok")

    def _update_total(self):
        total = sum(self._sizes.get(i, 0) for i in self.tree.checked)
        self.total_lbl.config(text=f"Selected: {human_bytes(total)}")

    def clean(self):
        chosen = [self.cats[i] for i in self.tree.get_checked() if i in self.cats]
        if not chosen:
            messagebox.showinfo("Clean", "Tick at least one category first.")
            return
        total = sum(self._sizes.get(c["id"], 0) for c in chosen)
        listing = "\n".join(f"  • {c['name']} ({human_bytes(self._sizes.get(c['id'], 0))})" for c in chosen)
        extra = "\n\nThe Trash will be emptied permanently." if any(c["id"] == "trash" for c in chosen) else ""
        if not messagebox.askyesno(
                "Confirm cleanup",
                f"Delete the contents of:\n\n{listing}\n\nTotal: {human_bytes(total)}{extra}\n\n"
                "This can't be undone. Continue?"):
            return
        self._lock(True)
        self.say("Cleaning…")
        self.app.run_bg(lambda p: clean_categories(chosen, p), self._clean_done, lambda m: self.say(m))

    def _clean_done(self, res, err):
        self._lock(False)
        if err is not None:
            self.say(f"Cleanup failed: {err}", "err")
            return
        freed, errors = res
        msg = f"Freed about {human_bytes(freed)}."
        if errors:
            msg += (f" {len(errors)} item(s) were skipped (in use or protected). To include protected "
                    "folders, give Terminal Full Disk Access in System Settings → Privacy & Security.")
        self._pending = (msg, "warn" if errors else "ok")
        self.scan()


# ==========================================================================
# Pages: Large & Old Files, Duplicates, Space Lens, Uninstaller
# ==========================================================================
def fmt_date(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%b %d, %Y")
    except (OverflowError, OSError, ValueError):
        return ""


def short_path(p):
    return p.replace(HOME, "~", 1) if p.startswith(HOME) else p


def trash_hint(errors):
    msg = f" {len(errors)} item(s) couldn't be moved ({errors[0]})."
    msg += " If macOS asked to let this app control Finder, click OK and try again."
    return msg


class LargeFilesPage(Page):
    title = "Large & Old Files"
    subtitle = "Find big files hiding in your folders. Removed files go to the Trash, so you can restore them."
    SIZES = {"50 MB": 50e6, "100 MB": 100e6, "250 MB": 250e6, "500 MB": 500e6, "1 GB": 1e9}
    AGES = {"Any age": None, "Older than 30 days": 30, "Older than 6 months": 182, "Older than 1 year": 365}

    def build(self):
        self.items = {}
        self._busy = False
        self._token = None
        ctl = Card(self.body, padx=14, pady=10)
        ctl.pack(fill="x")
        row = ctl.inner
        self.loc = LocationPicker(row)
        self.loc.pack(side="left")
        tk.Label(row, text="Bigger than", font=F["small"], bg=CARD, fg=MUTED).pack(side="left", padx=(14, 6))
        self.size_var = tk.StringVar(value="100 MB")
        ttk.Combobox(row, textvariable=self.size_var, values=list(self.SIZES),
                     state="readonly", width=7).pack(side="left")
        self.age_var = tk.StringVar(value="Any age")
        ttk.Combobox(row, textvariable=self.age_var, values=list(self.AGES),
                     state="readonly", width=18).pack(side="left", padx=(8, 0))
        self.scan_btn = FlatButton(row, "Scan", self.scan, padx=18, pady=6)
        self.scan_btn.pack(side="right")
        self.cancel_btn = FlatButton(row, "Cancel", self.cancel, style="secondary", padx=14, pady=6)

        self.make_footer(self.body).pack(side="bottom", fill="x", pady=(8, 0))
        actions = tk.Frame(self.body, bg=BG)
        actions.pack(side="bottom", fill="x", pady=(8, 0))
        FlatButton(actions, "Reveal in Finder", self._reveal, style="secondary").pack(side="left")
        FlatButton(actions, "Clear ticks", lambda: self.tree.set_all(False), style="secondary").pack(
            side="left", padx=8)
        self.sum_lbl = tk.Label(actions, text="", font=F["bold"], bg=BG, fg=TEXT)
        self.sum_lbl.pack(side="left", padx=12)
        self.trash_btn = FlatButton(actions, "Move ticked to Trash", self._trash, style="danger")
        self.trash_btn.pack(side="right")

        card = Card(self.body, padx=8, pady=8)
        card.pack(fill="both", expand=True, pady=(10, 0))
        self.tree = CheckTree(
            card.inner,
            [("name", "Name", 250, "w", True), ("size", "Size", 90, "e", False),
             ("mod", "Modified", 100, "w", False), ("folder", "Folder", 250, "w", True)],
            on_change=self._update_sum, on_double=self._reveal, height=10)
        self.tree.pack(fill="both", expand=True)

    def _lock(self, busy):
        self._busy = busy
        self.set_busy(busy)
        self.scan_btn.set_enabled(not busy)
        if busy:
            self.cancel_btn.pack(side="right", padx=(0, 8))
        else:
            self.cancel_btn.pack_forget()

    def cancel(self):
        if self._token is not None:
            self._token.cancel()
            self.say("Cancelling…")

    def scan(self):
        if self._busy:
            return
        roots = self.loc.roots()
        min_bytes = int(self.SIZES[self.size_var.get()])
        age = self.AGES[self.age_var.get()]
        token = self._token = CancelToken()
        self._lock(True)
        self.say("Scanning…")
        self.app.run_bg(lambda p: scan_large_files(roots, min_bytes, age, token, p),
                        self._scan_done, lambda m: self.say(m))

    def _scan_done(self, res, err):
        cancelled = self._token is not None and self._token.cancelled
        self._lock(False)
        if err is not None:
            self.say(f"Scan failed: {err}", "err")
            return
        self.tree.clear()
        self.items = {}
        for n, it in enumerate(res):
            iid = str(n)
            self.items[iid] = it
            self.tree.add(iid, (os.path.basename(it["path"]), human_bytes(it["size"]), fmt_date(it["mtime"]),
                                short_path(os.path.dirname(it["path"]))), tags=("odd",) if n % 2 else ())
        total = sum(i["size"] for i in res)
        prefix = "Cancelled - partial results. " if cancelled else ""
        self.say(f"{prefix}{len(res)} files · {human_bytes(total)} total. "
                 "Double-click a row to reveal it in Finder.", "warn" if cancelled else "ok")
        self._update_sum()

    def _update_sum(self):
        ids = [i for i in self.tree.checked if i in self.items]
        total = sum(self.items[i]["size"] for i in ids)
        self.sum_lbl.config(text=f"{len(ids)} ticked · {human_bytes(total)}" if ids else "")

    def _reveal(self, iid=None):
        iid = iid or self.tree.selected()
        if iid in self.items:
            reveal_in_finder(self.items[iid]["path"])
        else:
            messagebox.showinfo("Reveal", "Select a file first.")

    def _trash(self):
        ids = [i for i in self.tree.get_checked() if i in self.items]
        if not ids:
            messagebox.showinfo("Move to Trash", "Tick the files you want to remove first.")
            return
        paths = [self.items[i]["path"] for i in ids]
        total = sum(self.items[i]["size"] for i in ids)
        if self.app.confirm_and_trash(paths, total, f"{len(paths)} file{'s' if len(paths) != 1 else ''}",
                                      self._trash_done):
            self.set_busy(True)
            self.say("Moving to Trash…")

    def _trash_done(self, res, err):
        self.set_busy(False)
        if err is not None:
            self.say(f"Couldn't move to Trash: {err}", "err")
            return
        moved, errors = res
        for iid in list(self.items):
            if not os.path.exists(self.items[iid]["path"]):
                self.tree.remove(iid)
                del self.items[iid]
        self._update_sum()
        msg = f"Moved {moved} file{'s' if moved != 1 else ''} to the Trash."
        self.say(msg + (trash_hint(errors) if errors else ""), "warn" if errors else "ok")


class DuplicatesPage(Page):
    title = "Duplicates"
    subtitle = ("Finds files with identical content. At least one copy of each is always kept, "
                "and removals go to the Trash.")
    SIZES = {"1 MB": 1e6, "10 MB": 10e6, "50 MB": 50e6, "100 MB": 100e6}

    def build(self):
        self.items = {}
        self._busy = False
        self._token = None
        ctl = Card(self.body, padx=14, pady=10)
        ctl.pack(fill="x")
        row = ctl.inner
        self.loc = LocationPicker(row)
        self.loc.pack(side="left")
        tk.Label(row, text="Bigger than", font=F["small"], bg=CARD, fg=MUTED).pack(side="left", padx=(14, 6))
        self.size_var = tk.StringVar(value="1 MB")
        ttk.Combobox(row, textvariable=self.size_var, values=list(self.SIZES),
                     state="readonly", width=7).pack(side="left")
        self.scan_btn = FlatButton(row, "Scan", self.scan, padx=18, pady=6)
        self.scan_btn.pack(side="right")
        self.cancel_btn = FlatButton(row, "Cancel", self.cancel, style="secondary", padx=14, pady=6)

        self.make_footer(self.body).pack(side="bottom", fill="x", pady=(8, 0))
        actions = tk.Frame(self.body, bg=BG)
        actions.pack(side="bottom", fill="x", pady=(8, 0))
        FlatButton(actions, "Auto-select (keep oldest)", self._auto_select, style="secondary").pack(side="left")
        FlatButton(actions, "Clear ticks", lambda: self.tree.set_all(False), style="secondary").pack(
            side="left", padx=8)
        FlatButton(actions, "Reveal", self._reveal, style="secondary").pack(side="left")
        self.sum_lbl = tk.Label(actions, text="", font=F["bold"], bg=BG, fg=TEXT)
        self.sum_lbl.pack(side="left", padx=12)
        FlatButton(actions, "Move ticked to Trash", self._trash, style="danger").pack(side="right")

        card = Card(self.body, padx=8, pady=8)
        card.pack(fill="both", expand=True, pady=(10, 0))
        self.tree = CheckTree(
            card.inner,
            [("name", "Name", 220, "w", True), ("grp", "Group", 56, "e", False),
             ("size", "Size", 84, "e", False), ("mod", "Modified", 96, "w", False),
             ("folder", "Folder", 240, "w", True)],
            on_change=self._update_sum, on_double=self._reveal, height=10)
        self.tree.pack(fill="both", expand=True)

    def _lock(self, busy):
        self._busy = busy
        self.set_busy(busy)
        self.scan_btn.set_enabled(not busy)
        if busy:
            self.cancel_btn.pack(side="right", padx=(0, 8))
        else:
            self.cancel_btn.pack_forget()

    def cancel(self):
        if self._token is not None:
            self._token.cancel()
            self.say("Cancelling…")

    def scan(self):
        if self._busy:
            return
        roots = self.loc.roots()
        min_bytes = int(self.SIZES[self.size_var.get()])
        token = self._token = CancelToken()
        self._lock(True)
        self.say("Scanning…")
        self.app.run_bg(lambda p: scan_duplicates(roots, min_bytes, token, p),
                        self._scan_done, lambda m: self.say(m))

    def _scan_done(self, groups, err):
        cancelled = self._token is not None and self._token.cancelled
        self._lock(False)
        if err is not None:
            self.say(f"Scan failed: {err}", "err")
            return
        self.tree.clear()
        self.items = {}
        if cancelled:
            self.say("Cancelled.", "warn")
            return
        for g, files in enumerate(groups):
            for f, it in enumerate(files):
                iid = f"{g}:{f}"
                self.items[iid] = dict(it, group=g, index=f)
                self.tree.add(iid, (os.path.basename(it["path"]), g + 1, human_bytes(it["size"]),
                                    fmt_date(it["mtime"]), short_path(os.path.dirname(it["path"]))),
                              tags=("odd",) if g % 2 else ())
        reclaim = sum(files[0]["size"] * (len(files) - 1) for files in groups)
        if groups:
            self.say(f"{len(groups)} sets of duplicates · up to {human_bytes(reclaim)} reclaimable. "
                     "Use Auto-select, or tick copies yourself.", "ok")
        else:
            self.say("No duplicates found in that location.", "ok")
        self._update_sum()

    def _auto_select(self):
        for iid, it in self.items.items():
            self.tree.set_checked(iid, it["index"] != 0, notify=False)
        self.tree._notify()

    def _update_sum(self):
        ids = [i for i in self.tree.checked if i in self.items]
        total = sum(self.items[i]["size"] for i in ids)
        self.sum_lbl.config(text=f"{len(ids)} ticked · {human_bytes(total)}" if ids else "")

    def _reveal(self, iid=None):
        iid = iid or self.tree.selected()
        if iid in self.items:
            reveal_in_finder(self.items[iid]["path"])
        else:
            messagebox.showinfo("Reveal", "Select a file first.")

    def _trash(self):
        checked = {i for i in self.tree.get_checked() if i in self.items}
        if not checked:
            messagebox.showinfo("Move to Trash", "Tick the copies you want to remove first "
                                                 "(or use Auto-select).")
            return
        by_group = {}
        for iid, it in self.items.items():
            by_group.setdefault(it["group"], set()).add(iid)
        for g, ids in by_group.items():
            if ids <= checked:
                messagebox.showwarning("Keep one copy",
                                       f"Every copy in group {g + 1} is ticked. Untick at least one "
                                       "so a copy is kept.")
                return
        paths = [self.items[i]["path"] for i in checked]
        total = sum(self.items[i]["size"] for i in checked)
        if self.app.confirm_and_trash(paths, total, f"{len(paths)} duplicate file{'s' if len(paths) != 1 else ''}",
                                      self._trash_done):
            self.set_busy(True)
            self.say("Moving to Trash…")

    def _trash_done(self, res, err):
        self.set_busy(False)
        if err is not None:
            self.say(f"Couldn't move to Trash: {err}", "err")
            return
        moved, errors = res
        for iid in list(self.items):
            if not os.path.exists(self.items[iid]["path"]):
                self.tree.remove(iid)
                del self.items[iid]
        counts = {}
        for it in self.items.values():
            counts[it["group"]] = counts.get(it["group"], 0) + 1
        for iid in list(self.items):  # a lone survivor is no longer a duplicate
            if counts[self.items[iid]["group"]] < 2:
                self.tree.remove(iid)
                del self.items[iid]
        self._update_sum()
        msg = f"Moved {moved} file{'s' if moved != 1 else ''} to the Trash."
        self.say(msg + (trash_hint(errors) if errors else ""), "warn" if errors else "ok")


class SpaceLensPage(Page):
    title = "Space Lens"
    subtitle = "See what's using your storage. Double-click a folder to dive in."

    def build(self):
        self.path = HOME
        self.entries = {}
        self._token = None
        self._scan_id = 0
        self._first = True
        nav = Card(self.body, padx=14, pady=10)
        nav.pack(fill="x")
        row = nav.inner
        FlatButton(row, "↑ Up", self.go_up, style="secondary", padx=12, pady=5).pack(side="left")
        for label, p in (("Home", HOME), ("Applications", "/Applications"), ("Macintosh HD", "/")):
            FlatButton(row, label, lambda p=p: self.open(p), style="secondary", padx=12, pady=5).pack(
                side="left", padx=(6, 0))
        FlatButton(row, "Rescan", lambda: self.open(self.path), padx=14, pady=5).pack(side="right")
        self.cancel_btn = FlatButton(row, "Cancel", self.cancel, style="secondary", padx=12, pady=5)

        self.path_lbl = tk.Label(self.body, text=short_path(HOME), font=F["bold"], bg=BG, fg=TEXT, anchor="w")
        self.path_lbl.pack(fill="x", pady=(10, 0))
        self.make_footer(self.body).pack(side="bottom", fill="x", pady=(8, 0))
        actions = tk.Frame(self.body, bg=BG)
        actions.pack(side="bottom", fill="x", pady=(8, 0))
        FlatButton(actions, "Reveal in Finder", self._reveal, style="secondary").pack(side="left")
        card = Card(self.body, padx=8, pady=8)
        card.pack(fill="both", expand=True, pady=(6, 0))
        self.tree = CheckTree(
            card.inner,
            [("name", "Name", 330, "w", True), ("size", "Size", 100, "e", False),
             ("share", "Share of this folder", 260, "w", False)],
            checkable=False, on_double=self._dive, height=12)
        self.tree.pack(fill="both", expand=True)

    def on_show(self):
        if self._first:
            self._first = False
            self.open(HOME)

    def cancel(self):
        if self._token is not None:
            self._token.cancel()
            self.say("Cancelling…")

    def go_up(self):
        if self.path != "/":
            self.open(os.path.dirname(self.path.rstrip("/")) or "/")

    def open(self, path):
        if self._token is not None:
            self._token.cancel()  # abandon any scan still running
        self.path = path
        self.path_lbl.config(text=short_path(path))
        self._scan_id += 1
        sid = self._scan_id
        token = self._token = CancelToken()
        self.tree.clear()
        self.entries = {}
        self.set_busy(True)
        self.cancel_btn.pack(side="right", padx=(0, 8))
        self.say("Measuring… big folders can take a little while.")
        self.app.run_bg(lambda p: list_children_sizes(path, token, p),
                        lambda res, err: self._done(sid, token, res, err),
                        lambda m: self.say(m) if sid == self._scan_id else None)

    def _done(self, sid, token, res, err):
        if sid != self._scan_id:
            return  # a newer scan replaced this one
        self.set_busy(False)
        self.cancel_btn.pack_forget()
        if err is not None:
            self.say(f"Couldn't read this folder: {err}", "err")
            return
        total = sum(e["size"] for e in res)
        for n, e in enumerate(res):
            iid = str(n)
            self.entries[iid] = e
            share = e["size"] / total if total else 0
            bar = "█" * int(round(share * 26))
            pct = "<1%" if 0 < share < 0.01 else f"{share * 100:.0f}%"
            self.tree.add(iid, (("▸ " if e["is_dir"] else "    ") + e["name"],
                                human_bytes(e["size"]), f"{bar} {pct}"), tags=("odd",) if n % 2 else ())
        note = "Cancelled - partial. " if token.cancelled else ""
        self.say(f"{note}{human_bytes(total)} in {len(res)} items.", "warn" if token.cancelled else "ok")

    def _dive(self, iid):
        e = self.entries.get(iid)
        if not e:
            return
        if e["is_dir"]:
            self.open(e["path"])
        else:
            reveal_in_finder(e["path"])

    def _reveal(self):
        e = self.entries.get(self.tree.selected())
        if e:
            reveal_in_finder(e["path"])
        else:
            messagebox.showinfo("Reveal", "Select an item first.")


class UninstallerPage(Page):
    title = "Uninstaller"
    subtitle = ("Remove an app together with the leftovers it scatters across your Library. "
                "Everything goes to the Trash.")

    def build(self):
        self.apps = []
        self._sizes = {}
        self._cur = None
        self._left = {}
        self._first = True
        self.make_footer(self.body).pack(side="bottom", fill="x", pady=(8, 0))

        main = tk.Frame(self.body, bg=BG)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=2, uniform="u")
        main.columnconfigure(1, weight=3, uniform="u")
        main.rowconfigure(0, weight=1)

        left = Card(main, padx=12, pady=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.filter_var = tk.StringVar()
        entry = tk.Entry(left.inner, textvariable=self.filter_var, font=F["base"], bg=CARD, fg=TEXT,
                         relief="flat", highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT, insertbackground=TEXT)
        entry.pack(fill="x", pady=(0, 8), ipady=5)
        self.filter_var.trace_add("write", lambda *a: self._render_list())
        self.app_tree = CheckTree(
            left.inner,
            [("name", "Application", 190, "w", True), ("size", "Size", 80, "e", False),
             ("ver", "Version", 70, "w", False)],
            checkable=False, on_select=self._on_select, height=14)
        self.app_tree.pack(fill="both", expand=True)

        right = Card(main, padx=14, pady=12)
        right.grid(row=0, column=1, sticky="nsew")
        self.app_title = tk.Label(right.inner, text="Select an application", font=F["h2"],
                                  bg=CARD, fg=TEXT, anchor="w")
        self.app_title.pack(fill="x")
        self.app_meta = tk.Label(right.inner, text="", font=F["small"], bg=CARD, fg=MUTED,
                                 anchor="w", justify="left", wraplength=400)
        self.app_meta.pack(fill="x", pady=(2, 10))
        tk.Label(right.inner, text="Leftover files (ticked ones are removed too)", font=F["small_bold"],
                 bg=CARD, fg=MUTED).pack(anchor="w")
        self.left_tree = CheckTree(
            right.inner,
            [("name", "Item", 210, "w", True), ("size", "Size", 76, "e", False),
             ("where", "Location", 150, "w", False)],
            on_change=self._update_total, height=8)
        self.left_tree.pack(fill="both", expand=True, pady=(4, 8))
        bottom = tk.Frame(right.inner, bg=CARD)
        bottom.pack(fill="x")
        self.total_lbl = tk.Label(bottom, text="", font=F["bold"], bg=CARD, fg=TEXT)
        self.total_lbl.pack(side="left")
        self.uninstall_btn = FlatButton(bottom, "Uninstall", self._uninstall, style="danger")
        self.uninstall_btn.pack(side="right")
        self.uninstall_btn.set_enabled(False)

    def on_show(self):
        if self._first:
            self._first = False
            self.load()

    def load(self):
        self._cur = None
        self.app_title.config(text="Select an application")
        self.app_meta.config(text="")
        self.total_lbl.config(text="")
        self.uninstall_btn.set_enabled(False)
        self._left = {}
        self.left_tree.clear()
        self.apps = list_apps()
        self._render_list()
        self.say("Measuring applications…")
        self.set_busy(True)
        apps = list(self.apps)
        self.app.run_bg(lambda p: compute_app_sizes(apps, p), self._sizes_done, lambda m: self.say(m))

    def _sizes_done(self, res, err):
        self.set_busy(False)
        if err is not None:
            self.say(f"Couldn't measure apps: {err}", "err")
            return
        self._sizes = res
        self.apps.sort(key=lambda a: self._sizes.get(a["path"], 0), reverse=True)
        self._cur = None
        self._render_list()
        self.say(f"{len(self.apps)} applications. Select one to see what it leaves behind.", "ok")

    def _render_list(self):
        q = self.filter_var.get().strip().lower()
        self.app_tree.clear()
        shown = 0
        for idx, a in enumerate(self.apps):
            if q and q not in a["name"].lower():
                continue
            size = self._sizes.get(a["path"])
            self.app_tree.add(str(idx), (a["name"], human_bytes(size) if size is not None else "…",
                                         a["version"]), tags=("odd",) if shown % 2 else ())
            shown += 1

    def _on_select(self, iid):
        app = self.apps[int(iid)]
        self._cur = app
        self._left = {}
        self.left_tree.clear()
        self.app_title.config(text=app["name"])
        meta = f"Version {app['version'] or '?'} · {app['bundle_id'] or 'no bundle id'}\n{short_path(app['path'])}"
        if app["protected"]:
            meta += "\nThis is a built-in macOS app and can't be uninstalled here."
        self.app_meta.config(text=meta)
        self.uninstall_btn.set_enabled(not app["protected"])
        self.total_lbl.config(text="Looking for leftovers…")
        path = app["path"]
        self.app.run_bg(lambda p: leftovers_with_sizes(app),
                        lambda res, err: self._left_done(path, res, err))

    def _left_done(self, path, res, err):
        if self._cur is None or self._cur["path"] != path:
            return  # selection changed while we were searching
        if err is not None:
            self.total_lbl.config(text="Couldn't search for leftovers")
            return
        self._left = {}
        self.left_tree.clear()
        for n, (p, size) in enumerate(res):
            iid = str(n)
            self._left[iid] = (p, size)
            self.left_tree.add(iid, (os.path.basename(p), human_bytes(size), short_path(os.path.dirname(p))),
                               checked=True, tags=("odd",) if n % 2 else ())
        self._update_total()

    def _update_total(self):
        if self._cur is None:
            return
        base = self._sizes.get(self._cur["path"], 0)
        extra = sum(self._left[i][1] for i in self.left_tree.checked if i in self._left)
        n = len([i for i in self.left_tree.checked if i in self._left])
        self.total_lbl.config(text=f"Removes {human_bytes(base + extra)} (app + {n} leftover item"
                                   f"{'s' if n != 1 else ''})")

    def _uninstall(self):
        app = self._cur
        if app is None:
            return
        if app["protected"]:
            messagebox.showinfo("Uninstall", "Built-in macOS apps can't be removed.")
            return
        if is_app_running(app["path"]):
            messagebox.showinfo("App is running", f"Quit {app['name']} first, then try again.")
            return
        ids = [i for i in self.left_tree.get_checked() if i in self._left]
        paths = [app["path"]] + [self._left[i][0] for i in ids]
        total = self._sizes.get(app["path"], 0) + sum(self._left[i][1] for i in ids)
        summary = f"{app['name']} and {len(ids)} leftover item{'s' if len(ids) != 1 else ''}"
        if self.app.confirm_and_trash(paths, total, summary, self._uninstall_done):
            self.set_busy(True)
            self.say("Moving to Trash…")

    def _uninstall_done(self, res, err):
        self.set_busy(False)
        if err is not None:
            self.say(f"Uninstall failed: {err}", "err")
            return
        moved, errors = res
        self.load()
        msg = f"Moved {moved} item{'s' if moved != 1 else ''} to the Trash."
        self.say(msg + (trash_hint(errors) if errors else ""), "warn" if errors else "ok")


# ==========================================================================
# Pages: Maintenance, Benchmark
# ==========================================================================
class MaintenancePage(Page):
    title = "Maintenance"
    subtitle = ("Housekeeping tasks that fix real problems. Tasks marked 'password' show macOS's own "
                "admin dialog - this app never sees your password.")

    TASKS = [
        dict(id="dns", title="Flush DNS cache", admin=True,
             desc="Clears cached website lookups. Helps when a site won't load or points to an old server."),
        dict(id="spotlight", title="Rebuild Spotlight index", admin=True,
             desc="Fixes missing or wrong search results. Search is slow for a while as macOS reindexes."),
        dict(id="snapshots", title="Thin Time Machine local snapshots", admin=True,
             desc="Local snapshots can quietly hold many GB. This frees that space; your backup disk is untouched."),
        dict(id="verify", title="Verify startup disk", admin=False,
             desc="Checks the disk's file system for errors. Can take a few minutes."),
    ]

    def build(self):
        self._busy = False
        self.buttons = {}
        self.make_footer(self.body).pack(side="bottom", fill="x", pady=(8, 0))
        for task in self.TASKS:
            card = Card(self.body, padx=14, pady=10)
            card.pack(fill="x", pady=4)
            row = card.inner
            row.columnconfigure(0, weight=1)
            tk.Label(row, text=task["title"] + ("   · password" if task["admin"] else ""),
                     font=F["bold"], bg=CARD, fg=TEXT, anchor="w").grid(row=0, column=0, sticky="w")
            tk.Label(row, text=task["desc"], font=F["small"], bg=CARD, fg=MUTED, anchor="w",
                     justify="left", wraplength=620).grid(row=1, column=0, sticky="w")
            btn = FlatButton(row, "Run", lambda t=task: self._run(t), padx=18, pady=6)
            btn.grid(row=0, column=1, rowspan=2, padx=(10, 0))
            self.buttons[task["id"]] = btn

        log_card = Card(self.body, padx=10, pady=10)
        log_card.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(log_card.inner, height=6, wrap="word", state="disabled", font=F["mono"],
                           bg=CARD, fg=TEXT, relief="flat", highlightthickness=0)
        self.log.pack(fill="both", expand=True)
        self._log("Task output appears here.")

    def _log(self, text):
        self.log.config(state="normal")
        self.log.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {text}\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _lock(self, busy):
        self._busy = busy
        self.set_busy(busy)
        for b in self.buttons.values():
            b.set_enabled(not busy)

    def _run(self, task):
        if self._busy:
            return
        tid = task["id"]
        if tid == "dns":
            job = lambda p: run_admin_shell("dscacheutil -flushcache; killall -HUP mDNSResponder")  # noqa: E731
        elif tid == "spotlight":
            if not messagebox.askyesno("Rebuild Spotlight",
                                       "Spotlight will reindex your disk. Search results may be "
                                       "incomplete or slow for a while.\n\nContinue?"):
                return
            job = lambda p: run_admin_shell("mdutil -E /")  # noqa: E731
        elif tid == "snapshots":
            count = self._snapshot_count()
            if count == 0:
                self._log("No local Time Machine snapshots found - nothing to thin.")
                self.say("No local snapshots found.", "ok")
                return
            if not messagebox.askyesno("Thin snapshots",
                                       f"Found {count} local snapshot(s). Removing them frees space "
                                       "on this Mac only; backups on your Time Machine disk are not "
                                       "affected.\n\nContinue?"):
                return
            job = lambda p: run_admin_shell("tmutil thinlocalsnapshots / 999999999999 4")  # noqa: E731
        else:
            job = self._verify_job
        self._lock(True)
        self._log(f"Running: {task['title']}…")
        self.say(f"Running {task['title']}…")
        self.app.run_bg(job, lambda res, err: self._done(task, res, err))

    @staticmethod
    def _snapshot_count():
        try:
            r = subprocess.run(["tmutil", "listlocalsnapshots", "/"], capture_output=True,
                               text=True, timeout=20)
            return len([ln for ln in r.stdout.splitlines() if ln.strip().startswith("com.apple")])
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _verify_job(progress):
        try:
            r = subprocess.run(["diskutil", "verifyVolume", "/"], capture_output=True, text=True, timeout=1800)
            out = (r.stdout + r.stderr).strip()
            tail = "\n".join(out.splitlines()[-6:])
            return r.returncode == 0, tail or "No output."
        except FileNotFoundError:
            return False, "diskutil not found (are you on macOS?)."
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def _done(self, task, res, err):
        self._lock(False)
        if err is not None:
            self._log(f"{task['title']} failed: {err}")
            self.say(f"{task['title']} failed.", "err")
            return
        ok, msg = res
        self._log(f"{task['title']}: {'done' if ok else 'failed'}\n    {msg}")
        self.say(f"{task['title']}: {'done.' if ok else 'did not complete.'}", "ok" if ok else "err")


class BenchmarkPage(Page):
    title = "Benchmark"
    subtitle = "Measures CPU and disk speed and compares them with an approximate 2015 MacBook Air."

    def build(self):
        self._busy = False
        top = tk.Frame(self.body, bg=BG)
        top.pack(fill="x")
        self.run_btn = FlatButton(top, "Run benchmark", self.run, padx=22, pady=10)
        self.run_btn.pack(side="left")
        self.make_footer(top).pack(side="left", padx=14)

        tiles = tk.Frame(self.body, bg=BG)
        tiles.pack(fill="x", pady=(12, 0))
        self.tiles = {}
        for i, (key, title) in enumerate((("cpu", "CPU speed (1 core)"), ("read", "Disk read"),
                                          ("write", "Disk write"))):
            tiles.columnconfigure(i, weight=1, uniform="b")
            t = StatTile(tiles, title, bar=False)
            t.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            self.tiles[key] = t

        card = Card(self.body, padx=12, pady=12)
        card.pack(fill="both", expand=True, pady=(12, 0))
        self.notes = tk.Text(card.inner, wrap="word", state="disabled", font=F["mono"], bg=CARD, fg=TEXT,
                             relief="flat", highlightthickness=0)
        self.notes.pack(fill="both", expand=True)
        self._set_notes(
            "2015 MacBook Air reference (approximate):\n"
            f"  CPU:  {BASELINE['cpu_model']}\n"
            f"  RAM:  {BASELINE['ram_gb']} GB\n"
            f"  SSD:  ~{BASELINE['ssd_read_mbs']:.0f} MB/s read, ~{BASELINE['ssd_write_mbs']:.0f} MB/s write\n\n"
            "Click \"Run benchmark\" to test this Mac. Close heavy apps first for a cleaner result.")

    def _set_notes(self, text):
        self.notes.config(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", text)
        self.notes.config(state="disabled")

    def run(self):
        if self._busy:
            return
        self._busy = True
        self.run_btn.set_enabled(False)
        self.set_busy(True)
        self.say("Starting…")
        self.app.run_bg(self._job, self._done, lambda m: self.say(m))

    @staticmethod
    def _job(progress):
        progress("Testing CPU…")
        cpu = cpu_benchmark()
        progress("Testing disk…")
        write_mbs, read_mbs = disk_benchmark()
        return {"cpu": cpu, "write": write_mbs, "read": read_mbs, "therm": get_thermal_state()}

    def _done(self, r, err):
        self._busy = False
        self.run_btn.set_enabled(True)
        self.set_busy(False)
        if err is not None:
            self.say(f"Benchmark failed: {err}", "err")
            return
        cpu_x = r["cpu"] / BASELINE["hash_ops_per_sec"]
        rd_x = r["read"] / BASELINE["ssd_read_mbs"]
        wr_x = r["write"] / BASELINE["ssd_write_mbs"]
        self.tiles["cpu"].set(f"{cpu_x:.1f}×", f"{r['cpu']:,.0f} hashes/s")
        self.tiles["read"].set(f"{rd_x:.1f}×", f"{r['read']:.0f} MB/s")
        self.tiles["write"].set(f"{wr_x:.1f}×", f"{r['write']:.0f} MB/s")
        therm = r["therm"]
        therm_line = ("unavailable" if not therm["available"]
                      else f"THROTTLED to {therm['speed_limit']}%" if therm["throttled"] else "normal")
        ram = f"{bytes_to_gb(psutil.virtual_memory().total):.0f} GB" if psutil is not None else "unknown"
        self._set_notes(
            "Multipliers are relative to a 2015 MacBook Air (1.0× = same speed).\n\n"
            f"CPU   {r['cpu']:,.0f} hashes/s vs ~{BASELINE['hash_ops_per_sec']:,} reference\n"
            f"Read  {r['read']:.0f} MB/s vs ~{BASELINE['ssd_read_mbs']:.0f} MB/s reference\n"
            f"Write {r['write']:.0f} MB/s vs ~{BASELINE['ssd_write_mbs']:.0f} MB/s reference\n"
            f"RAM   {ram} installed (reference: {BASELINE['ram_gb']} GB)\n"
            f"Thermal state: {therm_line}\n\n"
            "Notes:\n"
            " - The CPU test uses one core, so multi-core chips don't show their full advantage.\n"
            " - Reference numbers are ballpark figures, not an official spec sheet.\n"
            " - Disk speed varies with free space; a nearly full drive benchmarks slower.\n"
            " - Frequent thermal throttling matters more for real-world speed than RAM 'cleaning'.")
        self.say("Benchmark complete.", "ok")


# ==========================================================================
# App shell
# ==========================================================================
PAGES = [
    ("smart", "Smart Scan", "✦", SmartScanPage),
    ("perf", "Performance", "⚡", PerformancePage),
    ("junk", "System Junk", "♻", JunkPage),
    ("large", "Large & Old Files", "▤", LargeFilesPage),
    ("dupes", "Duplicates", "❐", DuplicatesPage),
    ("lens", "Space Lens", "◎", SpaceLensPage),
    ("uninstall", "Uninstaller", "⌫", UninstallerPage),
    ("maint", "Maintenance", "⚙", MaintenancePage),
    ("bench", "Benchmark", "⏱", BenchmarkPage),
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1120x760")
        self.minsize(1000, 680)
        self.configure(bg=BG)
        init_fonts()
        self._init_style()
        self._set_icon()
        self._ui_queue = queue.Queue()
        self.pages = {}
        self.nav = {}
        self.current = None
        self._build_shell()
        self.after(60, self._drain_ui_queue)
        self.after(TICK_MS, self._tick)
        self.show_page("smart")

    def _set_icon(self):
        """Dock/window icon when run from source (a built .app uses its bundle icon)."""
        try:
            self._icon = tk.PhotoImage(file=resource_path(os.path.join("assets", "broom.png")))
            self.iconphoto(True, self._icon)
        except Exception:  # noqa: BLE001 - a missing icon must never stop the app
            pass

    # ---- styling
    def _init_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")  # the only built-in theme that honours custom colours everywhere
        s.configure(".", background=BG, foreground=TEXT, font=F["base"])
        s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                    rowheight=28, borderwidth=0, font=F["base"])
        s.configure("Treeview.Heading", background="#eceef9", foreground=MUTED, relief="flat",
                    borderwidth=0, font=F["small_bold"], padding=(8, 6))
        s.map("Treeview", background=[("selected", ACCENT_SOFT)], foreground=[("selected", TEXT)])
        s.map("Treeview.Heading", background=[("active", "#e2e5f5")])
        s.configure("Vertical.TScrollbar", background="#cfd3e8", troughcolor=CARD, bordercolor=CARD,
                    lightcolor="#cfd3e8", darkcolor="#cfd3e8", arrowcolor=MUTED, relief="flat")
        s.configure("Accent.Horizontal.TProgressbar", troughcolor="#e4e6f4", background=ACCENT,
                    bordercolor="#e4e6f4", lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)
        s.configure("TCombobox", fieldbackground=CARD, background="#e8eaf6", foreground=TEXT,
                    arrowcolor=MUTED, bordercolor=BORDER, padding=4)
        s.map("TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", CARD)], selectforeground=[("readonly", TEXT)])
        self.option_add("*TCombobox*Listbox.font", F["base"])
        self.option_add("*TCombobox*Listbox.background", CARD)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT_SOFT)
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)

    def _build_shell(self):
        sidebar = tk.Frame(self, bg=SIDEBAR, width=232)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=18, pady=(26, 20))
        tk.Label(brand, text="✦", font=F["h2"], bg=SIDEBAR, fg="#a894ff").pack(side="left")
        tk.Label(brand, text="  MacBook Optimizer", font=F["bold"], bg=SIDEBAR, fg="#ffffff").pack(side="left")

        for key, label, icon, _cls in PAGES:
            item = NavItem(sidebar, icon, label, lambda k=key: self.show_page(k))
            item.pack(fill="x", padx=10, pady=1)
            self.nav[key] = item

        foot = f"v{__version__} · Python {platform.python_version()} · Tk {self.tk.call('info', 'patchlevel')}"
        if psutil is None:
            tk.Label(sidebar, text="psutil missing:\npip install psutil", font=F["small"], bg=SIDEBAR,
                     fg="#ff9a9e", justify="left").pack(side="bottom", anchor="w", padx=18, pady=(0, 6))
        tk.Label(sidebar, text=foot, font=F["small"], bg=SIDEBAR, fg="#7f7cab").pack(
            side="bottom", anchor="w", padx=18, pady=10)

        self.content = tk.Frame(self, bg=BG)
        self.content.pack(side="right", fill="both", expand=True)
        for key, _label, _icon, cls in PAGES:
            self.pages[key] = cls(self)

    # ---- navigation
    def show_page(self, key):
        if self.current is not None:
            self.pages[self.current].pack_forget()
            self.nav[self.current].set_active(False)
        self.current = key
        self.pages[key].pack(fill="both", expand=True)
        self.nav[key].set_active(True)
        try:
            self.pages[key].on_show()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _tick(self):
        page = self.pages.get(self.current)
        if page is not None:
            try:
                page.refresh()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        self.after(TICK_MS, self._tick)

    # ---- background work: workers never touch Tk; they post to this queue
    def run_bg(self, fn, on_done, on_progress=None):
        q = self._ui_queue

        def progress(msg):
            if on_progress is not None:
                q.put((on_progress, (msg,)))

        def target():
            try:
                result = fn(progress)
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                q.put((on_done, (None, exc)))
            else:
                q.put((on_done, (result, None)))

        threading.Thread(target=target, daemon=True).start()

    def _drain_ui_queue(self):
        try:
            while True:
                cb, args = self._ui_queue.get_nowait()
                try:
                    cb(*args)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.after(80, self._drain_ui_queue)

    # ---- shared: confirm, then move to Trash on a worker thread
    def confirm_and_trash(self, paths, total_bytes, summary, done_cb):
        if not paths:
            return False
        if not messagebox.askyesno(
                "Move to Trash",
                f"Move {summary} ({human_bytes(total_bytes)}) to the Trash?\n\n"
                "You can restore them from the Trash if you change your mind."):
            return False
        self.run_bg(lambda p: trash_paths(paths), done_cb)
        return True


def main():
    if not IS_MAC:
        print("Note: designed for macOS - some features (Trash, purge, login items, maintenance) "
              "won't work on this platform.")
    app = App()
    print(f"Python {platform.python_version()} · Tk {app.tk.call('info', 'patchlevel')}")
    app.mainloop()


if __name__ == "__main__":
    main()
