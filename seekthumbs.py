#!/usr/bin/env python3
"""seekthumbs: builds small seek-bar preview thumbnails ("trickplay") for a film library, stores them in a local folder, and keeps up as films are added.

Built for a READ-ONLY rclone mount with API/bandwidth limits: the library is only read (never written), everything goes to a separate local store,
films are processed one at a time, slowly, in the background, and only when no player is running. Standard library plus ffmpeg/ffprobe.

    seekthumbs.py probe FILE        try 3 thumbnails from one film, report timing; writes nothing. Run this first to tune --pause and --interval.
    seekthumbs.py plan              list films that still need thumbnails (reads directory listings only, no film is opened)
    seekthumbs.py scan              one pass: build thumbnails for films that lack them (at most --max-films per pass)
    seekthumbs.py watch             scan, sleep --poll seconds, repeat (this is what the systemd service runs)
    seekthumbs.py status            store size, entry count, the largest entries against the size budget
    seekthumbs.py verify            check every store entry against its index and the size budget
    seekthumbs.py prune [--yes]     list (or delete, with --yes) store entries whose film no longer exists; refuses if the library looks unmounted
    seekthumbs.py install-service [--enable]   write ~/.config/systemd/user/seekthumbs.service

Settings come from ~/.config/seekthumbs/config.ini ([seekthumbs] library=, store=, ...) and can be overridden on the command line.

Store layout (mirrors the library, so a player script can find a film's entry by path alone):
    STORE/<path of the film relative to LIBRARY>.seekthumbs/index.json   {v, src, size, duration, interval, w, h, count, offsets[], lengths[], q, made}
    STORE/<same>.seekthumbs/thumbs.pack                                  the JPEGs one after another; thumbnail i is at offsets[i], lengths[i] bytes
Thumbnail i shows the film at about (i + 0.5) * interval seconds. A length of 0 means that one could not be read (use the previous one).
Size budget: at most budget_mb per 2 hours of film (default 10), aimed at 80 % of it; quality and then width are lowered until it fits.
"""
import argparse
import configparser
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".wmv"}
DEFAULTS = dict(library="", store="", interval=20.0, width=240, pause=1.5, film_pause=60.0, poll=1800.0, settle=600.0, min_size_mb=300.0,
                max_films=3, budget_mb=10.0, safety=0.8, seek_timeout=90.0, probesize="1M", yield_to_playback=1, breaker_hours=1.0)
PLAYERS = ("mpv", "mpv-atmos", "mpv-object-audio", "vlc", "kodi", "kodi.bin")
SUFFIX = ".seekthumbs"
STOP = False


def log(*a):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def on_signal(signum, frame):
    global STOP
    STOP = True


def sleep_interruptible(seconds):
    end = time.time() + seconds
    while not STOP and time.time() < end:
        time.sleep(min(1.0, end - time.time()))


# ---------------------------------------------------------------- configuration

def load_config(args):
    cfg = dict(DEFAULTS)
    ini = os.path.expanduser(getattr(args, "config", None) or "~/.config/seekthumbs/config.ini")
    cp = configparser.ConfigParser()
    if os.path.exists(ini):
        cp.read(ini)
        for k, v in cp["seekthumbs"].items() if cp.has_section("seekthumbs") else []:
            if k in cfg:
                cfg[k] = type(DEFAULTS[k])(v) if not isinstance(DEFAULTS[k], str) else v
    for k in DEFAULTS:
        v = getattr(args, k, None)
        if v is not None:
            cfg[k] = v
    cfg["library"] = os.path.expanduser(cfg["library"]) if cfg["library"] else ""
    cfg["store"] = os.path.expanduser(cfg["store"]) if cfg["store"] else ""
    return cfg


def need_paths(cfg, library=True):
    if library and not cfg["library"]:
        sys.exit("no library path: set library= in ~/.config/seekthumbs/config.ini or pass --library")
    if not cfg["store"]:
        sys.exit("no store path: set store= in ~/.config/seekthumbs/config.ini or pass --store")


# ---------------------------------------------------------------- library and store

