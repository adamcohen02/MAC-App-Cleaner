import importlib.util, os, plistlib, sys, tempfile, time, types

# ---- stub tkinter so the module imports without Tk/display ----
def make_cls(name):
    return type(name, (), {"__init__": lambda self, *a, **k: None})
tk = types.ModuleType("tkinter")
for n in ("Tk", "Frame", "Label", "Canvas", "Entry", "Listbox", "Text", "StringVar", "BooleanVar"):
    setattr(tk, n, make_cls(n))
ttk = types.ModuleType("tkinter.ttk")
for n in ("Treeview", "Style", "Combobox", "Scrollbar", "Progressbar"):
    setattr(ttk, n, make_cls(n))
font = types.ModuleType("tkinter.font")
mb = types.ModuleType("tkinter.messagebox")
fd = types.ModuleType("tkinter.filedialog")
tk.ttk, tk.font, tk.messagebox, tk.filedialog = ttk, font, mb, fd
sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.font": font,
                    "tkinter.messagebox": mb, "tkinter.filedialog": fd})

spec = importlib.util.spec_from_file_location("app", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "macbook_optimizer.py"))
app = importlib.util.module_from_spec(spec); spec.loader.exec_module(app)

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("PASS", name)
    else: fail += 1; print("FAIL", name, extra)

tmp = tempfile.mkdtemp()
noop = lambda *_: None
tok = app.CancelToken()

# --- formatting
check("human_bytes decimal", app.human_bytes(1_500_000) == "1.5 MB", app.human_bytes(1_500_000))
check("human_bytes bytes", app.human_bytes(12) == "12 B")
check("human_bytes binary", app.human_bytes(1024**3, True) == "1.0 GB")

