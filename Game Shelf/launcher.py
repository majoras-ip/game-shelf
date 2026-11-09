#!/usr/bin/env python3
"""Game Shelf: a local launcher for the Dolphin games in this folder."""
import json
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
GAMES_DIR = HERE.parent
STATE_FILE = HERE / "state.json"
INDEX = HERE / "index.html"
EXTS = {".iso", ".gcm", ".ciso", ".gcz", ".rvz", ".wia", ".wbfs", ".wad", ".dol", ".elf"}

# Each system: folder names it can live in, file types, the emulator that runs it, and how to launch it.
RA_CORES = Path.home() / "Library/Application Support/RetroArch/cores"
SYSTEMS = {
    "GameCube": {"folders": ["GameCube", "GC"], "exts": EXTS, "emu": "dolphin"},
    "Wii":      {"folders": ["Wii"], "exts": EXTS, "emu": "dolphin"},
    "NES":      {"folders": ["NES"], "exts": {".nes", ".zip"}, "emu": "retroarch", "core": "nestopia_libretro"},
    "SNES":     {"folders": ["SNES", "Super Nintendo"], "exts": {".sfc", ".smc", ".zip"}, "emu": "retroarch", "core": "snes9x_libretro"},
    "Game Boy": {"folders": ["Game Boy", "GB", "GBC", "Game Boy Color"], "exts": {".gb", ".gbc", ".zip"}, "emu": "retroarch", "core": "gambatte_libretro"},
    "GBA":      {"folders": ["GBA", "Game Boy Advance"], "exts": {".gba", ".zip"}, "emu": "retroarch", "core": "mgba_libretro"},
    "N64":      {"folders": ["N64", "Nintendo 64"], "exts": {".z64", ".n64", ".v64", ".zip"}, "emu": "retroarch", "core": "mupen64plus_next_libretro"},
    "Genesis":  {"folders": ["Genesis", "Mega Drive"], "exts": {".md", ".gen", ".smd", ".bin", ".zip"}, "emu": "retroarch", "core": "genesis_plus_gx_libretro"},
    "PS1":      {"folders": ["PS1", "PSX", "PlayStation"], "exts": {".cue", ".chd", ".pbp", ".m3u", ".iso"}, "emu": "duckstation"},
    "PS2":      {"folders": ["PS2", "PlayStation 2"], "exts": {".iso", ".chd", ".cso", ".gz"}, "emu": "pcsx2"},
    "PSP":      {"folders": ["PSP"], "exts": {".iso", ".cso", ".pbp", ".chd"}, "emu": "ppsspp"},
    "Xbox":     {"folders": ["Xbox"], "exts": {".iso", ".xiso"}, "emu": "xemu"},
    "Wii U":    {"folders": ["Wii U", "WiiU"], "exts": {".wua", ".wux", ".wud", ".rpx"}, "emu": "cemu"},
    "PS3":      {"folders": ["PS3", "PlayStation 3"], "exts": {".iso"}, "emu": "rpcs3", "dirs": True},
}
EMULATORS = {
    "dolphin":     {"name": "Dolphin", "apps": ["Dolphin"], "get": "dolphin-emu.org"},
    "retroarch":   {"name": "RetroArch", "apps": ["RetroArch"], "get": "brew install --cask retroarch-metal"},
    "duckstation": {"name": "DuckStation", "apps": ["DuckStation"], "get": "brew install --cask duckstation"},
    "pcsx2":       {"name": "PCSX2", "apps": ["PCSX2"], "get": "brew install --cask pcsx2"},
    "ppsspp":      {"name": "PPSSPP", "apps": ["PPSSPP", "PPSSPPSDL"], "bins": ["PPSSPPSDL", "ppsspp"], "get": "ppsspp.org"},
    "xemu":        {"name": "xemu", "apps": ["xemu"], "get": "brew install --cask xemu"},
    "cemu":        {"name": "Cemu", "apps": ["Cemu"], "get": "cemu.info"},
    "rpcs3":       {"name": "RPCS3", "apps": ["RPCS3"], "get": "rpcs3.net"},
}
ALL_EXTS = set().union(*(v["exts"] for v in SYSTEMS.values()))
EMU_DIR = GAMES_DIR / "Emulators"


