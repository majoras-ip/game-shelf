"""Intake: makes dropped-in games just work.

- Works out which console a disc image is for by looking inside it, so a game in the wrong folder still launches.
- Unpacks .zip / .7z / .rar downloads for disc-based systems, then moves the archive to the Trash.
- Installs PS3 .pkg files (and their .rap licenses) into RPCS3, and lists installed PS3 games.
- Writes the missing .cue sheet for PS1 .bin files.
- Checks for BIOS / firmware / RetroArch cores before launching, with clear messages.
- Downloads missing RetroArch cores from libretro's official build server on request.
"""
import hashlib
import json
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

SUPPORT = Path.home() / "Library/Application Support"
RPCS3_DATA = SUPPORT / "rpcs3"
RPCS3_HDD_GAMES = RPCS3_DATA / "dev_hdd0" / "game"
TRASH = Path.home() / ".Trash"

ARCHIVE_EXTS = {".zip", ".7z", ".rar"}
CART_SYSTEMS = {"NES", "SNES", "Game Boy", "GBA", "N64", "Genesis"}  # RetroArch plays .zip directly

# ---------------------------------------------------------------- jobs

_jobs_lock = threading.Lock()
_jobs = []          # newest last: {"id", "title", "state": running|done|error, "msg", "time"}
_job_seq = [0]
_busy = set()       # paths currently being processed


def _job(title, msg):
    with _jobs_lock:
        _job_seq[0] += 1
        j = {"id": _job_seq[0], "title": title, "state": "running", "msg": msg, "time": time.time()}
        _jobs.append(j)
        del _jobs[:-12]
        return j


def _finish(j, state, msg):
    with _jobs_lock:
        j.update(state=state, msg=msg, time=time.time())


def jobs_snapshot():
    now = time.time()
    with _jobs_lock:
        return [dict(j) for j in _jobs if j["state"] == "running" or now - j["time"] < 25]


def _run_job(key, title, msg, fn):
    if key in _busy:
        return
    _busy.add(key)
    j = _job(title, msg)

    def go():
        try:
            state, text = fn()
            _finish(j, state, text)
        except Exception as e:  # never let a bad file take the shelf down
            _finish(j, "error", f"Something went wrong: {e}")
        finally:
            _busy.discard(key)

    threading.Thread(target=go, daemon=True).start()


def to_trash(p):
    """Move a file or folder to the Trash so it can be recovered."""
    try:
        TRASH.mkdir(exist_ok=True)
        dest = TRASH / p.name
        n = 2
        while dest.exists():
            dest = TRASH / f"{p.stem} {n}{p.suffix}"
            n += 1
        shutil.move(str(p), str(dest))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- disc detection

_detect_cache = {}
_detect_cache_file = [None]
WII_MAGIC = 0x5D1C9EA3
GC_MAGIC = 0xC2339F3D


def init_cache(path):
    _detect_cache_file[0] = path
    try:
        _detect_cache.update(json.loads(path.read_text()))
    except Exception:
        pass


def _save_cache():
    f = _detect_cache_file[0]
    if f:
        try:
            f.write_text(json.dumps(_detect_cache))
        except OSError:
            pass


def _read(fh, off, n):
    fh.seek(off)
    return fh.read(n)


