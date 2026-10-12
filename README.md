<p align="center">
  <img src="assets/broom.png" width="128" alt="MacBook Optimizer broom icon">
</p>

<h1 align="center">MacBook Optimizer</h1>

<p align="center">
A free, open-source Mac cleaner and health checker written in Python (Tkinter).<br>
Smart Scan, junk cleanup, large &amp; duplicate file finders, an app uninstaller, and more - in the spirit of CleanMyMac.
</p>

---

## What it does

| Page | What it's for |
|---|---|
| **Smart Scan** | One-click health check with a 0-100 score: junk, disk space, memory, startup load, battery health, throttling, uptime. |
| **Performance** | Live CPU / memory / disk / battery, heaviest processes (sort by CPU or memory), login items, launch agents, RAM purge. |
| **System Junk** | Caches, logs, Mail downloads, saved window state, Xcode caches, Trash. You choose what to clear. |
| **Large & Old Files** | Find big or stale files by folder, size and age. Removal goes to the **Trash**. |
| **Duplicates** | Finds identical files (size, then fast fingerprint, then full SHA-256). Always keeps at least one copy. |
| **Space Lens** | Folder-by-folder view of what's using your storage. |
| **Uninstaller** | Removes an app plus the leftovers it scatters around `~/Library`. Everything goes to the **Trash**. |
| **Maintenance** | Flush DNS, rebuild Spotlight, thin Time Machine local snapshots, verify the startup disk. |
| **Benchmark** | CPU and disk speed compared with an approximate 2015 MacBook Air. |

## Install and run from source

You need a Mac with **Python 3.9+ that includes Tk 8.6**. The Python that ships with macOS (and the
Xcode command line tools) uses the old Tk 8.5, which draws **blank windows** on modern macOS - install
Python from [python.org](https://www.python.org/downloads/macos/) instead.

```bash
git clone https://github.com/<your-username>/macbook-optimizer.git
cd macbook-optimizer

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python3 macbook_optimizer.py
```

Check your Tk version any time with `python3 -c "import tkinter; print(tkinter.TkVersion)"` - it should print `8.6`.

## Make it a real Mac app (with the broom icon)

**Option A - standalone app (recommended).** Builds `MacBook Optimizer.app` with PyInstaller:

```bash
source .venv/bin/activate
./build_app.sh
cp -R "dist/MacBook Optimizer.app" ~/Desktop/      # or drag it into Applications
```

The app is built for the type of Mac you build it on (Apple Silicon or Intel).

**Option B - quick launcher.** Creates a tiny app on your Desktop that runs this folder's code with this
folder's `.venv`. Fast, but it stops working if you move or delete the project folder:

```bash
./make_launcher.sh
```

If macOS says an app you *downloaded* "can't be opened because it is from an unidentified developer",
right-click it, choose **Open**, then confirm. Apps you build yourself don't trigger this.

## Permissions you may see

All of these are normal and are only used for the features described:

- **Control Finder / System Events** - moves files to the Trash and reads your Login Items. Click OK.
- **Desktop / Documents / Downloads access** - used by the file scanners when you scan those folders.
- **Administrator password** - for Maintenance tasks (and RAM purge on newer macOS). The dialog is
  macOS's own; this app never sees or stores your password.
- **Full Disk Access** *(optional)* - System Settings → Privacy & Security → Full Disk Access. Lets the
  cleaner reach protected folders such as Mail Downloads and the Trash. Add the app (or Terminal, if you
  run from source).

## Safety model

- Nothing is deleted without a confirmation dialog showing what and how much.
- The file finders and the uninstaller move items to the **Trash** (restorable) instead of deleting them.
- **System Junk** is the only permanent delete, limited to specific folders in `~/Library` and the Trash.
  The Trash is never pre-selected.
- It never touches your Documents, Photos library, Mail messages, or system files. App packages
  (`.photoslibrary`, `.app`, ...), hidden folders and `node_modules` are skipped by the scanners.
- Leftover matching in the Uninstaller is deliberately strict (exact bundle IDs and app-name folders), so
  removing one app can't sweep up a different app's data.
- Built-in macOS apps (e.g. Safari) can't be uninstalled from the app.

**Use at your own risk and keep backups** (Time Machine is great). This is a hobby project provided as-is
with no warranty - see [LICENSE](LICENSE).

## Honest limitations

- **"Cleaning RAM" mostly isn't a thing on macOS.** The OS already uses spare memory as cache and frees it
  when needed. *Free up RAM* runs the real `purge` command and shows before/after numbers, but any gain is
  temporary. The things that genuinely speed up a Mac are free disk space, fewer startup apps, avoiding
  thermal throttling, and the occasional restart - which is what Smart Scan looks at.
- **The health score is a simple formula, not a measurement.** It subtracts points for low free disk
  space, heavy swap, throttling, long uptime, lots of junk, and many login items.
- **Benchmark baselines are ballpark figures** for a 2015 MacBook Air (Core i5-5250U), not an official spec
  sheet, and the CPU test is single-core.
- No malware scanner, no browser-history cleaner, and no stripping of language files from apps (that
  breaks code signing). Those are intentionally out of scope.

## How the health score works

Starts at 100, then subtracts: low free disk space (up to −30), swap use (up to −10), high memory use
(up to −8), CPU throttling (−20), uptime over 14/30 days (−5/−10), junk over 1/5 GB (−4/−8), more than
5/10 login items (−4/−8), and a battery that isn't in "Normal" condition (−10).

## Project layout

```
macbook_optimizer.py     the whole app (backend logic + Tkinter UI)
make_icon.py             draws the broom icon -> assets/
build_app.sh             builds the standalone .app (PyInstaller)
MacBookOptimizer.spec    PyInstaller recipe + macOS permission descriptions
make_launcher.sh         quick Desktop launcher alternative
assets/                  broom.png, broom_1024.png, AppIcon.icns
tests/test_backend.py    tests for the scanning / cleanup logic (no display needed)
requirements*.txt        runtime and build dependencies
```

## Development

```bash
python3 tests/test_backend.py      # Tk is stubbed out, so this runs anywhere (e.g. CI)
python3 make_icon.py               # regenerate the icon after tweaking it
```

The UI itself needs a Mac to run. Errors raised inside button handlers are printed to the terminal.

## Contributing

Issues and pull requests are welcome. If you add something that deletes or moves files, please follow the
existing pattern: confirm first, prefer the Trash, and add a test for the matching logic.

## License

[MIT](LICENSE)