def is_ps3_folder(d):
    return d.is_dir() and ((d / "PS3_GAME").is_dir() or (d / "USRDIR" / "EBOOT.BIN").exists() or (d / "PS3_DISC.SFB").exists())


def folder_system(name):
    n = name.strip().lower()
    for sysname, info in SYSTEMS.items():
        if n in (f.lower() for f in info["folders"]):
            return sysname
    return None


def game_files():
    """Yield (path, system) for every game: Dolphin games in the main folder, others in system subfolders."""
    for p in sorted(GAMES_DIR.iterdir(), key=lambda x: x.name.lower()):
        if p.name.startswith(".") or p.name in (HERE.name, "Emulators"):
            continue
        if p.is_file() and p.suffix.lower() in EXTS:
            yield p, None
        elif p.is_dir():
            sysname = folder_system(p.name)
            if not sysname:
                continue
            if SYSTEMS[sysname].get("dirs"):
                for d in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                    if not d.name.startswith(".") and is_ps3_folder(d):
                        yield d, sysname
            files = sorted((f for f in p.iterdir() if f.is_file() and not f.name.startswith(".")), key=lambda x: x.name.lower())
            stems_with_cue = {f.stem.lower() for f in files if f.suffix.lower() in (".cue", ".m3u")}
            for f in files:
                ext = f.suffix.lower()
                if ext not in SYSTEMS[sysname]["exts"]:
                    continue
                if ext in (".bin", ".iso") and sysname == "PS1" and f.stem.lower() in stems_with_cue:
                    continue  # the .cue sheet stands for its .bin tracks
                if ext == ".bin" and sysname != "Genesis":
                    continue
                yield f, sysname
WII_MAGIC = 0x5D1C9EA3
GC_MAGIC = 0xC2339F3D
COVER_DIRS = [
    HERE / "covers",
    Path.home() / "Library/Application Support/Dolphin/Cache/GameCovers",
]
BG_DIR = HERE / "backgrounds"
DISC_DIR = HERE / "discs"
IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")
VID_EXTS = (".mp4", ".m4v", ".mov", ".webm")
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
        ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
PORT = 8642
_lock = threading.Lock()
LAST_SEEN = time.time()
IDLE_LIMIT = 600  # seconds with no shelf window open before the server quits


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        print("Could not save play history:", e)


def clean_title(stem):
    title = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", stem).strip()
    # "Legend of Zelda, The - The Wind Waker" -> "The Legend of Zelda - The Wind Waker"
    title = re.sub(r"^(.+?), (The|A|An)(?=$| - )", r"\2 \1", title)
    return title or stem


def detect_system(path):
    ext = path.suffix.lower()
    if ext in (".wbfs", ".wad"):
        return "Wii"
    if ext in (".dol", ".elf"):
        return "Homebrew"
    offset = {".iso": 0, ".gcm": 0, ".ciso": 0x8000}.get(ext)
    if offset is not None:
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                head = f.read(0x20)
            if len(head) == 0x20:
                if struct.unpack(">I", head[0x18:0x1C])[0] == WII_MAGIC:
                    return "Wii"
                if struct.unpack(">I", head[0x1C:0x20])[0] == GC_MAGIC:
                    return "GameCube"
                if head[:1] in (b"R", b"S"):
                    return "Wii"
        except OSError:
            pass
    return "GameCube"


def read_game_id(path):
    offset = {".iso": 0, ".gcm": 0, ".ciso": 0x8000, ".wbfs": 0x200}.get(path.suffix.lower())
    if offset is None:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            raw = f.read(6)
        gid = raw.decode("ascii")
        return gid if len(gid) == 6 and gid.isalnum() else None
    except Exception:
        return None