def _iso_root_names(fh):
    """Return (names, read_file) for an ISO9660 image in 2048 or raw 2352-byte sectors, or (None, None)."""
    for ss, base in ((2048, 0), (2352, 24), (2352, 16)):
        pvd = _read(fh, 16 * ss + base, 2048)
        if len(pvd) < 190 or pvd[1:6] != b"CD001":
            continue
        root = pvd[156:190]
        lba, size = struct.unpack_from("<I", root, 2)[0], struct.unpack_from("<I", root, 10)[0]
        size = min(size, 64 * 2048)
        data = b""
        for i in range((size + 2047) // 2048):
            data += _read(fh, (lba + i) * ss + base, 2048)
        names, records, pos = [], {}, 0
        while pos < len(data):
            ln = data[pos]
            if ln == 0:
                pos = ((pos // 2048) + 1) * 2048
                continue
            rec = data[pos:pos + ln]
            if len(rec) < 34:
                break
            nlen = rec[32]
            name = rec[33:33 + nlen].decode("ascii", "ignore").split(";")[0].upper()
            if name not in ("\x00", "\x01", ""):
                names.append(name)
                records[name] = (struct.unpack_from("<I", rec, 2)[0], struct.unpack_from("<I", rec, 10)[0])
            pos += ln

        def read_file(name, limit=4096, _ss=ss, _base=base, _rec=records):
            if name not in _rec:
                return b""
            l2, s2 = _rec[name]
            return _read(fh, l2 * _ss + _base, min(s2, limit))

        return names, read_file
    return None, None


def detect_disc(path):
    """Best guess of the console a disc image belongs to: GameCube, Wii, PS1, PS2, PS3, PSP, Xbox or None."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = f"{path}|{st.st_size}|{int(st.st_mtime)}"
    if key in _detect_cache:
        return _detect_cache[key]
    result = None
    try:
        with open(path, "rb") as fh:
            head = fh.read(0x20)
            if len(head) == 0x20:
                if struct.unpack(">I", head[0x18:0x1C])[0] == WII_MAGIC:
                    result = "Wii"
                elif struct.unpack(">I", head[0x1C:0x20])[0] == GC_MAGIC:
                    result = "GameCube"
            if result is None and path.suffix.lower() == ".ciso":
                h2 = _read(fh, 0x8000, 0x20)
                if len(h2) == 0x20:
                    if struct.unpack(">I", h2[0x18:0x1C])[0] == WII_MAGIC:
                        result = "Wii"
                    elif struct.unpack(">I", h2[0x1C:0x20])[0] == GC_MAGIC:
                        result = "GameCube"
            if result is None:
                for off in (0x10000, 0x18310000, 0xFD90000):
                    if _read(fh, off, 20) == b"MICROSOFT*XBOX*MEDIA":
                        result = "Xbox"
                        break
            if result is None:
                names, read_file = _iso_root_names(fh)
                if names:
                    if "PS3_DISC.SFB" in names or "PS3_GAME" in names:
                        result = "PS3"
                    elif "PSP_GAME" in names or "UMD_DATA.BIN" in names:
                        result = "PSP"
                    elif "SYSTEM.CNF" in names:
                        cnf = read_file("SYSTEM.CNF")
                        result = "PS2" if b"BOOT2" in cnf else "PS1" if b"BOOT" in cnf else None
    except OSError:
        return None
    _detect_cache[key] = result
    _save_cache()
    return result


# ---------------------------------------------------------------- titles from inside games

def read_sfo(path):
    try:
        d = path.read_bytes()
        _, _, kt, dt, n = struct.unpack("<4sIIII", d[:20])
        out = {}
        for i in range(n):
            ko, fmt, ln, mx, do = struct.unpack("<HHIII", d[20 + i * 16:36 + i * 16])
            k = d[kt + ko:d.index(b"\0", kt + ko)].decode()
            v = d[dt + do:dt + do + ln]
            out[k] = struct.unpack("<I", v[:4])[0] if fmt == 0x404 else v.rstrip(b"\0").decode("utf-8", "ignore")
        return out
    except Exception:
        return {}


def ps3_title(d):
    for sfo in (d / "PS3_GAME" / "PARAM.SFO", d / "PARAM.SFO"):
        if sfo.exists():
            t = read_sfo(sfo).get("TITLE")
            if t:
                return re.sub(r"[™®©]", "", t).replace("\n", " ").strip()
    return None


def is_wiiu_folder(d):
    return d.is_dir() and (d / "meta" / "meta.xml").exists() and (d / "code").is_dir()


def wiiu_meta(d):
    try:
        x = (d / "meta" / "meta.xml").read_text(errors="ignore")
    except OSError:
        return {}
    def tag(t):
        m = re.search(rf"<{t}[^>]*>([^<]*)</{t}>", x)
        return m.group(1).strip() if m else ""
    return {"title": tag("longname_en").replace("\n", " "), "title_id": tag("title_id")}


def wiiu_kind(d):
    """'game', 'update', 'dlc' or None for a Wii U title folder."""
    tid = wiiu_meta(d).get("title_id", "").upper()
    if len(tid) == 16:
        return {"0000": "game", "000E": "update", "000C": "dlc"}.get(tid[4:8], "game")
    return "game"


def wiiu_rpx(d):
    rpx = sorted((d / "code").glob("*.rpx"))
    return rpx[0] if rpx else None


def installed_ps3_games():
    """PS3 games installed from .pkg files into RPCS3's virtual hard drive."""
    out = []
    try:
        if not RPCS3_HDD_GAMES.is_dir():
            return out
        for d in sorted(RPCS3_HDD_GAMES.iterdir()):
            sfo = read_sfo(d / "PARAM.SFO") if (d / "PARAM.SFO").exists() else {}
            if sfo.get("CATEGORY") == "HG" and (d / "USRDIR" / "EBOOT.BIN").exists():
                out.append((d, sfo.get("TITLE") or d.name))
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- intake jobs

def _stable(p, sizes):
    """True once a file has stopped growing (so we don't touch a download that's still copying)."""
    try:
        st = p.stat()
    except OSError:
        return False
    if time.time() - st.st_mtime < 8:
        sizes[str(p)] = st.st_size
        return False
    prev = sizes.get(str(p))
    sizes[str(p)] = st.st_size
    return prev == st.st_size


def _unpack(archive, dest_dir):
    tmp = Path(tempfile.mkdtemp(prefix=".unpacking-", dir=dest_dir))
    try:
        if archive.suffix.lower() == ".zip" and not Path("/usr/bin/tar").exists():
            with zipfile.ZipFile(archive) as z:
                z.extractall(tmp)
        else:
            r = subprocess.run(["/usr/bin/tar", "-xf", str(archive), "-C", str(tmp)], capture_output=True, text=True)
            if r.returncode != 0:
                if archive.suffix.lower() == ".zip":
                    with zipfile.ZipFile(archive) as z:
                        z.extractall(tmp)
                else:
                    raise RuntimeError((r.stderr or "unpack failed").strip().splitlines()[-1])
        items = [x for x in tmp.iterdir() if not x.name.startswith((".", "__MACOSX"))]
        # A single wrapper folder that isn't itself a game folder: lift its contents up.
        if len(items) == 1 and items[0].is_dir() and not (
            (items[0] / "PS3_GAME").exists() or (items[0] / "meta").exists() or (items[0] / "PS3_DISC.SFB").exists()
        ):
            items = [x for x in items[0].iterdir() if not x.name.startswith(".")]
        moved = []
        for x in items:
            target = dest_dir / x.name
            if target.exists():
                continue
            shutil.move(str(x), str(target))
            moved.append(target.name)
        return moved
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _write_cue(bins, cue_path):
    lines = []
    for i, b in enumerate(bins, 1):
        lines.append(f'FILE "{b.name}" BINARY')
        if i == 1:
            lines += [f"  TRACK {i:02d} MODE2/2352", "    INDEX 01 00:00:00"]
        else:
            lines += [f"  TRACK {i:02d} AUDIO", "    INDEX 00 00:00:00", "    INDEX 01 00:02:00"]
    cue_path.write_text("\n".join(lines) + "\n")


def _rpcs3_binary(find_app, app_binary):
    app = find_app(["RPCS3"])
    return app_binary(app) if app else None


def process_folders(games_dir, folder_system, find_app, app_binary, sizes):
    """One pass over the games folders: unpack archives, install PS3 packages, add missing PS1 cue sheets."""
    targets = [(games_dir, None)]
    for p in games_dir.iterdir():
        if p.is_dir() and not p.name.startswith("."):
            s = folder_system(p.name)
            if s:
                targets.append((p, s))
    for folder, system in targets:
        try:
            entries = [f for f in folder.iterdir() if f.is_file() and not f.name.startswith(".")]
        except OSError:
            continue
        for f in entries:
            ext = f.suffix.lower()
            if ext in ARCHIVE_EXTS and system not in CART_SYSTEMS or (ext in {".7z", ".rar"} and system in CART_SYSTEMS):
                if not _stable(f, sizes):
                    continue
                def unpack(f=f, folder=folder):
                    moved = _unpack(f, folder)
                    to_trash(f)
                    return "done", (f"Unpacked {', '.join(moved[:2])}" + (" and more" if len(moved) > 2 else "")) if moved else "Nothing new inside, so I left it."
                _run_job(str(f), f.stem, "Unpacking…", unpack)
            elif ext == ".pkg" and (system == "PS3" or folder == games_dir):
                if not _stable(f, sizes):
                    continue
                def install(f=f, folder=folder):
                    binary = _rpcs3_binary(find_app, app_binary)
                    if not binary:
                        return "error", "RPCS3 isn't installed, so this PS3 package can't be installed yet."
                    raps = list(folder.glob("*.rap"))
                    if raps:
                        exdata = RPCS3_DATA / "dev_hdd0" / "home" / "00000001" / "exdata"
                        exdata.mkdir(parents=True, exist_ok=True)
                        for r in raps:
                            shutil.copy2(r, exdata / r.name)
                    r = subprocess.run([str(binary), "--installpkg", str(f)], capture_output=True, text=True, timeout=3600)
                    if r.returncode != 0:
                        return "error", "RPCS3 couldn't install it. Open RPCS3 → File → Install Packages/Raps/Edats and pick it."
                    to_trash(f)
                    for rp in raps:
                        to_trash(rp)
                    return "done", "Installed. It's on your shelf now."
                _run_job(str(f), f.stem, "Installing into RPCS3…", install)
        if system == "PS1":
            try:
                cues = {c.stem.lower() for c in folder.glob("*.cue")} | {c.stem.lower() for c in folder.glob("*.m3u")}
                groups = {}
                for b in folder.glob("*.bin"):
                    base = re.sub(r"\s*\(Track \d+\)$", "", b.stem, flags=re.I)
                    groups.setdefault(base, []).append(b)
                for base, bins in groups.items():
                    if base.lower() in cues or any(b.stem.lower() in cues for b in bins):
                        continue
                    if not all(_stable(b, sizes) for b in bins):
                        continue
                    bins.sort(key=lambda b: int(m.group(1)) if (m := re.search(r"\(Track (\d+)\)", b.stem, re.I)) else 0)
                    _write_cue(bins, folder / f"{base}.cue")
                    _finish(_job(base, ""), "done", "Added the missing .cue file so it can boot.")
            except OSError:
                pass


def folders_signature(games_dir, folder_system):
    h = hashlib.md5()
    try:
        dirs = [games_dir] + [p for p in games_dir.iterdir() if p.is_dir() and folder_system(p.name)]
        for d in dirs:
            for p in sorted(d.iterdir()):
                if p.name.startswith(".unpacking-"):
                    continue
                try:
                    st = p.stat()
                    h.update(f"{p.name}|{st.st_size}|{int(st.st_mtime)};".encode())
                except OSError:
                    pass
        if RPCS3_HDD_GAMES.is_dir():
            h.update(",".join(sorted(x.name for x in RPCS3_HDD_GAMES.iterdir())).encode())
    except OSError:
        pass
    return h.hexdigest()[:12]


def start(games_dir, folder_system, find_app, app_binary, log_error):
    sizes = {}

    def loop():
        while True:
            try:
                process_folders(games_dir, folder_system, find_app, app_binary, sizes)
            except Exception as e:
                log_error(f"Intake pass failed: {e!r}")
            time.sleep(4)

    threading.Thread(target=loop, daemon=True).start()


# ---------------------------------------------------------------- before launching

def _has_files(d, exts=None):
    try:
        return d.is_dir() and any(f.is_file() and (exts is None or f.suffix.lower() in exts) for f in d.iterdir())
    except OSError:
        return False


def preflight(system, core_path=None):
    """Return (message, fix) if something the emulator needs is missing, else (None, None)."""
    if system == "PS1" and not _has_files(SUPPORT / "DuckStation" / "bios"):
        return ("PS1 games need a PS1 BIOS from your own console. Add it in DuckStation → Settings → BIOS, "
                "then try again."), None
    if system == "PS2" and not _has_files(SUPPORT / "PCSX2" / "bios"):
        return ("PS2 games need a PS2 BIOS from your own console. Add it in PCSX2 → Preferences → BIOS, "
                "then try again."), None
    if system == "PS3" and RPCS3_DATA.is_dir() and not (RPCS3_DATA / "dev_flash" / "vsh" / "module").is_dir():
        return ("PS3 firmware isn't installed yet. Put Sony's PS3UPDAT.PUP in Emulators → _installers "
                "and reopen Game Shelf."), None
    if core_path is not None and not core_path.exists():
        return (f"RetroArch needs the {core_path.stem.replace('_libretro', '')} core to play {system} games."), \
               {"action": "core", "system": system, "label": "Download it"}
    return None, None


CORE_URL = "https://buildbot.libretro.com/nightly/apple/osx/{arch}/latest/{core}.dylib.zip"


def download_core(core, cores_dir, system):
    def fetch():
        import platform
        arch = "arm64" if platform.machine() == "arm64" else "x86_64"
        url = CORE_URL.format(arch=arch, core=core)
        cores_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            zp = Path(td) / "core.zip"
            try:
                with urllib.request.urlopen(url, timeout=120) as r, open(zp, "wb") as out:
                    shutil.copyfileobj(r, out)
            except OSError:
                return "error", "Couldn't reach libretro's download server. Check your internet connection and try again."
            with zipfile.ZipFile(zp) as z:
                z.extractall(cores_dir)
        if not (cores_dir / f"{core}.dylib").exists():
            return "error", "The download didn't include the core. Install it from RetroArch → Online Updater."
        return "done", f"{system} is ready to play."
    _run_job("core:" + core, f"{system} core", "Downloading from libretro…", fetch)