# --- file fixtures
docs = os.path.join(tmp, "docs"); os.makedirs(docs)
def mk(path, size, byte=b"a", age_days=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f: f.write(byte * size)
    if age_days:
        t = time.time() - age_days * 86400; os.utime(path, (t, t))
mk(f"{docs}/big_new.bin", 3_000_000, b"x")
mk(f"{docs}/big_old.bin", 2_000_000, b"y", age_days=400)
mk(f"{docs}/small.txt", 1_000, b"z")
mk(f"{docs}/.hidden_big", 5_000_000, b"h")
mk(f"{docs}/Pic.photoslibrary/inner.bin", 9_000_000, b"p")      # package: must be skipped
mk(f"{docs}/node_modules/x/huge.bin", 9_000_000, b"n")           # skipped dir
mk(f"{docs}/sub/dupA.dat", 1_500_000, b"d", age_days=30)         # duplicate set (3 copies)
mk(f"{docs}/sub2/dupB.dat", 1_500_000, b"d", age_days=10)
mk(f"{docs}/sub3/dupC.dat", 1_500_000, b"d", age_days=5)
mk(f"{docs}/sub/samesize_diff.dat", 1_500_000, b"e")             # same size, different content
os.link(f"{docs}/sub3/dupC.dat", f"{docs}/sub3/dupC_hardlink.dat")  # hard link: not a duplicate
os.symlink(f"{docs}/big_new.bin", f"{docs}/link_to_big")          # symlink: ignored

# --- large files
res = app.scan_large_files([docs], 1_000_000, None, tok, noop)
names = [os.path.basename(r["path"]) for r in res]
check("large: finds big files", "big_new.bin" in names and "big_old.bin" in names, names)
check("large: skips hidden/package/node_modules/symlink",
      not any(n in names for n in (".hidden_big", "inner.bin", "huge.bin", "link_to_big")), names)
check("large: sorted desc", [r["size"] for r in res] == sorted([r["size"] for r in res], reverse=True))
old = app.scan_large_files([docs], 1_000_000, 365, tok, noop)
check("large: age filter", [os.path.basename(r["path"]) for r in old] == ["big_old.bin"],
      [os.path.basename(r["path"]) for r in old])
c = app.CancelToken(); c.cancel()
check("large: cancel stops early", app.scan_large_files([docs], 1, None, c, noop) == [])

# --- duplicates
groups = app.scan_duplicates([docs], 1_000_000, tok, noop)
check("dupes: exactly one group", len(groups) == 1, [[os.path.basename(f["path"]) for f in g] for g in groups])
if groups:
    g = [os.path.basename(f["path"]) for f in groups[0]]
    check("dupes: 3 copies, oldest first", g == ["dupA.dat", "dupB.dat", "dupC.dat"], g)
    check("dupes: ignores same-size-different-content", "samesize_diff.dat" not in g)
    check("dupes: hard link not counted", "dupC_hardlink.dat" not in g)
check("dupes: cancel returns empty", app.scan_duplicates([docs], 1, c, noop) == [])

# --- sizes / children / clear
check("fast_size file", app.fast_size(f"{docs}/small.txt") > 0)
check("fast_size dir", app.fast_size(docs) >= 9_000_000)
kids = app.list_children_sizes(docs, tok, noop)
check("children sorted desc", [k["size"] for k in kids] == sorted([k["size"] for k in kids], reverse=True))
check("children is_dir flags", {k["name"]: k["is_dir"] for k in kids}["sub"] is True
      and {k["name"]: k["is_dir"] for k in kids}["small.txt"] is False)
cdir = os.path.join(tmp, "cachedir"); mk(f"{cdir}/a/b.bin", 5000); mk(f"{cdir}/c.bin", 3000)
freed, errs = app.clear_directory_contents(cdir)
check("clear: removes contents, keeps dir", os.path.isdir(cdir) and os.listdir(cdir) == [] and freed > 0 and not errs,
      (os.listdir(cdir), freed, errs))

# --- uninstaller matching (strict) using a fake HOME
fake_home = os.path.join(tmp, "home"); lib = f"{fake_home}/Library"
for p in ("Application Support/Slack", "Caches/com.tinyspeck.slackmacgap", "Preferences/com.tinyspeck.slackmacgap.plist",
          "Saved Application State/com.tinyspeck.slackmacgap.savedState", "Containers/com.tinyspeck.slackmacgap",
          "Group Containers/ABCDE12345.com.tinyspeck.slackmacgap",
          # must NOT match:
          "Caches/com.tinyspeck.slackmacgap.canary", "Application Support/Slack Helper Tools",
          "Preferences/com.tinyspeck.slackmacgap.canary.plist", "Application Support/Other"):
    full = os.path.join(lib, p)
    if p.endswith(".plist"): os.makedirs(os.path.dirname(full), exist_ok=True); open(full, "w").write("x")
    else: os.makedirs(full, exist_ok=True)
app.HOME = fake_home
found = app.find_app_leftovers({"name": "Slack", "bundle_id": "com.tinyspeck.slackmacgap"})
rel = sorted(os.path.relpath(p, lib) for p in found)
expected = sorted(["Application Support/Slack", "Caches/com.tinyspeck.slackmacgap",
                   "Preferences/com.tinyspeck.slackmacgap.plist",
                   "Saved Application State/com.tinyspeck.slackmacgap.savedState",
                   "Containers/com.tinyspeck.slackmacgap",
                   "Group Containers/ABCDE12345.com.tinyspeck.slackmacgap"])
check("leftovers: exact matches only", rel == expected, f"got {rel}")
check("leftovers: never matches sibling app (canary)", not any("canary" in r for r in rel))
check("leftovers: no match when name too short & no bundle id",
      app.find_app_leftovers({"name": "ab", "bundle_id": ""}) == [])

# --- list_apps with fake bundle
apps_dir = os.path.join(tmp, "Applications"); bundle = f"{apps_dir}/Foo.app/Contents"; os.makedirs(bundle)
plistlib.dump({"CFBundleIdentifier": "com.foo.app", "CFBundleShortVersionString": "2.1"}, open(f"{bundle}/Info.plist", "wb"))
os.makedirs(f"{apps_dir}/Safari.app/Contents")
plistlib.dump({"CFBundleIdentifier": "com.apple.Safari"}, open(f"{apps_dir}/Safari.app/Contents/Info.plist", "wb"))
app.APP_DIRS = [apps_dir]
la = {a["name"]: a for a in app.list_apps()}
check("apps: reads bundle id & version", la["Foo"]["bundle_id"] == "com.foo.app" and la["Foo"]["version"] == "2.1")
check("apps: Safari protected, Foo not", la["Safari"]["protected"] and not la["Foo"]["protected"])

# --- categories & junk scan
cats = app.build_cleanup_categories()
check("categories: trash never default", all(not c["default"] for c in cats if c["id"] == "trash"))
check("categories: only existing paths", all(any(os.path.isdir(p) for p in c["paths"]) for c in cats))

# --- health score
check("score: perfect", app.compute_health_score({"disk_free_pct": 60, "swap_used": 0, "ram_pct": 50,
      "uptime_days": 2, "junk": 0, "login_count": 2}) == 100)
bad = app.compute_health_score({"disk_free_pct": 5, "swap_used": 3e9, "ram_pct": 95, "throttled": True,
      "uptime_days": 40, "junk": 9e9, "login_count": 15, "battery_health": {"condition": "Service Recommended"}})
check("score: bad is low & clamped", 0 <= bad <= 30, bad)
check("score: empty metrics ok", app.compute_health_score({}) == 100)

# --- smart scan end-to-end (Linux: macOS-only bits degrade gracefully)
msgs = []
m = app.gather_smart_scan(msgs.append)
check("smart scan: returns metrics", "junk" in m and "uptime_days" in m and msgs, list(m))
check("smart scan: score computes", 0 <= app.compute_health_score(m) <= 100)

# --- non-mac guards
if not app.IS_MAC:
    moved, errs = app.trash_paths([f"{docs}/small.txt"])
    check("trash: refuses off-macOS, file untouched", moved == 0 and errs and os.path.exists(f"{docs}/small.txt"))
    check("admin: refuses off-macOS", app.run_admin_shell("echo hi")[0] is False)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