def _match(folder, names, exts):
    try:
        if not folder.is_dir():
            return None
        wanted = {n.lower() for n in names if n}
        for f in folder.iterdir():
            if f.suffix.lower() in exts and f.stem.lower() in wanted:
                return f
    except OSError as e:
        log_error(f"Couldn't read {folder}: {e}")
    return None


def log_error(msg):
    try:
        with open(HERE / "errors.log", "a") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def find_cover(path, title, gid):
    names = [title, path.stem, gid, gid[:4] if gid else None]
    for d in COVER_DIRS:
        hit = _match(d, names, IMG_EXTS)
        if hit:
            return hit
    return None


def find_disc(path, title, gid):
    return _match(DISC_DIR, [title, path.stem, gid, gid[:4] if gid else None], IMG_EXTS)


YT_RE = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/))([A-Za-z0-9_-]{11})")


def find_youtube(path, title, gid):
    f = _match(BG_DIR, [title, path.stem, gid], (".txt",))
    if not f:
        return None
    try:
        text = f.read_text(errors="ignore")
    except OSError:
        return None
    m = YT_RE.search(text)
    if m:
        return m.group(1)
    t = text.strip()
    return t if re.fullmatch(r"[A-Za-z0-9_-]{11}", t) else None


def find_background(path, title, gid):
    return _match(BG_DIR, [title, path.stem, gid], VID_EXTS)


def media_for(name):
    try:
        p = (GAMES_DIR / name).resolve()
        p.relative_to(GAMES_DIR.resolve())
    except (ValueError, OSError):
        return None, None, None
    if HERE.resolve() in p.parents or not (p.is_file() and p.suffix.lower() in ALL_EXTS or is_ps3_folder(p)):
        return None, None, None
    title, gid = clean_title(p.name if p.is_dir() else p.stem), (read_game_id(p) if p.is_file() else None)
    return find_cover(p, title, gid), find_background(p, title, gid), find_disc(p, title, gid)


def scan():
    state = load_state()
    games = []
    for p, sysname in game_files():
      try:
        rel = p.relative_to(GAMES_DIR).as_posix()
        dolphin = sysname in (None, "GameCube", "Wii")
        system = detect_system(p) if sysname is None else sysname
        if sysname in ("GameCube", "Wii") and p.suffix.lower() in (".iso", ".ciso", ".gcm"):
            system = detect_system(p)
        info = state.get(rel, state.get(p.name, {})) if sysname is None else state.get(rel, {})
        title = clean_title(p.name if p.is_dir() else p.stem)
        gid = read_game_id(p) if dolphin else None
        q = urllib.parse.quote(rel)
        cover, bg, disc = find_cover(p, title, gid), find_background(p, title, gid), find_disc(p, title, gid)
        g = {
            "id": gid,
            "cover": f"/media/cover?file={q}&v={int(cover.stat().st_mtime)}" if cover else None,
            "bg": f"/media/bg?file={q}&v={int(bg.stat().st_mtime)}" if bg else None,
            "disc": f"/media/disc?file={q}&v={int(disc.stat().st_mtime)}" if disc else None,
            "yt": find_youtube(p, title, gid),
            "resume": None, "shots": 0, "profileKind": None, "profile": None,
            "file": rel,
            "title": title,
            "system": system,
            "emu": SYSTEMS.get(system, {}).get("emu", "dolphin"),
            "format": "Folder" if p.is_dir() else p.suffix[1:].upper(),
            "size": 0 if p.is_dir() else p.stat().st_size,
            "lastPlayed": info.get("lastPlayed"),
            "plays": info.get("plays", 0),
        }
        if dolphin:
            st = latest_state(gid)
            g["resume"] = {"time": st.stat().st_mtime, "slot": int(st.suffix[2:])} if st else None
            g["shots"] = len(list_shots(gid))
            kind, key = profile_kind(system)
            g["profileKind"] = kind
            g["profile"] = get_game_profile(gid, key) if gid else None
        games.append(g)
      except Exception as e:
        log_error(f"Skipped {p.name}: {e!r}")
    return games


