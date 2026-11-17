"""Offline Kit setup: choose a size, download the library and AI model, make the Desktop shortcut.

Run by install.sh. Safe to re-run at any time: finished files are kept, partial
downloads resume, and newer versions of collections replace older ones (so
re-running is also how you update).

Options:
  --tier essentials|standard|complete   skip the size question
  --model NAME                          use a specific Ollama model
  --yes                                 don't ask for confirmation
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


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "offline-kit"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- catalog


def read_catalog():
    rows = []
    for line in (CODE / "catalog.tsv").read_text().splitlines():
        if not line.strip() or line.startswith("#") or line.startswith("tiers\t"):
            continue
        tiers, d, prefix, title, about = line.split("\t")
        rows.append(dict(tiers=tiers, dir=d, prefix=prefix, title=title, about=about))
    return rows


SIZE_UNITS = {"K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
LISTING_RE = re.compile(r'href="([^"/]+\.zim)">[^<]*</a>\s+\S+\s+\S+\s+([0-9.]+)([KMGT])')


def resolve_catalog(rows):
    """Find the newest file for each catalog entry, with its approximate size."""
    listings = {}
    for d in sorted({r["dir"] for r in rows}):
        files = {}
        for name, num, unit in LISTING_RE.findall(fetch(f"{MIRROR}/{d}/")):
            files[name] = int(float(num) * SIZE_UNITS[unit])
        listings[d] = files
    for r in rows:
        pat = re.compile(rf"^{re.escape(r['prefix'])}_(\d{{4}}-\d{{2}}[a-z]?)\.zim$")
        found = sorted((m.group(1), name) for name in listings[r["dir"]] if (m := pat.match(name)))
        if found:
            r["file"] = found[-1][1]
            r["approx_size"] = listings[r["dir"]][r["file"]]
        else:
            r["file"] = None
            say(f"  (couldn't find {r['prefix']} on the Kiwix server; skipping it)")
    return [r for r in rows if r["file"]]


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
    size, sha = meta4(r)
    say(f"[{index}/{count}] {r['title']} ({human(size)})")
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
    pat = re.compile(rf"^{re.escape(r['prefix'])}_\d{{4}}-\d{{2}}[a-z]?\.zim(\.part)?$")
    for p in LIBRARY.iterdir():
        if pat.match(p.name) and p.name != r["file"]:
            say(f"      removing older version {p.name}")
            p.unlink()


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
    raise RuntimeError("Ollama didn't start; see ~/OfflineKit/ollama.log")


def pull_model(model):
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


# ---------------------------------------------------------------- PDF search index


def index_pdfs():
    """Make the PDFs inside collections searchable (see pdfindex.py)."""
    sys.path.insert(0, str(CODE / "app"))
    import pdfindex
    from libzim.reader import Archive

    for zim in sorted(LIBRARY.glob("*.zim")):
        try:
            archive = Archive(zim)
            if pdfindex.pdf_count(archive) and not pdfindex.index_file(LIBRARY, zim).exists():
                say(f"Making the PDFs in {zim.name} searchable...")
                pdfindex.build(archive, zim, LIBRARY, log=say)
        except Exception as e:
            say(f"  couldn't index {zim.name}: {e}")


# ---------------------------------------------------------------- launcher


def make_launcher():
    script = f'#!/bin/bash\nexec /bin/bash "{CODE}/start.sh"\n'
    targets = [KIT / "Offline Kit.command"]
    desktop = Path.home() / "Desktop"
    if desktop.is_dir():
        targets.append(desktop / "Offline Kit.command")
    for t in targets:
        t.write_text(script)
        t.chmod(t.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return targets[-1]


# ---------------------------------------------------------------- main


def choose_tier(rows, args, config, model_size):
    if args.tier:
        return args.tier
    if config.get("tier"):
        return config["tier"]
    free = shutil.disk_usage(KIT).free
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=list(TIERS))
    ap.add_argument("--model")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--skip-library", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    model, model_size = pick_model()
    model = args.model or config.get("model") or model

    bold("Checking the Kiwix library for the latest versions...")
    rows = resolve_catalog(read_catalog())
    tier = choose_tier(rows, args, config, model_size)
    # Smallest first, so most collections are usable early in a long download.
    chosen = sorted(tier_rows(rows, tier), key=lambda r: r["approx_size"])
    config.update(tier=tier, model=model)
    CONFIG.write_text(json.dumps(config, indent=2))

    bold(f"Downloading the AI model ({model})")
    proc = start_ollama()
    try:
        pull_model(model)
    finally:
        if proc:
            proc.send_signal(signal.SIGTERM)

    if not args.skip_library:
        bold(f"Downloading the {TIERS[tier][1]} library ({human(remaining_bytes(chosen))} to go)")
        say("This can take a few hours. You can keep using your Mac. If it gets interrupted,")
        say("just run the installer again and it will continue where it left off.\n")
        todo = [r for r in chosen if not (LIBRARY / r["file"]).exists()]
        mirror = fastest_mirror(max(todo, key=lambda r: r["approx_size"])) if todo else MIRROR
        failed = [r["title"] for i, r in enumerate(chosen, 1) if not download(r, i, len(chosen), mirror)]
    else:
        failed = []

    index_pdfs()
    launcher = make_launcher()
    bold("All set!" if not failed else "Almost done.")
    if failed:
        say("These collections didn't finish downloading: " + ", ".join(failed))
        say("Run the installer again to retry them. Everything else is ready to use.")
    say(f"To start Offline Kit, double-click \"{launcher.name}\" on your Desktop.")
    say("It works without the internet.")


if __name__ == "__main__":
    main()
