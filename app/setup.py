"""Offline Kit setup: choose a size, download the library and AI model, make the Desktop shortcut.

Run by install.sh. Safe to re-run at any time: finished files are kept, partial
downloads resume, and newer versions of collections replace older ones (so
re-running is also how you update).

Options:
  --tier essentials|standard|complete   skip the size question
  --model NAME                          use a specific Ollama model
  --yes                                 don't ask for confirmation
  --app-only                            update the program only; leave the library as it is
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
CODE = KIT / "kit"
LIBRARY = KIT / "library"
CONFIG = KIT / "config.json"
MIRROR = "https://download.kiwix.org/zim"
OLLAMA_HOST = "127.0.0.1:11435"
GB = 1000**3

TIERS = {
    "essentials": ("E", "Essentials", "Wikipedia summaries, medicine, repair, survival guides"),
    "standard": ("S", "Standard", "Full Wikipedia text plus 30+ practical collections"),
    "complete": ("C", "Complete", "Everything, including Wikipedia with all images"),
}
# Space kept free so the Mac keeps working normally.
RESERVE = 15 * GB
# Never start a download that would leave less than this free.
FLOOR = 3 * GB


class Stop(Exception):
    """A problem to show the user as a plain message (no traceback)."""


def say(msg=""):
    print(msg, flush=True)


def bold(msg):
    say(f"\n\033[1m{msg}\033[0m")


def ask(prompt):
    """Read an answer from the keyboard, even when the installer was piped from curl."""
    try:
        with open("/dev/tty") as tty:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            return tty.readline().strip()
    except OSError:
        return ""


def human(n):
    return f"{n / GB:.1f} GB" if n >= GB else f"{n / 1000**2:.0f} MB"


def fetch(url, timeout=60, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "offline-kit"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == tries - 1:
                raise
        except OSError:
            if attempt == tries - 1:
                raise
        time.sleep(3 * (attempt + 1))


# ---------------------------------------------------------------- catalog


def read_catalog():
    rows = []
    for line in (CODE / "catalog.tsv").read_text().splitlines():
        if not line.strip() or line.startswith("#") or line.startswith("tiers\t"):
            continue
        parts = [x.strip() for x in line.split("\t")]
        if len(parts) != 5:
            say(f"  (ignoring a line in catalog.tsv that doesn't have 5 tab-separated columns: {line[:60]})")
            continue
        tiers, d, prefix, title, about = parts
        rows.append(dict(tiers=tiers, dir=d, prefix=prefix, title=title, about=about))
    return rows


SIZE_UNITS = {"": 1, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
# The server's folder listing is a web page with one link per file, followed by
# its date and size ("27M", "1.2G", or a plain number of bytes). The layout has
# varied between servers, so this reads it loosely: find each .zim link, then
# take the last size-looking word in the text after it.
LINK_RE = re.compile(r'<a\b[^>]*\bhref="([^"/?#]+\.zim)"[^>]*>', re.I)
SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([KMGT]?)i?B?$", re.I)


def parse_listing(page):
    files = {}
    links = list(LINK_RE.finditer(page))
    for n, m in enumerate(links):
        end = links[n + 1].start() if n + 1 < len(links) else len(page)
        tail = page[m.end():end]
        tail = tail.split("</a>", 1)[1] if "</a>" in tail else ""
        tail = re.split(r"\n|</tr>|</li>|<a\b", tail, maxsplit=1, flags=re.I)[0]
        size = 0
        for word in reversed(re.sub(r"<[^>]+>", " ", tail).split()):
            sm = SIZE_RE.match(word)
            if sm:
                size = int(float(sm.group(1)) * SIZE_UNITS[sm.group(2).upper()])
                break
        files[urllib.parse.unquote(m.group(1))] = size
    return files


def resolve_catalog(rows):
    """Find the newest file for each catalog entry, with its approximate size.

    Returns (found rows, titles that couldn't be found on the server).
    """
    listings = {}
    for d in sorted({r["dir"] for r in rows}):
        try:
            listings[d] = parse_listing(fetch(f"{MIRROR}/{d}/"))
        except OSError as e:
            raise Stop("couldn't reach the Kiwix library server (download.kiwix.org).\n"
                       f"Check your internet connection and run the installer again.\n({e})")
    for r in rows:
        pat = re.compile(rf"^{re.escape(r['prefix'])}_(\d{{4}}-\d{{2}}[a-z]?)\.zim$")
        found = sorted((m.group(1), name) for name in listings[r["dir"]] if (m := pat.match(name)))
        if found:
            r["file"] = found[-1][1]
            r["approx_size"] = listings[r["dir"]][r["file"]]
        else:
            r["file"] = None  # reported at the end, if it's part of the chosen size
    if not any(r["file"] for r in rows):
        raise Stop("couldn't find any of the library's collections on the Kiwix server.\n"
                   "The server's layout may have changed. Get the newest version of Offline Kit and try again.")
    return [r for r in rows if r["file"]], [r for r in rows if not r["file"]]


def local_versions(prefix):
    """Files in the library for one catalog entry: (finished .zim files, partial downloads)."""
    pat = re.compile(rf"^{re.escape(prefix)}_\d{{4}}-\d{{2}}[a-z]?\.zim(\.part)?$")
    found = [p for p in LIBRARY.iterdir() if pat.match(p.name)] if LIBRARY.is_dir() else []
    return [p for p in found if p.suffix == ".zim"], [p for p in found if p.suffix == ".part"]


def tier_rows(rows, tier):
    code = TIERS[tier][0]
    return [r for r in rows if code in r["tiers"]]


def remaining_bytes(rows):
    """Bytes still to download for these rows (counting partial downloads)."""
    total = 0
    for r in rows:
        final = LIBRARY / r["file"]
        part = LIBRARY / (r["file"] + ".part")
        if final.exists():
            continue
        total += r["approx_size"] - (part.stat().st_size if part.exists() else 0)
    return max(total, 0)


# ---------------------------------------------------------------- model choice

# Chosen by testing on real questions against the library (see docs/MODELS.md).
MODEL_BY_RAM = [
    # (minimum RAM in GB, model, approx download size)
    (30, "bonsai-2:27b", 8.3 * GB),
    (14, "bonsai-2:9b", 3.2 * GB),
    (0, "bonsai-2:4b", 1.6 * GB),
]


def ram_bytes():
    return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip())


def pick_model():
    ram = ram_bytes() / 1024**3
    for min_ram, model, size in MODEL_BY_RAM:
        if ram >= min_ram:
            return model, size
    return MODEL_BY_RAM[-1][1:]


# ---------------------------------------------------------------- downloads


def meta4(r):
    """Exact size and SHA-256 for a file, from the Kiwix mirror's metalink."""
    xml = fetch(f"{MIRROR}/{r['dir']}/{r['file']}.meta4")
    size = int(re.search(r"<size>(\d+)</size>", xml).group(1))
    sha = re.search(r'<hash type="sha-256">([0-9a-f]{64})</hash>', xml).group(1)
    return size, sha


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


# Kiwix's own server redirects to a mirror, but sometimes picks a slow one, so we
# time a few and use the fastest. Any that fail or are out of date are skipped.
MIRRORS = [
    "https://download.kiwix.org/zim",
    "https://ftp.fau.de/kiwix/zim",
    "https://mirrors.dotsrc.org/kiwix/zim",
    "https://ftp.nluug.nl/pub/kiwix/zim",
    "https://mirror.download.kiwix.org/zim",
    "https://mirrors.ocf.berkeley.edu/kiwix/zim",
    "https://kiwix.mirror.garr.it/zim",
]


def fastest_mirror(sample):
    """Time a short download of `sample` (a catalog row) from each mirror."""
    say("Finding the fastest download server...")
    best, best_speed = MIRRORS[0], 0.0
    for m in MIRRORS:
        url = f"{m}/{sample['dir']}/{sample['file']}"
        try:
            out = subprocess.run(
                ["curl", "-sfL", "-o", "/dev/null", "-r", "0-15999999", "--max-time", "6",
                 "-w", "%{http_code} %{speed_download}", url],
                capture_output=True, text=True, timeout=15).stdout.split()
            code, speed = out[0], float(out[1])
        except Exception:
            continue
        if code in ("200", "206") and speed > best_speed:
            best, best_speed = m, speed
    say(f"  using {best.split('/')[2]} ({best_speed / 1e6:.1f} MB/s)")
    return best


def download(r, index, count, mirror=MIRROR):
    final = LIBRARY / r["file"]
    part = LIBRARY / (r["file"] + ".part")
    if final.exists():
        say(f"[{index}/{count}] {r['title']}: already downloaded")
        return True
    try:
        size, sha = meta4(r)
    except (OSError, AttributeError, ValueError) as e:
        say(f"[{index}/{count}] {r['title']}: couldn't get the file's details from the server ({e}).")
        return False
    say(f"[{index}/{count}] {r['title']} ({human(size)})")
    have = part.stat().st_size if part.exists() else 0
    free = shutil.disk_usage(LIBRARY).free
    if size - have + FLOOR > free:
        say(f"      skipped: not enough free space (needs {human(size - have)}, and this Mac has {human(free)} free).")
        return False
    for attempt in range(3):
        # Use the chosen mirror first; fall back to Kiwix's own server.
        base = mirror if attempt == 0 else MIRROR
        cmd = ["curl", "-fL", "--progress-bar", "--retry", "8", "--retry-delay", "10",
               "--speed-limit", "20000", "--speed-time", "60",
               "-C", "-", "-o", str(part), f"{base}/{r['dir']}/{r['file']}"]
        have = part.stat().st_size if part.exists() else 0
        if have < size:
            subprocess.call(cmd)
            have = part.stat().st_size if part.exists() else 0
        if have == size:
            say("      checking the file is intact...")
            if sha256_of(part) == sha:
                part.rename(final)
                remove_old_versions(r)
                return True
        if have >= size:
            say("      the download was damaged; starting this file again")
            part.unlink(missing_ok=True)
        # Otherwise it's incomplete (e.g. the connection dropped); try to resume.
    say(f"      couldn't download {r['title']}. Run the installer again later to retry.")
    return False


def remove_old_versions(r):
    zims, parts = local_versions(r["prefix"])
    for p in zims + parts:
        if p.name != r["file"]:
            say(f"      removing older version {p.name}")
            p.unlink()


def remove_other_tiers(all_rows, tier):
    """After changing size: remove catalog collections that aren't part of the chosen size.

    Only files that came from the catalog are touched; anything else you put in
    the library folder stays.
    """
    code = TIERS[tier][0]
    freed = 0
    for r in all_rows:
        if code in r["tiers"]:
            continue
        zims, parts = local_versions(r["prefix"])
        for p in zims + parts:
            freed += p.stat().st_size
            say(f"  removing {p.name} (not part of {TIERS[tier][1]})")
            p.unlink()
            stem = p.name.split(".zim")[0]
            for idx in (LIBRARY / ".pdfindex").glob(stem + ".*"):
                idx.unlink()
    if freed:
        say(f"  freed {human(freed)}")


# ---------------------------------------------------------------- ollama


def ollama_env():
    env = dict(os.environ, OLLAMA_HOST=OLLAMA_HOST, OLLAMA_MODELS=str(KIT / "models"))
    return env


def ollama_running():
    try:
        urllib.request.urlopen(f"http://{OLLAMA_HOST}/api/version", timeout=2)
        return True
    except OSError:
        return False


def start_ollama():
    if ollama_running():
        return None
    log = open(KIT / "ollama.log", "a")
    proc = subprocess.Popen([str(KIT / "ollama" / "ollama"), "serve"], env=ollama_env(),
                            stdout=log, stderr=log)
    for _ in range(60):
        if ollama_running():
            return proc
        time.sleep(0.5)
    proc.kill()
    raise RuntimeError("the AI engine (Ollama) didn't start; see ollama.log in the OfflineKit folder")


def pull_model_once(model):
    req = urllib.request.Request(f"http://{OLLAMA_HOST}/api/pull", method="POST",
                                 data=json.dumps({"model": model}).encode())
    last = 0
    with urllib.request.urlopen(req, timeout=600) as resp:
        for line in resp:
            msg = json.loads(line)
            if "error" in msg:
                raise RuntimeError(msg["error"])
            total, done = msg.get("total"), msg.get("completed")
            if total and done and time.time() - last > 2:
                print(f"\r      {human(done)} of {human(total)} ({100 * done // total}%)   ",
                      end="", flush=True)
                last = time.time()
    say("\r      done" + " " * 40)


def pull_model(model):
    """Download the model. Returns None on success, or a message saying what went wrong."""
    error = "unknown problem"
    for attempt in range(4):
        try:
            pull_model_once(model)
            return None
        except urllib.error.HTTPError as e:
            try:
                error = json.loads(e.read()).get("error") or str(e)
            except Exception:
                error = str(e)
        except (OSError, ValueError, RuntimeError) as e:
            error = str(e)
        say(f"\n      the model download was interrupted ({error})")
        if "not found" in error.lower() or "newer version" in error.lower():
            break  # trying again won't help
        if attempt < 3:
            say("      trying again...")
            time.sleep(5 * (attempt + 1))
    return error


def model_present(model):
    full = lambda m: m if ":" in m.rsplit("/", 1)[-1] else m + ":latest"
    try:
        with urllib.request.urlopen(f"http://{OLLAMA_HOST}/api/tags", timeout=10) as r:
            return full(model) in {full(m["name"]) for m in json.load(r).get("models", [])}
    except (OSError, ValueError, KeyError):
        return False


# ---------------------------------------------------------------- PDF search index


def index_pdfs():
    """Make the PDFs inside collections searchable (see pdfindex.py)."""
    sys.path.insert(0, str(CODE / "app"))
    import pdfindex
    from libzim.reader import Archive

    for zim in sorted(LIBRARY.glob("*.zim")):
        try:
            archive = Archive(zim)
            if pdfindex.should_index(archive) and not pdfindex.index_file(LIBRARY, zim).exists():
                say(f"Making the PDFs in {zim.name} searchable...")
                pdfindex.build(archive, zim, LIBRARY, log=say)
        except Exception as e:
            say(f"  couldn't index {zim.name}: {e}")


# ---------------------------------------------------------------- launcher


# Gives a file a custom Finder icon and hides its extension, using AppKit through
# JavaScript for Automation (built into macOS, so nothing extra to install).
SET_ICON_JS = """
ObjC.import("AppKit");
function run(argv) {
  const [icon, file] = argv;
  const image = $.NSImage.alloc.initWithContentsOfFile(icon);
  const ok = $.NSWorkspace.sharedWorkspace.setIconForFileOptions(image, file, 0);
  $.NSURL.fileURLWithPath(file).setResourceValueForKeyError(
    $.NSNumber.numberWithBool(true), $.NSURLHasHiddenExtensionKey, null);
  return ok;
}
"""


def set_icon(path):
    """Make the shortcut look like an app on the Desktop. Best effort: a plain icon still works."""
    try:
        subprocess.run(["osascript", "-l", "JavaScript", "-e", SET_ICON_JS, str(CODE / "app" / "icon.png"), str(path)],
                       capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


UPDATE_SCRIPT = """#!/bin/bash
# Updates the Offline Kit program to the newest version. The library is not touched.
TMP="$(mktemp)"
if curl -fsSL "https://raw.githubusercontent.com/{repo}/main/install.sh" -o "$TMP"; then
  bash "$TMP" --app-only
else
  echo "Couldn't download the update. Check the internet connection."
fi
rm -f "$TMP"
"""


def make_launcher():
    updater = KIT / "Update Offline Kit.command"
    updater.write_text(UPDATE_SCRIPT.format(repo=os.environ.get("OFFLINE_KIT_REPO", "j-irvine/offline-kit")))
    updater.chmod(updater.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    script = f'#!/bin/bash\nexec /bin/bash "{CODE}/start.sh"\n'
    targets = [KIT / "Offline Kit.command"]
    desktop = Path.home() / "Desktop"
    if desktop.is_dir():
        targets.append(desktop / "Offline Kit.command")
    for t in targets:
        t.write_text(script)
        t.chmod(t.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        set_icon(t)
    return targets[-1]


# ---------------------------------------------------------------- main


def choose_tier(rows, args, config, model_size):
    free = shutil.disk_usage(KIT).free
    preset = args.tier or config.get("tier")
    if preset in TIERS:
        need = remaining_bytes(tier_rows(rows, preset)) + model_size
        if need + FLOOR > free:
            raise Stop(f"not enough free space for the {TIERS[preset][1]} size.\n"
                       f"It needs about {human(need)} more, and this Mac has {human(free)} free.\n"
                       "Free up some space (Apple menu > System Settings > General > Storage), or choose a "
                       "smaller size:\n  bash ~/OfflineKit/kit/install.sh --tier essentials")
        return preset
    bold("How much do you want to download?")
    say(f"This Mac has {human(free)} free. About {human(RESERVE)} will be kept free.\n")
    options = list(TIERS)
    recommended = None
    for i, key in enumerate(options, 1):
        need = remaining_bytes(tier_rows(rows, key)) + model_size
        fits = need + RESERVE <= free
        if fits:
            recommended = key
        mark = "" if fits else "   (not enough space)"
        say(f"  {i}. {TIERS[key][1]:<11} {human(need):>9}  {TIERS[key][2]}{mark}")
    if recommended is None:
        say("\nThere isn't enough free space for even the smallest option.")
        say("Free up some space (Apple menu > System Settings > General > Storage) and run the installer again.")
        sys.exit(1)
    default = options.index(recommended) + 1
    answer = ask(f"\nType 1, 2 or 3 and press Return [{default}]: ") or str(default)
    try:
        return options[int(answer) - 1]
    except (ValueError, IndexError):
        say("I didn't understand that; using the recommended option.")
        return recommended


def update_app(config, model):
    """Finish a program-only update: nothing in the library is downloaded, replaced or removed."""
    model_error = None
    try:
        proc = start_ollama()
        try:
            if not model_present(model):
                bold(f"Downloading the AI model ({model})")
                model_error = pull_model(model)
        finally:
            if proc:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
    except (OSError, RuntimeError) as e:
        model_error = str(e)
    config.update(model=model)
    CONFIG.write_text(json.dumps(config, indent=2))
    index_pdfs()
    launcher = make_launcher()
    bold("Offline Kit has been updated." if not model_error else "Almost done.")
    say("Your library was left exactly as it was.")
    if model_error:
        say(f"The AI model ({model}) isn't ready: {model_error}")
        say("The library works without it, but the Ask tab won't. Run this again to retry.")
    say(f"To start Offline Kit, double-click \"{launcher.stem}\" (the green book icon) on your Desktop.")
    return 1 if model_error else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=list(TIERS))
    ap.add_argument("--model")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--app-only", action="store_true")
    ap.add_argument("--skip-library", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    try:
        config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    except ValueError:
        config = {}
    LIBRARY.mkdir(parents=True, exist_ok=True)
    (KIT / "models").mkdir(parents=True, exist_ok=True)
    model, model_size = pick_model()
    model = args.model or config.get("model") or model

    if args.app_only and config.get("tier") in TIERS and not args.tier:
        return update_app(config, model)

    bold("Checking the Kiwix library for the latest versions...")
    catalog = read_catalog()
    rows, missing = resolve_catalog(catalog)
    proc, model_error = None, None
    try:
        proc = start_ollama()
    except (OSError, RuntimeError) as e:
        model_error = str(e)
    try:
        have_model = model_present(model)
        tier = choose_tier(rows, args, config, 0 if have_model else model_size)
        # Smallest first, so most collections are usable early in a long download.
        chosen = sorted(tier_rows(rows, tier), key=lambda r: r["approx_size"])
        config.update(tier=tier, model=model)
        CONFIG.write_text(json.dumps(config, indent=2))

        if not model_error:
            bold(f"Downloading the AI model ({model})")
            model_error = pull_model(model)
    finally:
        if proc:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()

    failed = []
    if not args.skip_library:
        bold(f"Downloading the {TIERS[tier][1]} library ({human(remaining_bytes(chosen))} to go)")
        say("This can take a few hours. You can keep using your Mac. If it gets interrupted,")
        say("just run the installer again and it will continue where it left off.\n")
        todo = [r for r in chosen if not (LIBRARY / r["file"]).exists()]
        mirror = fastest_mirror(max(todo, key=lambda r: r["approx_size"])) if todo else MIRROR
        failed = [r["title"] for i, r in enumerate(chosen, 1) if not download(r, i, len(chosen), mirror)]
        if not failed:
            remove_other_tiers(catalog, tier)
    # Collections the server no longer lists: fine if an earlier copy is installed.
    code = TIERS[tier][0]
    missing = [r["title"] for r in missing if code in r["tiers"] and not local_versions(r["prefix"])[0]]

    index_pdfs()
    launcher = make_launcher()
    problems = failed or missing or model_error
    bold("All set!" if not problems else "Almost done.")
    if failed:
        say("These collections didn't finish downloading: " + ", ".join(failed))
        say("Run the installer again to retry them. Everything else is ready to use.")
    if missing:
        say("These collections couldn't be found on the Kiwix server and were left out: " + ", ".join(missing))
    if model_error:
        say(f"The AI model ({model}) couldn't be downloaded: {model_error}")
        say("The library works without it, but the Ask tab won't. Run the installer again to retry.")
    say(f"To start Offline Kit, double-click \"{launcher.stem}\" (the green book icon) on your Desktop.")
    say("It works without the internet." + ("" if problems else " Try it once now with Wi-Fi turned off."))
    return 1 if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Stop as e:
        say(f"\n\033[31mSomething went wrong: {e}\033[0m")
        sys.exit(1)
    except KeyboardInterrupt:
        say("\n\nStopped. Run the installer again to continue where it left off.")
        sys.exit(130)