DOLPHIN_DATA = Path.home() / "Library/Application Support/Dolphin"
STATE_DIR = DOLPHIN_DATA / "StateSaves"
SHOT_DIR = DOLPHIN_DATA / "ScreenShots"
GAMESET_DIR = DOLPHIN_DATA / "GameSettings"
PROFILE_DIR = DOLPHIN_DATA / "Config" / "Profiles"


def latest_state(gid):
    if not gid:
        return None
    try:
        states = [f for f in STATE_DIR.glob(gid + ".s*") if re.fullmatch(r"\.s\d\d", f.suffix) and f.is_file()]
    except OSError as e:
        log_error(f"Couldn't read save states: {e}")
        return None
    return max(states, key=lambda f: f.stat().st_mtime) if states else None


def list_shots(gid):
    if not gid:
        return []
    try:
        d = SHOT_DIR / gid
        if not d.is_dir():
            return []
        shots = [f for f in d.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg")]
    except OSError as e:
        log_error(f"Couldn't read screenshots: {e}")
        return []
    return sorted(shots, key=lambda f: f.stat().st_mtime, reverse=True)


def profile_kind(system):
    return ("Wiimote", "WiimoteProfile1") if system == "Wii" else ("GCPad", "PadProfile1")


def list_profiles(kind):
    try:
        d = PROFILE_DIR / kind
        return sorted(f.stem for f in d.glob("*.ini")) if d.is_dir() else []
    except OSError:
        return []


def read_ini(path):
    try:
        return path.read_text(errors="ignore").splitlines()
    except OSError:
        return []


def get_game_profile(gid, key):
    section = None
    for line in read_ini(GAMESET_DIR / f"{gid}.ini"):
        t = line.strip()
        if t.startswith("[") and t.endswith("]"):
            section = t[1:-1].strip().lower()
        elif section == "controls" and "=" in t and t.split("=", 1)[0].strip() == key:
            return t.split("=", 1)[1].strip() or None
    return None


def set_game_profile(gid, key, profile):
    path = GAMESET_DIR / f"{gid}.ini"
    lines = read_ini(path)
    out, section, done, in_controls = [], None, False, False
    for line in lines:
        t = line.strip()
        if t.startswith("[") and t.endswith("]"):
            if in_controls and not done and profile:
                out.append(f"{key} = {profile}")
                done = True
            section = t[1:-1].strip().lower()
            in_controls = section == "controls"
            out.append(line)
            continue
        if in_controls and "=" in t and t.split("=", 1)[0].strip() == key:
            if profile and not done:
                out.append(f"{key} = {profile}")
                done = True
            continue
        out.append(line)
    if profile and not done:
        if not in_controls:
            if out and out[-1].strip():
                out.append("")
            out.append("[Controls]")
        out.append(f"{key} = {profile}")
    GAMESET_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n")


DEFAULT_DEVICE = "SDL/0/Xbox Series X Controller"
BASE_LAYOUT = {
    "Buttons/A": "`Button S`", "Buttons/B": "`Button W`", "Buttons/X": "`Button E`", "Buttons/Y": "`Button N`",
    "Buttons/Z": "`Shoulder R`", "Buttons/Start": "Start",
    "Main Stick/Up": "`Left Y-`", "Main Stick/Down": "`Left Y+`", "Main Stick/Left": "`Left X-`", "Main Stick/Right": "`Left X+`",
    "Main Stick/Dead Zone": "10.", 
    "C-Stick/Up": "`Right Y-`", "C-Stick/Down": "`Right Y+`", "C-Stick/Left": "`Right X-`", "C-Stick/Right": "`Right X+`",
    "C-Stick/Dead Zone": "12.",
    "Triggers/L": "`Trigger L`", "Triggers/R": "`Trigger R`", "Triggers/L-Analog": "`Trigger L`", "Triggers/R-Analog": "`Trigger R`",
    "Triggers/Threshold": "50.",
    "D-Pad/Up": "`Pad N`", "D-Pad/Down": "`Pad S`", "D-Pad/Left": "`Pad W`", "D-Pad/Right": "`Pad E`",
    "Rumble/Motor": "Motor",
}
PRESETS = {
    "standard": {},
    "kart": {"Buttons/A": "`Button S` | `Shoulder R`", "Buttons/Z": "`Shoulder L`",
             "Triggers/R": "`Trigger R`", "Triggers/R-Analog": "`Trigger R`"},
    "kart_rt": {"Buttons/A": "`Trigger R` | `Button S`", "Buttons/Z": "`Shoulder L`",
                "Triggers/R": "`Shoulder R`", "Triggers/R-Analog": "`Shoulder R`"},
}


def current_pad_section():
    section, out = None, {}
    for line in read_ini(DOLPHIN_DATA / "Config" / "GCPadNew.ini"):
        t = line.strip()
        if t.startswith("[") and t.endswith("]"):
            section = t[1:-1]
        elif section == "GCPad1" and "=" in t:
            k, v = t.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def create_profile(name, preset):
    if preset == "current":
        layout = current_pad_section()
        if not layout:
            raise ValueError("Couldn't find your current Dolphin controls. Set them up in Dolphin first.")
    else:
        if preset not in PRESETS:
            raise ValueError("Unknown layout.")
        layout = {"Device": current_pad_section().get("Device") or DEFAULT_DEVICE, **BASE_LAYOUT, **PRESETS[preset]}
    d = PROFILE_DIR / "GCPad"
    d.mkdir(parents=True, exist_ok=True)
    lines = ["[Profile]"] + [f"{k} = {v}" for k, v in layout.items()]
    (d / f"{name}.ini").write_text("\n".join(lines) + "\n")


def find_dolphin():
    for c in (EMU_DIR / "Dolphin.app", Path("/Applications/Dolphin.app"), Path.home() / "Applications/Dolphin.app"):
        if c.exists():
            return c
    try:
        out = subprocess.run(
            ["mdfind", "kMDItemCFBundleIdentifier == 'org.dolphin-emu.dolphin'"],
            capture_output=True, text=True, timeout=5,
        ).stdout.splitlines()
        for line in out:
            if line.endswith(".app"):
                return Path(line)
    except Exception:
        pass
    return None


def on_battery():
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=3).stdout
        return "Battery Power" in out
    except Exception:
        return False