def walk_library(root, min_size):
    """Yields (relpath, abspath, size, mtime) for video files. Directory listings only; no file is opened."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in sorted(it, key=lambda x: x.name):
                    if e.name.startswith("."):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file() and os.path.splitext(e.name)[1].lower() in VIDEO_EXT:
                            st = e.stat()
                            if st.st_size >= min_size:
                                yield os.path.relpath(e.path, root), e.path, st.st_size, st.st_mtime
                    except OSError as ex:
                        log("skip", e.path, ex)
        except OSError as ex:
            log("cannot list", d, ex)


def entry_dir(store, rel):
    return os.path.join(store, rel + SUFFIX)


def read_index(edir):
    try:
        with open(os.path.join(edir, "index.json")) as f:
            ix = json.load(f)
        pack = os.path.join(edir, "thumbs.pack")
        if ix.get("v") == 1 and os.path.getsize(pack) == sum(ix["lengths"]):
            return ix
    except (OSError, ValueError, KeyError):
        pass
    return None


def entry_current(store, rel, size):
    ix = read_index(entry_dir(store, rel))
    return ix is not None and ix.get("size") == size


def store_entries(store):
    for dp, dn, fn in os.walk(store):
        if os.path.basename(dp).endswith(SUFFIX) and "index.json" in fn:
            dn[:] = []
            yield dp


class State:
    """Failures and the circuit breaker, kept in STORE/.state.json."""

    def __init__(self, store):
        self.path = os.path.join(store, ".state.json")
        try:
            with open(self.path) as f:
                self.d = json.load(f)
        except (OSError, ValueError):
            self.d = {}
        self.d.setdefault("fail", {})
        self.d.setdefault("breaker_until", 0)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f, indent=1)
        os.replace(tmp, self.path)

    def blocked(self, rel, size):
        f = self.d["fail"].get(rel)
        return bool(f and f.get("size") == size and f.get("next", 0) > time.time())

    def failed(self, rel, size, why):
        n = self.d["fail"].get(rel, {}).get("count", 0) + 1
        self.d["fail"][rel] = dict(size=size, count=n, why=why[:200], next=time.time() + min(6 * 3600 * 2 ** (n - 1), 7 * 86400))
        self.save()

    def ok(self, rel):
        if self.d["fail"].pop(rel, None) is not None:
            self.save()


# ---------------------------------------------------------------- ffmpeg

def low_priority():
    pre = ["nice", "-n", "19"]
    if shutil.which("ionice"):
        pre = ["ionice", "-c3"] + pre
    return pre


def have_filter(name):
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=20).stdout
        return f" {name} " in out
    except (OSError, subprocess.SubprocessError):
        return False


def ffprobe(path, cfg):
    cmd = ["ffprobe", "-v", "error", "-probesize", str(cfg["probesize"]), "-select_streams", "v:0",
           "-show_entries", "stream=codec_name,width,height,color_transfer:format=duration", "-of", "json", path]
    r = subprocess.run(low_priority() + cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("ffprobe: " + (r.stderr.strip() or "failed")[:200])
    j = json.loads(r.stdout or "{}")
    st = (j.get("streams") or [{}])[0]
    dur = float(j.get("format", {}).get("duration") or 0)
    if dur <= 0 or not st.get("width"):
        raise RuntimeError("no video stream or no duration (file incomplete?)")
    return dict(duration=dur, width=int(st["width"]), height=int(st["height"]), transfer=st.get("color_transfer") or "")


def vf_chain(info, width, zscale_ok):
    scale = f"scale={width}:-2:flags=bilinear"
    if info["transfer"] in ("smpte2084", "arib-std-b67") and zscale_ok:
        # HDR source: tone-map to SDR so the thumbnails are not grey and flat (not verified against a real HDR file yet)
        return ("zscale=t=linear:npl=100,format=gbrpf32le,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv:p=bt709,format=yuv420p,"
                + scale)
    return scale


def grab(src, t, out_png, vf, cfg):
    """One frame near time t (keyframe only: no decoding forward, so as little of the file as possible is read)."""
    cmd = low_priority() + ["ffmpeg", "-nostdin", "-v", "error", "-y", "-probesize", str(cfg["probesize"]), "-analyzeduration", "0",
                            "-noaccurate_seek", "-ss", f"{t:.2f}", "-i", src, "-map", "0:v:0", "-frames:v", "1", "-an", "-sn", "-dn",
                            "-vf", vf, out_png]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg["seek_timeout"])
    except subprocess.TimeoutExpired:
        return False, "timeout"
    ok = r.returncode == 0 and os.path.exists(out_png) and os.path.getsize(out_png) > 0
    return ok, "" if ok else (r.stderr.strip().splitlines() or ["no output"])[-1]


def jpeg(png, out, width, q):
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", png, "-vf", f"scale={width}:-2:flags=bicubic", "-q:v", str(q), "-huffman", "optimal",
           "-pix_fmt", "yuvj420p", out]
    return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0 and os.path.exists(out)


def choose_and_encode(frames, outdir, width0, budget_bytes):
    """frames: {index: png path}. Lowers quality, then width, until the total fits the budget. Returns (sizes {index: bytes}, width, q)."""
    keys = sorted(frames)
    sample = keys[:: max(1, len(keys) // 12)] or keys
    ladder = [(width0, q) for q in (5, 8, 12, 16, 22, 28)]
    for f in (0.75, 0.5):
        ladder += [(max(64, int(width0 * f)), q) for q in (8, 14, 22, 28)]
    chosen = ladder[-1]
    for w, q in ladder:
        tot = 0
        for k in sample:
            tmp = os.path.join(outdir, "_probe.jpg")
            if jpeg(frames[k], tmp, w, q):
                tot += os.path.getsize(tmp)
        if os.path.exists(os.path.join(outdir, "_probe.jpg")):
            os.remove(os.path.join(outdir, "_probe.jpg"))
        if tot * len(keys) / max(1, len(sample)) <= budget_bytes:
            chosen = (w, q)
            break
    idx = ladder.index(chosen)
    while True:
        w, q = ladder[idx]
        sizes = {}
        for k in keys:
            out = os.path.join(outdir, f"{k:06d}.jpg")
            if jpeg(frames[k], out, w, q):
                sizes[k] = os.path.getsize(out)
        if sum(sizes.values()) <= budget_bytes or idx == len(ladder) - 1:
            return sizes, w, q
        idx += 1


def build_entry(rel, src, size, cfg, state, zscale_ok):
    """Returns True on success. All writes go to the local store; the source is only read."""
    t0 = time.time()
    info = ffprobe(src, cfg)
    n = max(1, int(info["duration"] // cfg["interval"]))
    allowed = cfg["budget_mb"] * 1e6 * (info["duration"] / 7200.0) * cfg["safety"]
    vf = vf_chain(info, cfg["width"], zscale_ok)
    final = entry_dir(cfg["store"], rel)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    work = tempfile.mkdtemp(prefix=".work-", dir=os.path.dirname(final))
    try:
        frames, streak, fails = {}, 0, 0
        for i in range(n):
            if STOP:
                raise KeyboardInterrupt
            t = min((i + 0.5) * cfg["interval"], max(0.0, info["duration"] - 1.0))
            png = os.path.join(work, f"{i:06d}.png")
            ok, why = grab(src, t, png, vf, cfg)
            if not ok:
                time.sleep(cfg["pause"] * 2)
                ok, why = grab(src, t, png, vf, cfg)
            if ok:
                frames[i] = png
                streak = 0
            else:
                fails += 1
                streak += 1
                if streak >= 3:
                    raise RuntimeError(f"3 failed reads in a row at {t:.0f}s: {why}")
            time.sleep(cfg["pause"])
        if len(frames) < max(1, int(n * 0.8)):
            raise RuntimeError(f"only {len(frames)} of {n} thumbnails could be read")
        enc = os.path.join(work, "jpg")
        os.makedirs(enc)
        sizes, w, q = choose_and_encode(frames, enc, cfg["width"], allowed)
        if not sizes:
            raise RuntimeError("encoding failed")
        offsets, lengths, pos = [], [], 0
        with open(os.path.join(work, "thumbs.pack"), "wb") as pk:
            for i in range(n):
                if i in sizes:
                    with open(os.path.join(enc, f"{i:06d}.jpg"), "rb") as f:
                        b = f.read()
                    pk.write(b)
                    offsets.append(pos)
                    lengths.append(len(b))
                    pos += len(b)
                else:
                    offsets.append(pos)
                    lengths.append(0)
        h = int(round(w * info["height"] / info["width"] / 2) * 2)
        ix = dict(v=1, src=rel, size=size, duration=round(info["duration"], 2), interval=cfg["interval"], w=w, h=h, count=n,
                  offsets=offsets, lengths=lengths, q=q, made=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with open(os.path.join(work, "index.json"), "w") as f:
            json.dump(ix, f, separators=(",", ":"))
        for junk in ("jpg",):
            shutil.rmtree(os.path.join(work, junk), ignore_errors=True)
        for fn in os.listdir(work):
            if fn.endswith(".png"):
                os.remove(os.path.join(work, fn))
        total = pos + os.path.getsize(os.path.join(work, "index.json"))
        if os.path.isdir(final):
            shutil.rmtree(final)
        os.rename(work, final)
        log(f"built {rel}: {n} thumbs ({len(frames)} read, {fails} failed reads), {w}x{h} q{q}, {total / 1e6:.2f} MB "
            f"(budget {allowed / 1e6:.2f}), {time.time() - t0:.0f}s")
        return True
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- passes

def player_running():
    try:
        r = subprocess.run(["pgrep", "-x", "|".join(PLAYERS)] if False else ["pgrep", "-x", PLAYERS[0]], capture_output=True)
        if r.returncode == 0:
            return True
        for p in PLAYERS[1:]:
            if subprocess.run(["pgrep", "-x", p], capture_output=True).returncode == 0:
                return True
    except OSError:
        pass
    return False


def library_ready(cfg):
    try:
        return len(os.listdir(cfg["library"])) > 0
    except OSError:
        return False


def scan_pass(cfg, dry=False):
    need_paths(cfg)
    os.makedirs(cfg["store"], exist_ok=True)
    lock = open(os.path.join(cfg["store"], ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another seekthumbs is running; skipping")
        return 0
    if not library_ready(cfg):
        log("library folder is missing or empty (mount down?); nothing done")
        return 0
    state = State(cfg["store"])
    if not dry and state.d["breaker_until"] > time.time():
        log(f"circuit breaker open for another {(state.d['breaker_until'] - time.time()) / 60:.0f} min after repeated read failures")
        return 0
    todo = []
    for rel, src, size, mtime in walk_library(cfg["library"], cfg["min_size_mb"] * 1e6):
        if entry_current(cfg["store"], rel, size) or state.blocked(rel, size):
            continue
        if time.time() - mtime < cfg["settle"]:
            continue
        todo.append((mtime, rel, src, size))
    todo.sort(reverse=True)          # newest additions first
    if dry:
        for mtime, rel, src, size in todo:
            print(f"{size / 1e9:6.1f} GB  {rel}")
        log(f"{len(todo)} film(s) need thumbnails")
        return len(todo)
    zs = have_filter("zscale")
    done = 0
    for mtime, rel, src, size in todo[: int(cfg["max_films"])]:
        if STOP:
            break
        while cfg["yield_to_playback"] and player_running() and not STOP:
            log("a player is running; waiting 5 min")
            sleep_interruptible(300)
        try:
            if build_entry(rel, src, size, cfg, state, zs):
                state.ok(rel)
                done += 1
        except KeyboardInterrupt:
            break
        except Exception as ex:                # noqa: BLE001 - one bad film must never stop the loop
            log(f"FAILED {rel}: {ex}")
            state.failed(rel, size, str(ex))
            if "in a row" in str(ex):
                state.d["breaker_until"] = time.time() + cfg["breaker_hours"] * 3600
                state.save()
                log("repeated read failures: pausing all work for", cfg["breaker_hours"], "h (rate limit or mount trouble?)")
                break
        sleep_interruptible(cfg["film_pause"])
    left = max(0, len(todo) - done)
    log(f"pass finished: {done} built, {left} still waiting")
    return done


def cmd_watch(cfg):
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log("watching", cfg["library"], "every", cfg["poll"], "s; store", cfg["store"])
    while not STOP:
        try:
            scan_pass(cfg)
        except Exception as ex:                # noqa: BLE001
            log("pass failed:", ex)
        sleep_interruptible(cfg["poll"])
    log("stopped")


def cmd_probe(cfg, path):
    info = ffprobe(path, cfg)
    zs = have_filter("zscale")
    vf = vf_chain(info, cfg["width"], zs)
    print(f"{path}: {info['duration'] / 60:.0f} min, {info['width']}x{info['height']}, transfer {info['transfer'] or '?'}")
    tmp = tempfile.mkdtemp(prefix="seekthumbs-probe-")
    try:
        for frac in (0.1, 0.5, 0.9):
            t0 = time.time()
            ok, why = grab(path, info["duration"] * frac, os.path.join(tmp, "p.png"), vf, cfg)
            dt = time.time() - t0
            print(f"  seek to {frac * 100:.0f}%: {'ok' if ok else 'FAILED ' + why}, {dt:.1f} s" + (f", {os.path.getsize(os.path.join(tmp, 'p.png'))} bytes" if ok else ""))
            time.sleep(cfg["pause"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n = int(info["duration"] // cfg["interval"])
    print(f"a full pass at interval {cfg['interval']:.0f} s means {n} seeks; at pause {cfg['pause']} s that is at least {n * cfg['pause'] / 60:.0f} min plus the seek times above")


def cmd_status(cfg):
    need_paths(cfg, library=False)
    n = tot = 0
    worst = []
    for edir in store_entries(cfg["store"]):
        ix = read_index(edir)
        sz = sum(os.path.getsize(os.path.join(edir, f)) for f in os.listdir(edir))
        n += 1
        tot += sz
        if ix:
            worst.append((sz / max(1.0, ix["duration"] / 7200.0) / 1e6, sz, os.path.relpath(edir, cfg["store"])))
    print(f"{n} entries, {tot / 1e6:.1f} MB in {cfg['store']}")
    for per2h, sz, rel in sorted(worst, reverse=True)[:5]:
        print(f"  {per2h:5.2f} MB per 2 h  ({sz / 1e6:.2f} MB)  {rel}")
    st = State(cfg["store"])
    print(f"{len(st.d['fail'])} film(s) with recorded failures" + (f"; circuit breaker open {(st.d['breaker_until'] - time.time()) / 60:.0f} more min"
                                                               if st.d["breaker_until"] > time.time() else ""))
    for rel, f in list(st.d["fail"].items())[:5]:
        print(f"  {rel}: {f['why']} (x{f['count']})")


def cmd_verify(cfg):
    need_paths(cfg, library=False)
    bad = n = 0
    for edir in store_entries(cfg["store"]):
        n += 1
        ix = read_index(edir)
        if ix is None:
            print("BAD index/pack:", edir)
            bad += 1
            continue
        sz = os.path.getsize(os.path.join(edir, "thumbs.pack")) + os.path.getsize(os.path.join(edir, "index.json"))
        per2h = sz / max(1.0, ix["duration"] / 7200.0) / 1e6
        if per2h > cfg["budget_mb"]:
            print(f"OVER BUDGET {per2h:.2f} MB per 2 h: {edir}")
            bad += 1
        with open(os.path.join(edir, "thumbs.pack"), "rb") as f:
            for off, ln in list(zip(ix["offsets"], ix["lengths"]))[:3]:
                if ln:
                    f.seek(off)
                    if f.read(2) != b"\xff\xd8":
                        print("BAD JPEG start:", edir)
                        bad += 1
                        break
    print(f"{n} entries checked, {bad} problem(s)")
    return 1 if bad else 0


def cmd_prune(cfg, yes):
    need_paths(cfg)
    if not library_ready(cfg):
        sys.exit("library folder is missing or empty: refusing to prune (a missing mount would look like every film was deleted)")
    have = {rel for rel, _, _, _ in walk_library(cfg["library"], 0)}
    gone = [e for e in store_entries(cfg["store"]) if os.path.relpath(e, cfg["store"])[: -len(SUFFIX)] not in have]
    if len(have) == 0:
        sys.exit("no videos found in the library: refusing to prune")
    for e in gone:
        print(("deleting " if yes else "would delete ") + e)
        if yes:
            shutil.rmtree(e, ignore_errors=True)
    print(f"{len(gone)} orphaned entr{'y' if len(gone) == 1 else 'ies'}" + ("" if yes else " (use --yes to delete)"))


def cmd_install_service(cfg, enable):
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(unit_dir, exist_ok=True)
    exe = os.path.abspath(__file__)
    with open(os.path.join(unit_dir, "seekthumbs.service"), "w") as f:
        f.write(f"""[Unit]
