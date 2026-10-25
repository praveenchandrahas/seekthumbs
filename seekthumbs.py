#!/usr/bin/env python3
"""seekthumbs: builds small seek-bar preview thumbnails ("trickplay") for a film library, stores them in a local folder, and keeps up as films are added.

Built for a READ-ONLY rclone mount with API/bandwidth limits: the library is only read (never written), everything goes to a separate local store.
Fast by default and slowed down only when the mount pushes back: several reads run in parallel, and a governor halves the parallelism and waits
(30 s, doubling to 15 min) as soon as a read fails with a rate-limit signal (HTTP 429/403/503 text, "too many requests", a timeout), then
grows back one step per 40 clean reads. Standard library plus ffmpeg/ffprobe; PyAV (python3-av) is used when present, to open each film once.

    seekthumbs.py probe FILE        time a few seeks with each extractor and the bytes the machine received; writes nothing. Run this first.
    seekthumbs.py plan              list films that still need thumbnails (reads directory listings only, no film is opened)
    seekthumbs.py scan              one pass: build thumbnails for every film that lacks them (--max-films N to limit)
    seekthumbs.py watch             scan, sleep --poll seconds, repeat (this is what the systemd service runs)
    seekthumbs.py status            store size, entry count, the largest entries against the size budget
    seekthumbs.py verify            check every store entry against its index and the size budget
    seekthumbs.py prune [--yes]     list (or delete, with --yes) store entries whose film no longer exists; refuses if the library looks unmounted
    seekthumbs.py install-service [--enable]   write ~/.config/systemd/user/seekthumbs.service
    seekthumbs.py install-viewer [--mpv-config-dir DIR]   put the mpv viewer (thumbfast.lua) into DIR/scripts, moving a real thumbfast aside

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
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".wmv"}
DEFAULTS = dict(library="", store="", interval=20.0, width=240, workers=6, pause=0.0, film_pause=0.0, poll=1800.0, settle=600.0,
                min_size_mb=300.0, max_films=0, budget_mb=10.0, safety=0.8, seek_timeout=90.0, probesize="1M", yield_to_playback=1,
                breaker_hours=1.0, extractor="auto", rate_cooldown=30.0)
PLAYERS = ("mpv", "mpv-atmos", "mpv-object-audio", "vlc", "kodi", "kodi.bin")
SUFFIX = ".seekthumbs"
STOP = False
# Words that mean "the server is pushing back" rather than "this part of the file is bad".
THROTTLE_RE = re.compile(r"429|too many request|rate.?limit|quota|slow.?down|service unavailable|\b503\b|\b403\b|forbidden|temporar|timed? ?out|timeout",
                         re.I)


class Throttled(Exception):
    """The mount kept saying 'slow down' even at the lowest concurrency and the longest cool-down."""


class ReadFailures(Exception):
    """Reads of one film kept failing for reasons other than rate limiting."""


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


# ---------------------------------------------------------------- the governor: as fast as the mount allows

class Governor:
    """Limits how many reads run at once. Starts at the maximum. On a rate-limit signal it halves the limit and stops all new reads for a
    cool-down (30 s, doubling to 15 min); after 40 clean reads it raises the limit by one again. Only the first signal of a burst acts."""

    def __init__(self, maximum, cooldown=30.0):
        self.max = max(1, int(maximum))
        self.limit = self.max
        self.active = 0
        self.until = 0.0
        self.base = float(cooldown)
        self.cool = float(cooldown)
        self.streak = 0
        self.throttles = 0
        self.consec = 0
        self.floor_bursts = 0
        self.cv = threading.Condition()

    def acquire(self):
        with self.cv:
            while not STOP:
                now = time.time()
                if self.active < self.limit and now >= self.until:
                    self.active += 1
                    return True
                self.cv.wait(timeout=max(0.05, min(1.0, self.until - now)) if self.until > now else 1.0)
        return False

    def release(self, outcome):
        """outcome: 'ok', 'throttled' or 'fail'."""
        with self.cv:
            self.active -= 1
            if outcome == "throttled":
                self.throttles += 1
                self.consec += 1
                self.streak = 0
                if time.time() >= self.until:
                    if self.limit == 1:
                        self.floor_bursts += 1
                    self.limit = max(1, self.limit // 2)
                    self.until = time.time() + self.cool
                    log(f"rate-limit signal: reads in parallel now {self.limit}, no new reads for {self.cool:.0f} s")
                    self.cool = min(self.cool * 2, 900.0)
            elif outcome == "ok":
                self.consec = 0
                self.floor_bursts = 0
                self.streak += 1
                if self.streak >= 40 and self.limit < self.max:
                    self.limit += 1
                    self.streak = 0
                    if self.limit == self.max:
                        self.cool = self.base
                    log(f"clean reads: parallel reads back to {self.limit}")
            self.cv.notify_all()

    def hopeless(self):
        """True after 8 rate-limit bursts in a row at one read at a time (about an hour of backing off with the defaults)."""
        return self.floor_bursts >= 8


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


def is_hdr(info):
    return info["transfer"] in ("smpte2084", "arib-std-b67")


def vf_chain(info, width, zscale_ok):
    scale = f"scale={width}:-2:flags=bilinear"
    if is_hdr(info) and zscale_ok:
        # HDR source: tone-map to SDR so the thumbnails are not grey and flat (not verified against a real HDR file yet)
        # Scaled down FIRST: tone-mapping a 4K frame in float costs hundreds of ms, a 240 px one almost nothing. (Averaging pixels while still
        # PQ-encoded is not colour-exact, which does not matter at thumbnail size.)
        return (scale + ",zscale=t=linear:npl=100,format=gbrpf32le,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv:p=bt709,format=yuv420p")
    return scale


class CliExtractor:
    """One ffmpeg process per thumbnail: simple and robust, but every thumbnail re-opens the film (more round trips on a network mount)."""
    name = "ffmpeg"

    def __init__(self, src, vf, cfg):
        self.src, self.vf, self.cfg = src, vf, cfg

    def grab(self, t, out):
        cmd = low_priority() + ["ffmpeg", "-nostdin", "-v", "error", "-y", "-probesize", str(self.cfg["probesize"]), "-analyzeduration", "0",
                                "-noaccurate_seek", "-ss", f"{t:.2f}", "-i", self.src, "-map", "0:v:0", "-frames:v", "1", "-an", "-sn", "-dn",
                                "-vf", self.vf, out]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.cfg["seek_timeout"])
        except subprocess.TimeoutExpired:
            return False, "timeout"
        ok = r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0
        return ok, "" if ok else (r.stderr.strip()[-300:] or "no output")

    def close(self):
        pass


def parse_vf(vf):
    chain = []
    for part in vf.split(","):
        name, _, args = part.partition("=")
        chain.append((name, args or None))
    return chain


def write_ppm(path, w, h, rows):
    with open(path, "wb") as f:
        f.write(b"P6\n%d %d\n255\n" % (w, h))
        for r in rows:
            f.write(r)


class AvExtractor:
    """PyAV: each worker thread opens the film ONCE and then only seeks. Skips the repeated open, probe and index reads of one process per
    thumbnail. Needs PyAV; HDR needs a PyAV linked to an ffmpeg that has zscale (the apt package python3-av on Ubuntu, not the pip wheel)."""
    name = "pyav"

    @staticmethod
    def usable(vf):
        try:
            import av  # noqa: F401
        except ImportError:
            return False, "PyAV is not installed"
        try:
            AvExtractor._graph_for(vf, probe=True)
        except Exception as ex:                # noqa: BLE001
            return False, f"PyAV cannot build the filter chain ({ex})"
        return True, ""

    @staticmethod
    def _graph_for(vf, stream=None, probe=False):
        import av
        g = av.filter.Graph()
        if probe:
            for name, args in parse_vf(vf) + [("format", "rgb24")]:
                g.add(name, args) if args else g.add(name)
            return None
        buf = g.add_buffer(template=stream)
        prev = buf
        for name, args in parse_vf(vf) + [("format", "rgb24")]:
            f = g.add(name, args) if args else g.add(name)
            prev.link_to(f)
            prev = f
        sink = g.add("buffersink")
        prev.link_to(sink)
        g.configure()
        return g, buf, sink

    def __init__(self, src, vf, cfg):
        self.src, self.vf, self.cfg = src, vf, cfg
        self.local = threading.local()
        self.opened = []
        self.lock = threading.Lock()

    def _state(self):
        st = getattr(self.local, "st", None)
        if st is None:
            import av
            c = av.open(self.src, options={"probesize": str(self.cfg["probesize"])}, timeout=float(self.cfg["seek_timeout"]))
            stream = c.streams.video[0]
            try:
                stream.codec_context.skip_frame = "NONKEY"
            except Exception:                  # noqa: BLE001 - older PyAV: decode everything it is given, still correct
                pass
            g, buf, sink = self._graph_for(self.vf, stream)
            st = (c, stream, g, buf, sink)
            self.local.st = st
            with self.lock:
                self.opened.append(c)
        return st

    def _drop(self):
        st = getattr(self.local, "st", None)
        self.local.st = None
        if st:
            try:
                st[0].close()
            except Exception:                  # noqa: BLE001
                pass

    def grab(self, t, out):
        try:
            c, stream, g, buf, sink = self._state()
            ts = int(t / stream.time_base) + int(stream.start_time or 0)
            c.seek(ts, stream=stream, backward=True, any_frame=False)
            frame = None
            for n, pkt in enumerate(c.demux(stream)):
                for fr in pkt.decode():
                    frame = fr
                    break
                if frame is not None or n > 300:
                    break
            if frame is None:
                return False, "no frame decoded"
            buf.push(frame)
            o = sink.pull()
            plane = o.planes[0]
            ls, w, h = plane.line_size, o.width, o.height
            data = bytes(plane)
            write_ppm(out, w, h, (data[r * ls: r * ls + w * 3] for r in range(h)))
            return True, ""
        except Exception as ex:                # noqa: BLE001
            self._drop()
            raise RuntimeError(f"{type(ex).__name__}: {ex}") from ex

    def close(self):
        with self.lock:
            for c in self.opened:
                try:
                    c.close()
                except Exception:              # noqa: BLE001
                    pass
            self.opened = []


class AutoExtractor:
    """PyAV when it works, the ffmpeg command when it does not (a PyAV that fails 3 times before its first success is switched off for the run)."""

    def __init__(self, av, cli):
        self.av, self.cli = av, cli
        self.av_off = av is None
        self.av_ok = 0
        self.av_err = 0
        self.name = "pyav" if av else "ffmpeg"

    def grab(self, t, out):
        if not self.av_off:
            try:
                r = self.av.grab(t, out)
                if r[0]:
                    self.av_ok += 1
                    return r
            except Exception as ex:            # noqa: BLE001
                why = str(ex)
                if THROTTLE_RE.search(why):
                    return False, why
                self.av_err += 1
                if self.av_err >= 3 and self.av_ok == 0:
                    self.av_off = True
                    log("PyAV does not work on this film, using the ffmpeg command instead:", why[:120])
        return self.cli.grab(t, out)

    def close(self):
        if self.av:
            self.av.close()


def make_extractor(cfg, src, info, zscale_ok):
    vf = vf_chain(info, cfg["width"], zscale_ok)
    cli = CliExtractor(src, vf, cfg)
    want = cfg["extractor"]
    if want == "ffmpeg":
        return cli
    ok, why = AvExtractor.usable(vf)
    if not ok:
        if want == "pyav":
            log("pyav requested but not usable:", why, "- using the ffmpeg command")
        return cli
    av = AvExtractor(src, vf, cfg)
    return av if want == "pyav" else AutoExtractor(av, cli)


def jpeg(frame, out, width, q):
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", frame, "-vf", f"scale={width}:-2:flags=bicubic", "-q:v", str(q), "-huffman", "optimal",
           "-pix_fmt", "yuvj420p", out]
    return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0 and os.path.exists(out)


def choose_and_encode(frames, outdir, width0, budget_bytes, jobs):
    """frames: {index: image path}. Lowers quality, then width, until the total fits the budget. Returns (sizes {index: bytes}, width, q)."""
    keys = sorted(frames)
    sample = keys[:: max(1, len(keys) // 12)] or keys
    ladder = [(width0, q) for q in (5, 8, 12, 16, 22, 28)]
    for f in (0.75, 0.5):
        ladder += [(max(64, int(width0 * f)), q) for q in (8, 14, 22, 28)]
    chosen = len(ladder) - 1
    tmp = os.path.join(outdir, "_probe.jpg")
    for n, (w, q) in enumerate(ladder):
        tot = 0
        for k in sample:
            if jpeg(frames[k], tmp, w, q):
                tot += os.path.getsize(tmp)
        if tot * len(keys) / max(1, len(sample)) <= budget_bytes:
            chosen = n
            break
    if os.path.exists(tmp):
        os.remove(tmp)
    idx = chosen
    while True:
        w, q = ladder[idx]

        def one(k, w=w, q=q):
            out = os.path.join(outdir, f"{k:06d}.jpg")
            return k, (os.path.getsize(out) if jpeg(frames[k], out, w, q) else None)

        with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
            sizes = {k: s for k, s in ex.map(one, keys) if s}
        if sum(sizes.values()) <= budget_bytes or idx == len(ladder) - 1:
            return sizes, w, q
        idx += 1


def build_entry(rel, src, size, cfg, gov, zscale_ok):
    """Returns True on success. All writes go to the local store; the source is only read."""
    t0 = time.time()
    info = ffprobe(src, cfg)
    n = max(1, int(info["duration"] // cfg["interval"]))
    allowed = cfg["budget_mb"] * 1e6 * (info["duration"] / 7200.0) * cfg["safety"]
    ext = make_extractor(cfg, src, info, zscale_ok)
    final = entry_dir(cfg["store"], rel)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    work = tempfile.mkdtemp(prefix=".work-", dir=os.path.dirname(final))
    try:
        frames, stats, lock, abort = {}, dict(ok=0, fail=0), threading.Lock(), []
        times = [min((i + 0.5) * cfg["interval"], max(0.0, info["duration"] - 1.0)) for i in range(n)]
        ext_of_frame = "ppm" if ext.name == "pyav" or (isinstance(ext, AutoExtractor) and ext.name == "pyav") else "png"

        def work_one(i):
            if abort or STOP:
                return
            for attempt in range(12):
                if abort or STOP:
                    return
                if not gov.acquire():
                    return
                if abort:
                    gov.release("neutral")
                    return
                out = os.path.join(work, f"{i:06d}.{ext_of_frame}")
                try:
                    ok, why = ext.grab(times[i], out)
                except Exception as ex:        # noqa: BLE001
                    ok, why = False, f"{type(ex).__name__}: {ex}"
                if ok and not os.path.exists(out):                       # a fallback extractor wrote the other format
                    alt = os.path.join(work, f"{i:06d}.png")
                    out = alt if os.path.exists(alt) else out
                kind = "ok" if ok else ("throttled" if THROTTLE_RE.search(why or "") else "fail")
                gov.release(kind)
                if cfg["pause"]:
                    time.sleep(cfg["pause"])
                if ok:
                    with lock:
                        frames[i] = out
                        stats["ok"] += 1
                    return
                if kind == "throttled":
                    if gov.hopeless():
                        abort.append(("throttled", why))
                        return
                    continue
                if attempt >= 1:
                    with lock:
                        stats["fail"] += 1
                        if stats["fail"] >= 6 and stats["fail"] > stats["ok"]:
                            abort.append(("fail", why))
                    return

        with ThreadPoolExecutor(max_workers=max(1, int(cfg["workers"]))) as pool:
            list(pool.map(work_one, range(n)))
        ext.close()
        if STOP:
            raise KeyboardInterrupt
        if abort:
            kind, why = abort[0]
            if kind == "throttled":
                raise Throttled(f"still rate-limited after backing off: {why[:120]}")
            raise ReadFailures(f"repeated failed reads: {why[:150]}")
        if len(frames) < max(1, int(n * 0.8)):
            raise ReadFailures(f"only {len(frames)} of {n} thumbnails could be read")
        t_read = time.time() - t0
        enc = os.path.join(work, "jpg")
        os.makedirs(enc)
        sizes, w, q = choose_and_encode(frames, enc, cfg["width"], allowed, os.cpu_count() or 2)
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
        shutil.rmtree(enc, ignore_errors=True)
        for fn in os.listdir(work):
            if fn.endswith((".png", ".ppm")):
                os.remove(os.path.join(work, fn))
        total = pos + os.path.getsize(os.path.join(work, "index.json"))
        if os.path.isdir(final):
            shutil.rmtree(final)
        os.rename(work, final)
        dt = time.time() - t0
        log(f"built {rel}: {n} thumbs ({len(frames)} read, {stats['fail']} failed reads, {ext.name}), {w}x{h} q{q}, {total / 1e6:.2f} MB "
            f"(budget {allowed / 1e6:.2f}), {dt:.0f}s total, {n / max(0.1, t_read):.1f} reads/s")
        return True
    finally:
        try:
            ext.close()
        except Exception:                      # noqa: BLE001
            pass
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- passes

def player_running():
    try:
        for p in PLAYERS:
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
    batch = todo if int(cfg["max_films"]) <= 0 else todo[: int(cfg["max_films"])]
    zs = have_filter("zscale")
    gov = Governor(cfg["workers"], cfg["rate_cooldown"])
    done = bad_streak = 0
    t_start = time.time()
    for k, (mtime, rel, src, size) in enumerate(batch, 1):
        if STOP:
            break
        while cfg["yield_to_playback"] and player_running() and not STOP:
            log("a player is running; waiting 5 min")
            sleep_interruptible(300)
        try:
            if build_entry(rel, src, size, cfg, gov, zs):
                state.ok(rel)
                done += 1
                bad_streak = 0
                per = (time.time() - t_start) / done
                log(f"progress: {done}/{len(batch)} films, {per / 60:.1f} min per film so far, about {per * (len(batch) - k) / 60:.0f} min left")
        except KeyboardInterrupt:
            break
        except Throttled as ex:
            log(f"STOPPING: {ex}")
            state.d["breaker_until"] = time.time() + cfg["breaker_hours"] * 3600
            state.save()
            log("pausing all work for", cfg["breaker_hours"], "h; the film is not marked as failed")
            break
        except Exception as ex:                # noqa: BLE001 - one bad film must never stop the loop
            log(f"FAILED {rel}: {ex}")
            state.failed(rel, size, str(ex))
            if isinstance(ex, ReadFailures):
                bad_streak += 1
                if bad_streak >= 3:
                    state.d["breaker_until"] = time.time() + cfg["breaker_hours"] * 3600
                    state.save()
                    log("three films in a row could not be read: pausing all work for", cfg["breaker_hours"], "h (mount trouble?)")
                    break
        if cfg["film_pause"]:
            sleep_interruptible(cfg["film_pause"])
    left = max(0, len(todo) - done)
    log(f"pass finished: {done} built, {left} still waiting; {gov.throttles} rate-limit signal(s), parallel reads ended at {gov.limit}/{gov.max}")
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


def net_rx_bytes():
    """Bytes received by this machine on all interfaces except lo (rough: includes everything else the machine is doing)."""
    try:
        tot = 0
        with open("/proc/net/dev") as f:
            for line in list(f)[2:]:
                name, _, rest = line.partition(":")
                if name.strip() != "lo":
                    tot += int(rest.split()[0])
        return tot
    except (OSError, ValueError, IndexError):
        return None


def cmd_probe(cfg, path):
    info = ffprobe(path, cfg)
    zs = have_filter("zscale")
    n = int(info["duration"] // cfg["interval"])
    print(f"{path}: {info['duration'] / 60:.0f} min, {info['width']}x{info['height']}, transfer {info['transfer'] or '?'}, {n} thumbnails at {cfg['interval']:.0f} s")
    print("Quiet the machine first: the byte counts below include ALL network traffic of this computer.")
    vf = vf_chain(info, cfg["width"], zs)
    tmp = tempfile.mkdtemp(prefix="seekthumbs-probe-")
    try:
        cands = [("ffmpeg command", CliExtractor(path, vf, cfg), "png")]
        ok, why = AvExtractor.usable(vf)
        if ok:
            cands.append(("PyAV (open once)", AvExtractor(path, vf, cfg), "ppm"))
        else:
            print("PyAV not used:", why, "(apt install python3-av may fix it)")
        for label, ex, suffix in cands:
            rx0, t0, okc = net_rx_bytes(), time.time(), 0
            for j, frac in enumerate((0.15, 0.3, 0.45, 0.6, 0.75, 0.9)):
                try:
                    ok_, why_ = ex.grab(info["duration"] * frac, os.path.join(tmp, f"p{j}.{suffix}"))
                except Exception as e:         # noqa: BLE001
                    ok_, why_ = False, str(e)
                okc += ok_
                if not ok_:
                    print(f"   {label}: seek at {frac * 100:.0f} % failed: {why_[:120]}")
            dt, rx1 = time.time() - t0, net_rx_bytes()
            lat = dt / 6
            mb = (rx1 - rx0) / 1e6 / 6 if rx0 is not None and rx1 is not None else None
            print(f"{label}: {okc}/6 ok, {lat:.2f} s per thumbnail" + (f", {mb:.1f} MB received per thumbnail" if mb is not None else ""))
            w = int(cfg["workers"])
            print(f"   -> one film ≈ {n * lat / w / 60:.1f} min with {w} parallel reads" + (f", {n * mb / 1000:.1f} GB read" if mb is not None else "")
                  + f"; 100 films like this ≈ {100 * n * lat / w / 3600:.1f} h")
            ex.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
Description=Seek-bar thumbnails for the film library (parallel, low priority, backs off on rate limits)
After=network-online.target

[Service]
ExecStart={sys.executable} {exe} watch
Restart=on-failure
RestartSec=60
Nice=19
IOSchedulingClass=idle
CPUQuota=300%

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    print("wrote", os.path.join(unit_dir, "seekthumbs.service"))
    if enable:
        subprocess.run(["systemctl", "--user", "enable", "--now", "seekthumbs.service"])
    else:
        print("start it with: systemctl --user enable --now seekthumbs.service   (after setting library= and store= in ~/.config/seekthumbs/config.ini)")


def cmd_install_viewer(mpv_dir):
    mpv_dir = os.path.expanduser(mpv_dir)
    sdir = os.path.join(mpv_dir, "scripts")
    os.makedirs(sdir, exist_ok=True)
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thumbfast.lua")
    dst = os.path.join(sdir, "thumbfast.lua")
    if os.path.exists(dst):
        with open(dst, errors="replace") as f:
            ours = "seekthumbs viewer" in f.read(400)
        if not ours:
            shutil.move(dst, dst + ".bak")
            print("moved the existing thumbfast.lua to thumbfast.lua.bak (two scripts cannot both be thumbfast)")
    real_dir = os.path.join(sdir, "thumbfast")
    if os.path.isdir(real_dir):
        shutil.move(real_dir, real_dir + ".bak")
        print("moved the scripts/thumbfast folder to thumbfast.bak")
    shutil.copyfile(src, dst)
    print("installed", dst)
    print("Needs uosc (or another OSC that talks to thumbfast), ffmpeg on PATH, and library=/store= in ~/.config/seekthumbs/config.ini.")
    print("Check: play a film that has a store entry; mpv's log shows 'seek thumbnails: <path>'. Undo: delete it and rename thumbfast.lua.bak back.")


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
    p = sub.add_parser("install-viewer")
    p.add_argument("--mpv-config-dir", default="~/.config/mpv-object-audio")
    args = ap.parse_args(argv)
    cfg = load_config(args)
    if args.cmd == "probe":
        cmd_probe(cfg, args.file)
    elif args.cmd == "plan":
        scan_pass(cfg, dry=True)
    elif args.cmd == "scan":
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)
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
    elif args.cmd == "install-viewer":
        cmd_install_viewer(args.mpv_config_dir)
    elif args.cmd == "install-service":
        cmd_install_service(cfg, args.enable)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