def performance_flags():
    # Applied only for this launch; your own Dolphin settings are left untouched.
    res = "2"  # 2x looks sharp on a laptop screen and keeps a MacBook Air at full speed
    settings = {
        "Dolphin.Core.GFXBackend": "Metal",               # native Mac graphics, fastest on Apple chips
        "Dolphin.Core.CPUThread": "True",                 # dual core
        "GFX.Settings.InternalResolution": res,           # 3x plugged in, 2x on battery
        "GFX.Settings.ShaderCompilationMode": "3",        # compile shaders in the background; never slows the game down
        "GFX.Settings.WaitForShadersBeforeStarting": "True",
        "GFX.Settings.MSAA": "1",                         # anti-aliasing off: saves the most GPU
        "GFX.Enhancements.MaxAnisotropy": "1",            # 2x texture filtering, nearly free
        "GFX.Settings.EnableGPUTextureDecoding": "False",
        "GFX.Hacks.EFBToTextureEnable": "True",           # Dolphin's default speed hacks, enforced
        "GFX.Hacks.XFBToTextureEnable": "True",
        "GFX.Hacks.SkipDuplicateXFBs": "True",
        "Dolphin.Core.CPUCore": "4",                      # ARM JIT: the fast CPU engine on Apple chips
        "Dolphin.Display.Fullscreen": "True",
    }
    flags = []
    for k, v in settings.items():
        flags += ["-C", f"{k}={v}"]
    return flags, res