Description=Seek-bar thumbnails for the film library (slow, low priority)
After=network-online.target

[Service]
ExecStart={sys.executable} {exe} watch
Restart=on-failure
RestartSec=60
Nice=19
IOSchedulingClass=idle
CPUQuota=50%

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    print("wrote", os.path.join(unit_dir, "seekthumbs.service"))
    if enable:
        subprocess.run(["systemctl", "--user", "enable", "--now", "seekthumbs.service"])
    else:
        print("start it with: systemctl --user enable --now seekthumbs.service   (after setting library= and store= in ~/.config/seekthumbs/config.ini)")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--library")
    ap.add_argument("--store")
    for k, v in DEFAULTS.items():
        if k not in ("library", "store"):
            ap.add_argument("--" + k.replace("_", "-"), dest=k, type=type(v) if not isinstance(v, str) else str, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("file")
    for c in ("plan", "scan", "watch", "status", "verify"):
        sub.add_parser(c)
    p = sub.add_parser("prune")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("install-service")
    p.add_argument("--enable", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args)
    if args.cmd == "probe":
        cmd_probe(cfg, args.file)
    elif args.cmd == "plan":
        scan_pass(cfg, dry=True)
    elif args.cmd == "scan":
        scan_pass(cfg)
    elif args.cmd == "watch":
        need_paths(cfg)
        cmd_watch(cfg)
    elif args.cmd == "status":
        cmd_status(cfg)
    elif args.cmd == "verify":
        return cmd_verify(cfg)
    elif args.cmd == "prune":
        cmd_prune(cfg, args.yes)
    elif args.cmd == "install-service":
        cmd_install_service(cfg, args.enable)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