def find_app(names):
    for n in names:
        for base in (EMU_DIR, Path("/Applications"), Path.home() / "Applications"):
            c = base / f"{n}.app"
            if c.exists():
                return c
    for n in names:
        try:
            out = subprocess.run(["mdfind", f"kMDItemFSName == '{n}.app'"], capture_output=True, text=True, timeout=5).stdout.splitlines()
            for line in out:
                if line.endswith(".app") and "/Volumes/" not in line:
                    return Path(line)
        except Exception:
            pass
    return None


def app_binary(app):
    import plistlib
    try:
        with open(app / "Contents/Info.plist", "rb") as f:
            exe = plistlib.load(f).get("CFBundleExecutable")
        if exe and (app / "Contents/MacOS" / exe).exists():
            return app / "Contents/MacOS" / exe
    except Exception:
        pass
    found = list((app / "Contents/MacOS").glob("*"))
    return found[0] if found else None


def emulator_command(emu, path):
    """Return (argv, error) for launching a non-Dolphin game."""
    info = EMULATORS[emu]
    binary = None
    app = find_app(info["apps"])
    if app:
        binary = app_binary(app)
    if not binary:
        for b in info.get("bins", []):
            for d in ("/opt/homebrew/bin", "/usr/local/bin"):
                if Path(d, b).exists():
                    binary = Path(d, b)
                    break
            if binary:
                break
    if not binary:
        return None, f"{info['name']} isn't installed. Get it with: {info['get']}"
    b, f = str(binary), str(path)
    if emu == "retroarch":
        return None, None  # handled by caller (needs the core)
    if emu == "duckstation":
        return [b, "-batch", "-fullscreen", "--", f], None
    if emu == "pcsx2":
        return [b, "-batch", "-fullscreen", "--", f], None
    if emu == "ppsspp":
        return [b, "--fullscreen", f], None
    if emu == "xemu":
        return [b, "-full-screen", "-dvd_path", f], None
    if emu == "cemu":
        return [b, "-f", "-g", f], None
    if emu == "rpcs3":
        return [b, "--no-gui", "--fullscreen", f], None
    return None, "Unknown emulator."


def launch_other(path, system):
    emu = SYSTEMS[system]["emu"]
    if emu == "retroarch":
        app = find_app(EMULATORS["retroarch"]["apps"])
        binary = app_binary(app) if app else None
        if not binary:
            return f"RetroArch isn't installed. Get it with: {EMULATORS['retroarch']['get']}"
        core = RA_CORES / (SYSTEMS[system]["core"] + ".dylib")
        if not core.exists():
            nice = SYSTEMS[system]["core"].replace("_libretro", "")
            return f"RetroArch needs the {nice} core for {system}. Open RetroArch → Online Updater → Core Downloader and install it."
        argv = [str(binary), "-f", "-L", str(core), str(path)]
    else:
        argv, err = emulator_command(emu, path)
        if err:
            return err
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return f"{EMULATORS[emu]['name']} couldn't start: {e}"
    return None


def install_rpcs3_firmware_once():
    """If a PS3UPDAT.PUP is waiting in Emulators/_installers, install it into RPCS3 one time."""
    pup = EMU_DIR / "_installers" / "PS3UPDAT.PUP"
    done = HERE / ".rpcs3_firmware_installed"
    if not pup.exists() or done.exists():
        return
    app = find_app(EMULATORS["rpcs3"]["apps"])
    binary = app_binary(app) if app else None
    if not binary:
        return
    def run():
        try:
            with open(HERE / "rpcs3_firmware.log", "w") as log:
                r = subprocess.run([str(binary), "--installfw", str(pup)], stdout=log, stderr=subprocess.STDOUT, timeout=900)
            if r.returncode == 0:
                done.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            log_error(f"RPCS3 firmware install failed: {e!r}")
    threading.Thread(target=run, daemon=True).start()


def launch(path, state=None):
    app = find_dolphin()
    if app is None:
        return "Dolphin wasn't found. Move Dolphin.app into your Applications folder, then try again."
    binary = app / "Contents/MacOS/Dolphin"
    try:
        # -b closes Dolphin when you stop the game, so you land back on the shelf.
        subprocess.Popen(
            [str(binary), "-b", *performance_flags()[0], "-e", str(path), *(["-s", str(state)] if state else [])],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as e:
        return f"Dolphin couldn't start: {e}"
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def send_file(self, f):
        size = f.stat().st_size
        ctype = MIME.get(f.suffix.lower(), "application/octet-stream")
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            try:
                if a:
                    start = int(a)
                    end = int(b) if b else end
                else:
                    start = max(size - int(b), 0)
            except ValueError:
                start, end = 0, size - 1
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with open(f, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            import traceback
            log_error("GET " + self.path + "\n" + traceback.format_exc())
            try:
                self.send_json(500, {"error": "Something went wrong. Details are in errors.log."})
            except Exception:
                pass

    def _get(self):
        url = urllib.parse.urlparse(self.path)
        if url.path in ("/media/cover", "/media/bg", "/media/disc"):
            name = urllib.parse.parse_qs(url.query).get("file", [""])[0]
            cover, bg, disc = media_for(name)
            f = {"/media/cover": cover, "/media/bg": bg, "/media/disc": disc}[url.path]
            return self.send_file(f) if f else self.send_json(404, {"error": "No file"})
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/ping":
            global LAST_SEEN
            LAST_SEEN = time.time()
            self.send_json(200, {"app": "shelf"})
        elif url.path == "/api/shots":
            g = self._game(urllib.parse.parse_qs(url.query).get("file", [""])[0])
            if not g:
                return self.send_json(404, {"error": "Not found"})
            q = urllib.parse.quote(g["file"])
            shots = list_shots(g["id"])[:12]
            self.send_json(200, {"shots": [{"url": f"/media/shot?file={q}&name={urllib.parse.quote(f.name)}", "time": f.stat().st_mtime} for f in shots]})
        elif url.path == "/media/shot":
            qs = urllib.parse.parse_qs(url.query)
            g = self._game(qs.get("file", [""])[0])
            name = qs.get("name", [""])[0]
            f = next((x for x in list_shots(g["id"]) if x.name == name), None) if g else None
            return self.send_file(f) if f else self.send_json(404, {"error": "Not found"})
        elif url.path == "/api/profiles":
            kind = urllib.parse.parse_qs(url.query).get("kind", ["GCPad"])[0]
            if kind not in ("GCPad", "Wiimote"):
                return self.send_json(400, {"error": "Bad kind"})
            self.send_json(200, {"profiles": list_profiles(kind)})
        elif self.path == "/api/games":
            self.send_json(200, {"games": scan(), "folder": GAMES_DIR.name})
        else:
            self.send_json(404, {"error": "Not found"})

    def _game(self, name):
        return next((g for g in scan() if g["file"] == name), None)

    def do_POST(self):
        if self.path not in ("/api/launch", "/api/profile", "/api/profile/new"):
            return self.send_json(404, {"error": "Not found"})
        origin = self.headers.get("Origin")
        if origin not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send_json(403, {"error": "Requests only work from the shelf page."})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self.send_json(400, {"error": "Bad request."})
        name = data.get("file", "")
        game = self._game(name)
        if not game:
            return self.send_json(404, {"error": "That game isn't in the folder anymore. Press R to refresh."})
        if self.path == "/api/profile/new":
            pname = re.sub(r"\s+", " ", str(data.get("name") or "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9 _!'&()+.-]{1,40}", pname) or pname.startswith("."):
                return self.send_json(400, {"error": "Use a short name with letters, numbers or spaces."})
            if game["system"] == "Wii":
                return self.send_json(400, {"error": "Wii Remote layouts need to be set up in Dolphin's controller settings."})
            if not game["id"]:
                return self.send_json(400, {"error": "This game has no ID, so it can't have its own controls."})
            try:
                create_profile(pname, data.get("preset", "standard"))
                set_game_profile(game["id"], "PadProfile1", pname)
            except ValueError as e:
                return self.send_json(400, {"error": str(e)})
            except OSError as e:
                log_error(f"Couldn't create profile: {e}")
                return self.send_json(500, {"error": "Couldn't save that layout."})
            return self.send_json(200, {"ok": True, "profile": pname})
        if self.path == "/api/profile":
            prof = (data.get("profile") or "").strip()
            kind, key = profile_kind(game["system"])
            if prof and prof not in list_profiles(kind):
                return self.send_json(400, {"error": "That controller profile wasn't found."})
            if not game["id"]:
                return self.send_json(400, {"error": "This game has no ID, so it can't have its own controls."})
            try:
                set_game_profile(game["id"], key, prof or None)
            except OSError as e:
                log_error(f"Couldn't save controller profile: {e}")
                return self.send_json(500, {"error": "Couldn't save the controller setting."})
            return self.send_json(200, {"ok": True, "profile": prof or None})
        state = latest_state(game["id"]) if data.get("resume") else None
        if data.get("resume") and not state:
            return self.send_json(404, {"error": "There's no save state for this game yet."})
        if game.get("emu", "dolphin") == "dolphin":
            error = launch(GAMES_DIR / name, state)
        else:
            error = launch_other(GAMES_DIR / name, game["system"])
        if error:
            return self.send_json(500, {"error": error})
        global LAST_SEEN
        LAST_SEEN = time.time() + 3600  # give long play sessions a buffer
        with _lock:
            state = load_state()
            entry = state.setdefault(name, {})
            entry["lastPlayed"] = time.time()
            entry["plays"] = entry.get("plays", 0) + 1
            save_state(state)
        self.send_json(200, {"ok": True, **entry})


def open_window(url):
    chrome = Path("/Applications/Google Chrome.app")
    if chrome.exists():
        # App mode gives a clean window with no tabs or address bar.
        subprocess.Popen(["open", "-na", str(chrome), "--args", f"--app={url}", "--window-size=1280,820"])
    else:
        webbrowser.open(url)


def main():
    global PORT
    server = None
    for port in range(8642, 8662):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            PORT = port
            break
        except OSError:
            continue
    if server is None:
        print("Couldn't find a free port between 8642 and 8661.")
        sys.exit(1)
    url = f"http://127.0.0.1:{PORT}/"
    install_rpcs3_firmware_once()
    for sysname, info in SYSTEMS.items():
        if info["emu"] == "dolphin":
            continue
        if not any((GAMES_DIR / f).is_dir() for f in info["folders"]):
            try:
                (GAMES_DIR / info["folders"][0]).mkdir(exist_ok=True)
            except OSError:
                pass
    for d in (HERE / "covers", BG_DIR, DISC_DIR):
        d.mkdir(exist_ok=True)
    print(f"Game Shelf is running at {url}", flush=True)
    print("Leave this window open while you play. Close it to stop the shelf.")
    if "--no-open" not in sys.argv:
        threading.Timer(0.4, open_window, args=(url,)).start()
    def watchdog():
        while True:
            time.sleep(30)
            if time.time() - LAST_SEEN > IDLE_LIMIT:
                print("No shelf window open for a while. Shutting down.")
                server.shutdown()
                return
    if "--no-idle" not in sys.argv:
        threading.Thread(target=watchdog, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
