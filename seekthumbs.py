#!/usr/bin/env python3
"""seekthumbs: builds small seek-bar preview thumbnails ("trickplay") for a film library, stores them in a local folder, and keeps up as films are added.

Built for a READ-ONLY rclone mount with API/bandwidth limits: the library is only read (never written), everything goes to a separate local store.
Fast by default and slowed down only when the mount pushes back: several reads run in parallel, and a governor halves the parallelism and waits
(30 s, doubling to 15 min) as soon as a read fails with a rate-limit signal (HTTP 429/403/503 text, "too many requests", a timeout), then
grows back one step per 40 clean reads. Standard library plus ffmpeg/ffprobe; PyAV (python3-av) is used when present, to open each film once.

    seekthumbs.py probe FILE        time a few seeks with each extractor and the bytes the machine received; writes nothing. Run this first.
    seekthumbs.py plan              list films that still need thumbnails (reads directory listings only, no film is opened)
    seekthumbs.py build FILE        build the thumbnails of ONE film now, gently (this is what the mpv viewer starts when you play a film that has none)
    seekthumbs.py scan              one pass: build thumbnails for every film that lacks them (--max-films N to limit)
    seekthumbs.py watch             scan, sleep --poll seconds, repeat (this is what the systemd service runs)
    seekthumbs.py refine [--dry-run]   fill in a finer interval for films built coarse (interval=30 first, then --interval 10): reads only what is missing
    seekthumbs.py status            store size, entry count, the largest entries against the size budget
    seekthumbs.py verify [--deep]   check every store entry against its index and the size budget; --deep also decodes every thumbnail and lists likely corrupt ones
    seekthumbs.py prune [--yes]     list (or delete, with --yes) store entries whose film no longer exists; refuses if the library looks unmounted
    seekthumbs.py install-service [--enable]   write ~/.config/systemd/user/seekthumbs.service
    seekthumbs.py install-viewer [--mpv-config-dir DIR]   put the mpv viewer (thumbfast.lua) into DIR/scripts, moving a real thumbfast aside

Settings come from ~/.config/seekthumbs/config.ini ([seekthumbs] library=, store=, ...) and can be overridden on the command line.

Store layout (mirrors the library, so a player script can find a film's entry by path alone):
    STORE/<path of the film relative to LIBRARY>.seekthumbs/index.json   {v, src, size, duration, interval, w, h, count, offsets[], lengths[], q, made}
    STORE/<same>.seekthumbs/thumbs.pack                                  the JPEGs one after another; thumbnail i is at offsets[i], lengths[i] bytes
A release that is the same film as one already built (same title and year in the file name, runtime within 2 s) gets only a small alias entry:
    STORE/<path>.seekthumbs/index.json   {v:2, alias_of:<path of the real entry>, src, size, duration, target_made, made}   (no thumbs.pack)
Thumbnail i shows the film at about (i + 0.5) * interval seconds. A length of 0 means that one could not be read (use the previous one).
Size budget: at most budget_mb per 2 hours of film (default 10), aimed at 80 % of it; quality and then width are lowered until it fits.
"""
import argparse
import configparser
import fcntl
import hashlib
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
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".wmv"}
DEFAULTS = dict(library="", store="", interval=20.0, width=240, workers=6, pause=0.0, film_pause=0.0, poll=1800.0, settle=600.0,
                min_size_mb=300.0, max_films=0, budget_mb=10.0, safety=0.8, seek_timeout=90.0, probesize="1M", yield_to_playback=1,
                breaker_hours=1.0, extractor="auto", rate_cooldown=30.0,
                play_workers=2, play_pause=2.0, worker_mem_mb=1000, read_url_base="", mem_reserve_mb=3000, play_extractor="pyav", share=1, share_tolerance=2.0, encode_jobs=0, skip_tv=0, exclude="",
                probe_timeout=300.0, wake_ahead=3, progress_every=60.0)
PLAYERS = ("mpv", "mpv-atmos", "mpv-object-audio", "vlc", "kodi", "kodi.bin")
SUFFIX = ".seekthumbs"
STOP = False
# Words that mean "the server is pushing back" rather than "this part of the file is bad".
THROTTLE_RE = re.compile(r"429|too many request|rate.?limit|quota|slow.?down|service unavailable|\b503\b|\b403\b|forbidden|temporar|timed? ?out|timeout",
                         re.I)


# Lines libav/ffmpeg print when it decoded garbage or lost data: seen on a rate-limited debrid mount, where ffmpeg still returned a frame and exit code 0.
# A read during which one of these appears is a failed read (it is retried once), whatever frame came out.
DECODE_ERR_RE = re.compile(r"invalid nal unit|error splitting the input|invalid data|corrupt|missing reference|could not find ref|error parsing|"
                           r"error while decoding|read error|invalid slice", re.I)


def decode_error_in(text):
    """The first decode-error line in some libav/ffmpeg log text, or None."""
    for line in (text or "").splitlines():
        if DECODE_ERR_RE.search(line):
            return line.strip()[:160]
    return None


def suspicious_frame(path):
    """True for a PPM frame that is near-constant and not dark: what a decoder emits when it lost its reference (mid-grey) or failed (white).
    Black or dark frames (credits, fades, a few hundred bytes as JPEG) are never flagged, nor is anything with a visible picture in it. A flagged frame is only
    read once more; if the second read gives the same, it is kept (a white-out or a grey fade is then real)."""
    if not path.endswith(".ppm"):
        return False
    try:
        with open(path, "rb") as f:
            f.readline(); f.readline(); f.readline()
            data = f.read()
    except OSError:
        return False
    if not data:
        return False
    sample = data[:: max(1, len(data) // 3000)]
    return (max(sample) - min(sample)) <= 6 and sum(sample) / len(sample) >= 90


class Throttled(Exception):
    """The mount kept saying 'slow down' even at the lowest concurrency and the longest cool-down."""


class NoPyAV(Exception):
    """The during-playback build needs PyAV and it is not usable (not installed, or its filter chain cannot be built)."""


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

TV_RE = re.compile(r"(?<![A-Za-z0-9])S\d{1,3}E\d{1,3}(?![0-9])", re.I)      # S01E02 as one word in the path: a TV episode


def excluded(cfg, rel):
    """True if the owner asked for this file to be left out: skip_tv=1 skips names with an SxxExx episode tag, exclude=REGEX skips any relative path that
    matches (case-insensitive, `|` for several). Applies to plan/scan/watch and to build FILE (so no film is built while it plays either); prune and status ignore it."""
    if cfg.get("skip_tv") and TV_RE.search(rel):
        return True
    rx = cfg.get("exclude")
    if rx:
        try:
            return re.search(rx, rel.replace(os.sep, "/"), re.I) is not None
        except re.error:
            return False
    return False


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


def read_alias(edir):
    """The alias index of an entry folder (another release of the same film), or None."""
    try:
        with open(os.path.join(edir, "index.json")) as f:
            al = json.load(f)
        if al.get("v") == 2 and isinstance(al.get("alias_of"), str) and al.get("target_made") and isinstance(al.get("size"), int):
            return al
    except (OSError, ValueError, AttributeError):
        pass
    return None


def resolve_entry(store, rel, size=None, hops=4):
    """The thumbnails to use for the film `rel`: (index, folder of the real entry, rel of the real entry) or None.
    Follows aliases (an alias points at the entry of another release of the same film). `size`, if given, must equal the size recorded for `rel` itself,
    so a replaced file never shows the old pictures. A broken link, a loop, a target that was rebuilt since (its `made` differs) or more than
    `hops` links give None."""
    seen, cur, expect = set(), rel, None
    for hop in range(hops + 1):
        if cur in seen or ".." in cur.split("/"):
            return None
        seen.add(cur)
        edir = entry_dir(store, cur)
        ix = read_index(edir)
        if ix is not None:
            if hop == 0 and size is not None and ix.get("size") != size:
                return None
            if expect is not None and ix.get("made") != expect:
                return None
            return ix, edir, cur
        al = read_alias(edir)
        if al is None:
            return None
        if hop == 0 and size is not None and al.get("size") != size:
            return None
        if expect is not None and al.get("made") != expect:
            return None
        expect, cur = al["target_made"], al["alias_of"]
    return None


def entry_current(store, rel, size):
    return resolve_entry(store, rel, size) is not None


# Words in a release name that end the title part ("Movie.Name.2019.2160p.UHD.BluRay.x265-GRP"). Not a complete list: a title that runs into an unknown tag
# before its year is simply not grouped (never wrongly grouped).
TAG_WORDS = {"bluray", "blu", "bdrip", "brrip", "bdremux", "remux", "web", "webdl", "webrip", "hdtv", "dvdrip", "dvd", "hdrip", "hdr", "hdr10", "hdr10plus",
             "dv", "dovi", "uhd", "4k", "x264", "x265", "h264", "h265", "hevc", "avc", "av1", "aac", "ac3", "dts", "truehd", "atmos", "ddp", "eac3",
             "repack", "proper", "internal", "multi", "dual", "extended", "unrated", "remastered", "imax", "directors", "theatrical", "criterion",
             "amzn", "nf", "dsnp", "hmax", "atvp"}


def release_key(name):
    """'normalised title|year' parsed from a file name, or None when it cannot be done with certainty (no year, a TV episode, a year only before a tag).
    The year is the last year-looking word before the first release tag; a year in first position belongs to the title (2001.A.Space.Odyssey.1968)."""
    base = os.path.splitext(os.path.basename(name))[0]
    s = "".join(c for c in unicodedata.normalize("NFKD", base) if not unicodedata.combining(c)).lower().replace("&", " and ")
    while True:                                # leading site tags and bracket groups: "www.UIndex.org - ", "[YTS.MX] "
        t = re.sub(r"^\s*(?:\[[^\]]*\]|\([^)]*\)|www\.[a-z0-9.-]+\.[a-z]{2,}\s*[-\u2013]?)\s*", "", s)
        if t == s:
            break
        s = t
    if re.search(r"\bs\d{1,2}[ ._-]?e\d{1,3}\b|\b\d{1,2}x\d{2,3}\b|\bseason[ ._-]?\d+|\bepisode[ ._-]?\d+", s):
        return None
    toks = [t for t in re.split(r"[^a-z0-9]+", s) if t]
    cut = len(toks)
    for i in range(1, len(toks)):
        t = toks[i]
        if t in TAG_WORDS or re.fullmatch(r"\d{3,4}[pi]", t) or re.fullmatch(r"[xh]26[45]", t):
            cut = i
            break
    year_at = None
    for i in range(1, cut):
        if re.fullmatch(r"(19|20)\d\d", toks[i]):
            year_at = i
    if year_at is None:
        return None
    return " ".join(toks[:year_at]) + "|" + toks[year_at]


def find_sibling(store, rel, duration, tol):
    """The real entry of another release of the same film (same release_key, runtime within `tol` s), or None. Reads only the local store."""
    key = release_key(rel)
    if key is None:
        return None
    best = None
    for edir in store_entries(store):
        other = os.path.relpath(edir, store)[: -len(SUFFIX)]
        if other == rel or release_key(other) != key:
            continue
        ix = read_index(edir)
        if ix is None or abs(float(ix["duration"]) - duration) > tol:
            continue
        d = abs(float(ix["duration"]) - duration)
        if best is None or d < best[0]:
            best = (d, ix, other)
    return best


def write_alias(store, rel, size, duration, target_rel, tix):
    final = entry_dir(store, rel)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    work = tempfile.mkdtemp(prefix=".work-", dir=os.path.dirname(final))
    try:
        al = dict(v=2, alias_of=target_rel, src=rel, size=size, duration=round(duration, 2), target_made=tix["made"],
                  made=datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        with open(os.path.join(work, "index.json"), "w") as f:
            json.dump(al, f, separators=(",", ":"))
        if os.path.isdir(final):
            shutil.rmtree(final)
        os.rename(work, final)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def film_lock(store, rel):
    """An exclusive lock for building one film (so two instances never build the same film). Returns the open file, or None if somebody holds it."""
    d = os.path.join(store, ".locks")
    os.makedirs(d, exist_ok=True)
    f = open(os.path.join(d, hashlib.sha1(rel.encode()).hexdigest() + ".lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def clean_stale_work(store, max_age=86400.0):
    """Removes .work-* folders left behind by a build that was killed (they hold raw frames, tens of MB per film)."""
    for dp, dn, fn in os.walk(store):
        for d in list(dn):
            if d.startswith(".work-"):
                dn.remove(d)
                full = os.path.join(dp, d)
                try:
                    if time.time() - os.path.getmtime(full) > max_age:
                        shutil.rmtree(full, ignore_errors=True)
                except OSError:
                    pass


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


class ProbeTimeout(Exception):
    """A file did not open (or probe) within probe_timeout. On a debrid mount the first touch of a cold file can take tens of seconds, so this is
    'slow', not 'broken': the scan skips the file and tries it again at the end of the pass instead of recording a failure."""


class NoMatchingRelease(Exception):
    """Alias-only build: this release is not the same film as anything already built (runtime differs by more than share_tolerance)."""


class JobStopped(Exception):
    """The owner stopped the build from the web page; the partial work is thrown away."""


class Job:
    """Live state of one build, shared with the web page: phase, counts, pause and stop. Only the web page creates one; the command line never does."""

    def __init__(self, rel, title=""):
        self.rel, self.title = rel, title
        self.phase = "starting"                      # waking, probing, reading, encoding
        self.n, self.stats = 0, dict(ok=0, fail=0, retried=0, suspect=0)
        self.t0 = time.time()
        self.t_wake = self.t_probe = 0.0
        self.t_read0 = None
        self.shared = False
        self.bytes = 0
        self.stopped = False
        self._go = threading.Event()
        self._go.set()

    def pause(self):
        self._go.clear()

    def resume(self):
        self._go.set()

    @property
    def paused(self):
        return not self._go.is_set()

    def stop(self):
        self.stopped = True
        self._go.set()

    def checkpoint(self):
        """Blocks while paused. False when the build should end (stopped, or the program is shutting down)."""
        while not self._go.wait(0.5):
            if STOP or self.stopped:
                break
        return not (self.stopped or STOP)


def read_source(cfg, rel, src):
    """Where the pictures are read from. By default the file on the mount (`src`). With `read_url_base` set (for example the WebDAV address of the same
    folder, http://127.0.0.1:8282/webdav/.../__all__/) the film is read over HTTP with range requests instead: `base + the path of the film relative to the
    library`, each part percent-encoded. The mount stays the library listing and the fallback."""
    base = (cfg.get("read_url_base") or "").strip()
    if not base:
        return src
    import urllib.parse
    return base.rstrip("/") + "/" + "/".join(urllib.parse.quote(p, safe="") for p in rel.split("/"))


def mask_url(u):
    """The URL without a user:password@ part, for the log."""
    return re.sub(r"^(https?://)[^/@]*@", r"\1", u)


def http_wake(url, timeout):
    """The HTTP counterpart of wake_file: 1 MB at the start and at the end of the file, as range requests. Errors are ignored (ffprobe reports real ones)."""
    import urllib.request
    try:
        def rng(spec):
            rq = urllib.request.Request(url, headers={"Range": spec})
            with urllib.request.urlopen(rq, timeout=timeout) as r:
                r.read(1 << 20)
                return r.headers.get("Content-Range", "")
        cr = rng("bytes=0-1048575")                   # "bytes 0-1048575/12345" -> the size
        total = int(cr.rsplit("/", 1)[1]) if "/" in cr and cr.rsplit("/", 1)[1].isdigit() else 0
        if total > (2 << 20):
            rng(f"bytes={total - (1 << 20)}-{total - 1}")
    except Exception:                                 # noqa: BLE001
        pass


def wake_file(path, timeout):
    """Reads 1 MB at the start and at the end of the file (where MKV keeps its header and its index) so the mount has the file ready before ffprobe and
    PyAV need it. Returns the seconds it took; raises ProbeTimeout if the first touch is still not answered after `timeout` seconds."""
    t0 = time.time()

    def rd():
        if path.startswith(("http://", "https://")):
            http_wake(path, timeout)
            return
        try:
            with open(path, "rb") as f:
                f.read(1 << 20)
                end = f.seek(0, 2)
                f.seek(max(0, end - (1 << 20)))
                f.read(1 << 20)
        except OSError:
            pass                                   # ffprobe reports a real error; this read is only a nudge

    th = threading.Thread(target=rd, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise ProbeTimeout(f"the file did not answer within {timeout:.0f} s (cold on the mount?)")
    return time.time() - t0


class WakeAhead:
    """Wakes the next few files in the background while the current film is built, so their first-touch delays overlap instead of adding up."""

    def __init__(self, n, timeout):
        self.timeout = timeout
        self.seen = set()
        self.pool = ThreadPoolExecutor(max_workers=int(n)) if int(n) > 0 else None

    def ahead(self, paths):
        if not self.pool:
            return
        for p in paths:
            if p not in self.seen:
                self.seen.add(p)
                self.pool.submit(self._one, p)

    def _one(self, p):
        try:
            wake_file(p, self.timeout)
        except Exception:                          # noqa: BLE001 - a nudge only; the real attempt reports problems
            pass

    def close(self):
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)


def ffprobe(path, cfg):
    cmd = ["ffprobe", "-v", "error", "-probesize", str(cfg["probesize"]), "-select_streams", "v:0",
           "-show_entries", "stream=codec_name,width,height,color_transfer:format=duration", "-of", "json", path]
    tmo = float(cfg.get("probe_timeout") or 300.0)
    try:
        r = subprocess.run(low_priority() + cmd, capture_output=True, text=True, timeout=tmo)
    except subprocess.TimeoutExpired:
        raise ProbeTimeout(f"ffprobe did not finish within {tmo:.0f} s") from None
    if r.returncode != 0:
        # ffprobe starts every message with the file's full path; with long release names that used up the whole 200 characters and hid the reason
        msg = (r.stderr.strip() or "failed").replace(path, "<file>").replace(os.path.basename(path), "<file>")
        raise RuntimeError("ffprobe: " + msg[:300])
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
        if ok:
            bad = decode_error_in(r.stderr)
            if bad:                            # exit code 0 and a frame, but ffmpeg said it could not decode properly: do not trust the frame
                try:
                    os.remove(out)
                except OSError:
                    pass
                return False, "decode error: " + bad
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


def release_memory():
    """Runs the cyclic garbage collector and asks glibc to return freed memory to the system (a no-op where that is not available)."""
    import gc
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


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
            with self.lock:
                self.opened = [o for o in self.opened if o is not st[0]]      # a closed container must not be kept alive by this list
            del st
            # The container, its decoder and the filter graph refer to each other, so dropping them does not free them: they wait for Python's cyclic
            # collector, which does not see the hundreds of MB of native memory behind them. Measured on the owner's rig: every failed read (mount I/O
            # errors come in bursts) left about 20 MB behind, 3 GB after one film. Collect now, and give the freed memory back to the system.
            release_memory()

    @staticmethod
    def _log_setup():
        import av
        av.logging.set_level(av.logging.ERROR)          # decode errors are error-level; warnings would only be noise
        av.logging.set_skip_repeated(False)             # an identical error in the next grab must be reported again

    def grab(self, t, out):
        """Decodes one frame. libav's log is captured during the grab (all threads: a decoder's helper threads log too, so a concurrent grab can be
        blamed for another one's error: only ever an extra retry); a decode-error line makes it a failed read, whatever frame came out."""
        import av
        self._log_setup()
        exc = None
        res = (False, "")
        with av.logging.Capture(local=False) as logs:
            try:
                res = self._grab(t, out)
            except Exception as ex:            # noqa: BLE001
                exc = ex
        bad = decode_error_in("\n".join(str(l[2]) for l in logs))
        if bad:
            try:
                os.remove(out)
            except OSError:
                pass
            return False, "decode error: " + bad
        if exc is not None:
            raise exc
        return res

    def _grab(self, t, out):
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
        release_memory()


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
                if r[1].startswith("decode error"):          # a bad read, not a broken PyAV: the caller retries; no 6x dearer ffmpeg command
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
        if want == "pyav" and cfg.get("strict_extractor"):
            raise NoPyAV(why)
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
    job = cfg.get("_job")
    if job:
        job.phase = "waking"
    rsrc = read_source(cfg, rel, src)             # the mount path, or the HTTP address of the same file
    if rsrc != src:
        log(f"reading {rel} over HTTP: {mask_url(rsrc)}")
    t_wake = wake_file(rsrc, float(cfg.get("probe_timeout") or 300.0))
    t1 = time.time()
    if job:
        job.t_wake, job.phase = t_wake, "probing"
        if job.stopped:
            raise JobStopped()
    try:
        info = ffprobe(rsrc, cfg)
    except ProbeTimeout:
        raise
    except RuntimeError as ex:
        if rsrc == src:
            raise
        log(f"HTTP read failed ({str(ex)[:160]}); using the mount path for {rel}")
        rsrc = src
        info = ffprobe(src, cfg)
    t_probe = time.time() - t1
    if job:
        job.t_probe = t_probe
    refine = cfg.get("_refine")                 # (index, folder) of the coarser set this build refines, or None
    if cfg.get("share", 1) and not refine:
        sib = find_sibling(cfg["store"], rel, info["duration"], float(cfg.get("share_tolerance", 2.0)))
        if sib:
            write_alias(cfg["store"], rel, size, info["duration"], sib[2], sib[1])
            if job:
                job.shared, job.phase = True, "shared"
            log(f"shared {rel}: same film as {sib[2]} (runtimes {sib[0]:.1f} s apart); no thumbnails built")
            return True
        if cfg.get("_alias_only"):
            raise NoMatchingRelease(f"runtime {info['duration']:.1f} s matches no release already built (tolerance {float(cfg.get('share_tolerance', 2.0)):g} s)")
    n = max(1, int(info["duration"] // cfg["interval"]))
    allowed = cfg["budget_mb"] * 1e6 * (info["duration"] / 7200.0) * cfg["safety"]
    ext = make_extractor(cfg, rsrc, info, zscale_ok)
    final = entry_dir(cfg["store"], rel)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    work = tempfile.mkdtemp(prefix=".work-", dir=os.path.dirname(final))
    try:
        frames, stats, lock, abort = {}, dict(ok=0, fail=0, retried=0, suspect=0), threading.Lock(), []
        if job:
            job.n, job.stats, job.phase, job.t_read0 = n, stats, "reading", time.time()
        times =[min((i + 0.5) * cfg["interval"], max(0.0, info["duration"] - 1.0)) for i in range(n)]
        reuse = {}
        if refine:
            # thumbnails of the existing set that sit exactly on the new grid (a 30 s set holds every third thumbnail of a 10 s set): taken from its pack
            oix, oedir = refine
            old_at = {}
            for oi in range(oix["count"]):
                if oix["lengths"][oi] > 0:
                    old_at[round(min((oi + 0.5) * oix["interval"], max(0.0, oix["duration"] - 1.0)), 2)] = oi
            olddir = os.path.join(work, "old")
            os.makedirs(olddir)
            with open(os.path.join(oedir, "thumbs.pack"), "rb") as pk_old:
                for j in range(n):
                    oi = old_at.get(round(times[j], 2))
                    if oi is not None:
                        pk_old.seek(oix["offsets"][oi])
                        data = pk_old.read(oix["lengths"][oi])
                        if len(data) == oix["lengths"][oi]:
                            p_old = os.path.join(olddir, f"{j:06d}.jpg")
                            with open(p_old, "wb") as fo:
                                fo.write(data)
                            reuse[j] = p_old
            frames.update(reuse)
        todo_ix = [j for j in range(n) if j not in reuse]
        n_read = len(todo_ix)
        if job:
            job.n = n_read
        ext_of_frame = "ppm" if ext.name == "pyav" or (isinstance(ext, AutoExtractor) and ext.name == "pyav") else "png"

        def work_one(i):
            if abort or STOP or (job and job.stopped):
                return
            decode_retried = suspect_seen = False
            for attempt in range(12):
                if abort or STOP:
                    return
                if job and not job.checkpoint():                          # paused here (before taking a read slot); False = stopped
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
                if ok and not suspect_seen and suspicious_frame(out):
                    suspect_seen = True                                   # a near-constant grey/white frame: read it once more; the same again is kept
                    with lock:
                        stats["suspect"] += 1
                    try:
                        os.remove(out)
                    except OSError:
                        pass
                    continue
                if ok:
                    with lock:
                        frames[i] = out
                        stats["ok"] += 1
                    return
                if (why or "").startswith("decode error") and not decode_retried:
                    decode_retried = True                                 # garbage decoded (typically while the mount was rate-limiting): one retry
                    with lock:
                        stats["retried"] += 1
                    continue
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

        n_workers, mem_note = memory_limited_workers(cfg["workers"], cfg)
        if mem_note:
            log("MEMORY: " + mem_note)
        log(f"reading {n_read} thumbnails of {rel}" + (f" ({len(reuse)} of the {n} already exist at {refine[0]['interval']:g} s and are kept)" if refine else "") + f" with {n_workers} parallel read(s) (wake {t_wake:.0f} s, probe {t_probe:.0f} s)")
        every = float(cfg.get("progress_every", 60.0) or 0)
        halt = threading.Event()

        def ticker():
            last, t_last = 0, time.time()
            while every and not halt.wait(every):
                with lock:
                    ok = stats["ok"]
                now = time.time()
                rate = (ok - last) / max(0.001, now - t_last)
                left = (n_read - ok) / rate / 60 if rate > 0 else float("inf")
                log(f"  {ok}/{n_read} thumbnails read, {rate:.2f}/s in the last {now - t_last:.0f} s" + (f", about {left:.0f} min left" if left != float("inf") else ", no progress") + (f", memory {self_rss_mb():.0f} MB" if self_rss_mb() else ""))
                last, t_last = ok, now

        tk = threading.Thread(target=ticker, daemon=True)
        tk.start()
        try:
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                list(pool.map(work_one, todo_ix))
        finally:
            halt.set()
        ext.close()
        if job and job.stopped:
            raise JobStopped()
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
        if job:
            job.phase = "encoding"
        enc = os.path.join(work, "jpg")
        os.makedirs(enc)
        sizes, w, q = choose_and_encode(frames, enc, cfg["width"], allowed, int(cfg.get("encode_jobs") or 0) or (os.cpu_count() or 2))
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
        now_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        # a refined set keeps the `made` stamp of the set it grew from, so aliases of other releases that point at it stay valid
        ix = dict(v=1, src=rel, size=size, duration=round(info["duration"], 2), interval=cfg["interval"], w=w, h=h, count=n,
                  offsets=offsets, lengths=lengths, q=q, made=refine[0]["made"] if refine else now_iso)
        if refine:
            ix["refined"] = now_iso
        with open(os.path.join(work, "index.json"), "w") as f:
            json.dump(ix, f, separators=(",", ":"))
        shutil.rmtree(enc, ignore_errors=True)
        shutil.rmtree(os.path.join(work, "old"), ignore_errors=True)
        for fn in os.listdir(work):
            if fn.endswith((".png", ".ppm")):
                os.remove(os.path.join(work, fn))
        total = pos + os.path.getsize(os.path.join(work, "index.json"))
        if os.path.isdir(final):
            shutil.rmtree(final)
        os.rename(work, final)
        if job:
            job.bytes, job.phase = total, "done"
        dt = time.time() - t0
        log(f"{'refined' if refine else 'built'} {rel}: {n} thumbs ({stats['ok']} read, {stats['fail']} failed reads, {ext.name}), {w}x{h} q{q}, {total / 1e6:.2f} MB "
            f"(budget {allowed / 1e6:.2f}), {dt:.0f}s total (wake {t_wake:.0f}s, probe {t_probe:.0f}s, read {t_read - t_wake - t_probe:.0f}s), "
            f"{n_read / max(0.1, t_read - t_wake - t_probe):.1f} reads/s; "
            f"{stats['retried']} reads retried after decode errors, {stats['suspect']} near-constant grey/white frame(s) read again")
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


def order_todo(todo, share=True):
    """Orders films for a pass and tells which are one film in several releases. Returns [(item, role, n)]: role 'build' (a singleton, or the release of a group that
    is built: the smallest file, the cheapest to read) or 'alias' (the others of its group, which only get an alias if their runtime agrees); n = size of the group.
    Groups are ordered by their newest file, so new additions still come first."""
    groups = {}
    for it in todo:
        k = release_key(it[1]) if share else None
        groups.setdefault(k if k else "\0" + it[1], []).append(it)
    out = []
    for k, members in sorted(groups.items(), key=lambda kv: (-max(m[0] for m in kv[1]), kv[0])):
        members.sort(key=lambda m: (m[3], m[1]))
        for i, it in enumerate(members):
            out.append((it, "build" if i == 0 else "alias", len(members)))
    return out


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
    if not dry:
        clean_stale_work(cfg["store"])
    if not dry and state.d["breaker_until"] > time.time():
        log(f"circuit breaker open for another {(state.d['breaker_until'] - time.time()) / 60:.0f} min after repeated read failures")
        return 0
    todo = []
    n_excluded = 0
    for rel, src, size, mtime in walk_library(cfg["library"], cfg["min_size_mb"] * 1e6):
        if excluded(cfg, rel):
            n_excluded += 1
            continue
        if entry_current(cfg["store"], rel, size) or state.blocked(rel, size):
            continue
        if time.time() - mtime < cfg["settle"]:
            continue
        todo.append((mtime, rel, src, size))
    if n_excluded:
        log(f"{n_excluded} file(s) left out by skip_tv/exclude")
    ordered = order_todo(todo, bool(cfg.get("share", 1)))
    todo = [it for it, role, n in ordered]       # newest additions first; the releases of one film together, the smallest file first
    if dry:
        for (mtime, rel, src, size), role, n in ordered:
            note = f"   [{n} releases share this: built from this one, the smallest]" if role == "build" and n > 1 else \
                   "   [alias of the one above if its runtime agrees within " + f"{cfg.get('share_tolerance', 2.0):g} s]" if role == "alias" else ""
            print(f"{size / 1e9:6.1f} GB  {rel}{note}")
        films = sum(1 for it, role, n in ordered if role == "build")
        log(f"{len(todo)} file(s) need thumbnails = {films} film(s) to build" + (f" + {len(todo) - films} release(s) that would only get an alias" if len(todo) > films else ""))
        return len(todo)
    batch = todo if int(cfg["max_films"]) <= 0 else todo[: int(cfg["max_films"])]
    zs = have_filter("zscale")
    gov = Governor(cfg["workers"], cfg["rate_cooldown"])
    done = bad_streak = 0
    t_start = time.time()
    queue, retried, k = list(batch), set(), 0           # a file that is slow to open goes to the back of the queue once
    wk = WakeAhead(cfg.get("wake_ahead", 3), float(cfg.get("probe_timeout") or 300.0))
    while k < len(queue):
        mtime, rel, src, size = queue[k]
        k += 1
        if STOP:
            break
        while cfg["yield_to_playback"] and player_running() and not STOP:
            log("a player is running; waiting 5 min")
            sleep_interruptible(300)
        wk.ahead([q[2] for q in queue[k:k + max(0, int(cfg.get("wake_ahead", 3)))]])
        lk = film_lock(cfg["store"], rel)
        if lk is None:
            log(f"{rel} is being built by another seekthumbs; skipping")
            continue
        try:
            if build_entry(rel, src, size, cfg, gov, zs):
                state.ok(rel)
                done += 1
                bad_streak = 0
                per = (time.time() - t_start) / done
                log(f"progress: {done}/{len(batch)} films, {per / 60:.1f} min per film so far, about {per * (len(queue) - k) / 60:.0f} min left")
        except KeyboardInterrupt:
            break
        except ProbeTimeout as ex:
            if rel not in retried:
                retried.add(rel)
                queue.append((mtime, rel, src, size))
                log(f"SLOW TO OPEN {rel}: {ex}; skipped for now, tried again at the end of this pass (not a failure)")
            else:
                log(f"FAILED {rel}: still not opening on the second try: {ex}")
                state.failed(rel, size, str(ex))
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
        finally:
            lk.close()
        if cfg["film_pause"]:
            sleep_interruptible(cfg["film_pause"])
    wk.close()
    left = max(0, len(todo) - done)
    log(f"pass finished: {done} built, {left} still waiting; {gov.throttles} rate-limit signal(s), parallel reads ended at {gov.limit}/{gov.max}")
    return done


def refine_pass(cfg, dry=False):
    """Fills in a finer interval for films that were built coarse (for example 30 s first, then `interval = 10`): only the thumbnails that do not exist yet are
    read; those that sit on the new grid are kept. Films whose interval is already as fine as `interval` are left alone. Returns the number of films."""
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
    todo = []
    for rel, src, size, mtime in walk_library(cfg["library"], cfg["min_size_mb"] * 1e6):
        if excluded(cfg, rel):
            continue
        edir = entry_dir(cfg["store"], rel)
        ix = read_index(edir)
        if ix is None or ix.get("size") != size or not ix.get("interval") or ix["interval"] <= cfg["interval"] * 1.01:
            continue                                   # no set of its own (an alias, or not built), not this file, or already fine enough
        if state.blocked(rel, size):
            continue
        n_new = max(1, int(ix["duration"] // cfg["interval"]))
        keep = len({round(min((i + 0.5) * ix["interval"], max(0.0, ix["duration"] - 1.0)), 2) for i in range(ix["count"]) if ix["lengths"][i] > 0}
                   & {round(min((j + 0.5) * cfg["interval"], max(0.0, ix["duration"] - 1.0)), 2) for j in range(n_new)})
        todo.append((rel, src, size, ix, edir, n_new, keep))
    todo.sort(key=lambda t: t[2])                      # the smallest files first, as for the first build
    if dry:
        for rel, src, size, ix, edir, n_new, keep in todo:
            print(f"{size / 1e9:6.1f} GB  {rel}   {ix['interval']:g} s -> {cfg['interval']:g} s: {n_new - keep} to read, {keep} kept")
        log(f"{len(todo)} film(s) could be refined to {cfg['interval']:g} s; {sum(t[5] - t[6] for t in todo)} thumbnails to read")
        return len(todo)
    batch = todo if int(cfg["max_films"]) <= 0 else todo[: int(cfg["max_films"])]
    zs = have_filter("zscale")
    gov = Governor(cfg["workers"], cfg["rate_cooldown"])
    done = 0
    for rel, src, size, ix, edir, n_new, keep in batch:
        if STOP:
            break
        while cfg["yield_to_playback"] and player_running() and not STOP:
            log("a player is running; waiting 5 min")
            sleep_interruptible(300)
        lk = film_lock(cfg["store"], rel)
        if lk is None:
            log(f"{rel} is being built by another seekthumbs; skipping")
            continue
        try:
            c = dict(cfg, _refine=(ix, edir))
            if build_entry(rel, src, size, c, gov, zs):
                state.ok(rel)
                done += 1
        except KeyboardInterrupt:
            break
        except Throttled as ex:
            log(f"STOPPING: {ex}")
            state.d["breaker_until"] = time.time() + cfg["breaker_hours"] * 3600
            state.save()
            break
        except Exception as ex:                # noqa: BLE001 - the coarse set stays in place and keeps working
            log(f"REFINE FAILED {rel}: {ex}; the {ix['interval']:g} s set is kept")
        finally:
            lk.close()
    log(f"refine finished: {done} of {len(batch)} film(s) refined")
    return done


def cmd_build(cfg, path, own_process=True):
    """Builds the thumbnails of ONE film (the one that is playing). Meant to run beside playback: lowest CPU and I/O priority, play_workers parallel reads
    (default 2) with the same governor and circuit breaker as scan, and it does NOT wait for players (the player is the reason it runs).
    Returns 0 for 'done or nothing to do', 1 for a failed build, 2 for a bad request."""
    need_paths(cfg)
    if own_process:                            # (the tests call this inside their own process and leave signals and priority alone)
        for sg in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sg, on_signal)
        try:
            os.nice(19)
        except OSError:
            pass
        if shutil.which("ionice"):
            subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], capture_output=True)
    path = os.path.abspath(os.path.expanduser(path))
    rel = os.path.relpath(path, os.path.abspath(cfg["library"]))
    if rel.startswith(".."):
        log(f"{path} is not under the library {cfg['library']}; nothing built")
        return 2
    if os.path.splitext(path)[1].lower() not in VIDEO_EXT:
        log("not a video file; skipped:", rel)
        return 0
    try:
        size = os.stat(path).st_size
    except OSError as ex:
        log("cannot read", path, ex)
        return 2
    if size < cfg["min_size_mb"] * 1e6:
        log(f"smaller than {cfg['min_size_mb']:g} MB; skipped: {rel}")
        return 0
    if excluded(cfg, rel):
        log(f"left out by skip_tv/exclude; skipped: {rel}")
        return 0
    os.makedirs(cfg["store"], exist_ok=True)
    lk = film_lock(cfg["store"], rel)
    if lk is None:
        log("already being built by another seekthumbs:", rel)
        return 0
    try:
        if entry_current(cfg["store"], rel, size):
            log("already has thumbnails:", rel)
            return 0
        clean_stale_work(cfg["store"])
        state = State(cfg["store"])
        if state.d["breaker_until"] > time.time():
            log(f"circuit breaker open for another {(state.d['breaker_until'] - time.time()) / 60:.0f} min after repeated read failures; not building")
            return 0
        if state.blocked(rel, size):
            log("this film failed recently and is not retried yet:", rel, "-", state.d["fail"][rel]["why"][:100])
            return 0
        c = dict(cfg)
        c["workers"] = max(1, int(cfg["play_workers"]))
        c["extractor"] = cfg["play_extractor"]
        c["strict_extractor"] = cfg["play_extractor"] == "pyav"      # never fall back to one ffmpeg process per thumbnail next to a film that is playing
        c["pause"] = float(cfg["play_pause"])      # each worker rests this long after a read: caps the extra bandwidth (a 4K remux is read at about its own bit rate otherwise)
        c["encode_jobs"] = int(cfg.get("encode_jobs") or 0) or 2
        gov = Governor(c["workers"], cfg["rate_cooldown"])
        log(f"building {rel} ({size / 1e9:.1f} GB) with {c['workers']} parallel read(s), {c['pause']:g} s rest after each, lowest priority")
        try:
            build_entry(rel, path, size, c, gov, have_filter("zscale"))
            state.ok(rel)
            return 0
        except KeyboardInterrupt:
            log("interrupted; nothing kept")
            return 1
        except NoPyAV as ex:
            log(f"NOT building {rel}: PyAV is not usable ({ex}). The ffmpeg command would read several times more data from the mount (measured on one 4K remux: "
                "62.7 MB against 9.8 MB per thumbnail) while the film plays. Install python3-av (apt), or set play_extractor=auto to allow it. Not recorded as a failure.")
            return 1
        except Throttled as ex:
            log(f"STOPPING: {ex}")
            state.d["breaker_until"] = time.time() + cfg["breaker_hours"] * 3600
            state.save()
            return 1
        except ProbeTimeout as ex:
            log(f"SLOW TO OPEN {rel}: {ex}; not recorded as a failure, it is tried again next time")
            return 1
        except Exception as ex:                # noqa: BLE001
            log(f"FAILED {rel}: {ex}")
            state.failed(rel, size, str(ex))
            return 1
    finally:
        lk.close()


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


def mem_available_mb():
    """MemAvailable from /proc/meminfo in MB, or None."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return None


def self_rss_mb():
    """This process's resident memory in MB (all read threads share it), or None."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return None


def cgroup_headroom_mb():
    """(headroom MB, limit MB) under the tightest memory.max of this process's cgroup or any parent, or None when there is none (or no cgroup v2).
    Headroom = limit minus the anonymous (and kernel) memory in use there; page cache is left out because the kernel drops it before it kills anything.
    MemAvailable describes the whole computer, while a service started with `-p MemoryMax=5G` is killed at 5 GB whatever is free."""
    root = os.environ.get("SEEKTHUMBS_CGROUP_ROOT", "/sys/fs/cgroup")
    try:
        with open(os.environ.get("SEEKTHUMBS_PROC_CGROUP", "/proc/self/cgroup")) as f:
            rel = next(l.strip().split(":", 2)[2] for l in f if l.startswith("0::"))
    except (OSError, StopIteration, IndexError):
        return None
    best = None
    parts = [p for p in rel.split("/") if p]
    for depth in range(len(parts), -1, -1):
        d = os.path.join(root, *parts[:depth])
        try:
            with open(os.path.join(d, "memory.max")) as f:
                raw = f.read().strip()
            if raw == "max":
                continue
            limit = int(raw)
            stat = {}
            with open(os.path.join(d, "memory.stat")) as f:
                for line in f:
                    k, _, v = line.partition(" ")
                    stat[k] = int(v)
            used = stat.get("anon", 0) + stat.get("kernel", stat.get("slab_unreclaimable", 0))
        except (OSError, ValueError):
            continue
        room = (limit - used) / 1048576.0
        if best is None or room < best[0]:
            best = (room, limit / 1048576.0)
    return best


def memory_limited_workers(requested, cfg):
    """(workers to use, note). Every parallel read keeps its own open decoder (about 180 MB each for a 4K 10-bit HEVC film measured without HDR
    tone-mapping; the HDR path on the owner's rig used far more), so the number of reads is cut to what memory allows:
    workers <= (MemAvailable - mem_reserve_mb) / worker_mem_mb, and, under a cgroup memory limit (systemd-run -p MemoryMax=...), also
    <= (that limit's headroom - 300 MB) / worker_mem_mb. worker_mem_mb = 0 turns the limit off. Never below 1."""
    requested = max(1, int(requested))
    per, reserve = float(cfg.get("worker_mem_mb") or 0), float(cfg.get("mem_reserve_mb") or 0)
    if per <= 0:
        return requested, ""
    avail, cg, caps = mem_available_mb(), cgroup_headroom_mb(), []
    if avail is not None:
        caps.append((int((avail - reserve) // per), f"{avail / 1024:.1f} GB of memory is free, {reserve / 1024:.1f} GB is kept for the rest of the computer"))
    if cg is not None:
        caps.append((int((cg[0] - 300) // per), f"only {max(cg[0], 0) / 1024:.1f} GB is left under this service's {cg[1] / 1024:.1f} GB memory limit"))
    if not caps:
        return requested, ""
    n_cap, why = min(caps, key=lambda c: c[0])
    n = max(1, min(requested, n_cap))
    if n < requested:
        return n, f"{requested} parallel reads asked for, {n} used: {why}; each read is counted at {per:.0f} MB (settings worker_mem_mb and mem_reserve_mb)"
    return n, ""


def prefer_to_be_killed_first(adj=300):
    """If the computer runs out of memory, let the kernel pick this process (and the ffmpeg children it starts) before the desktop, browser and player.
    An ordinary user may raise this value, never lower it. Returns True when it was set."""
    try:
        with open("/proc/self/oom_score_adj") as f:
            cur = int(f.read().strip() or 0)
        if cur >= adj:
            return True
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write(str(adj))
        return True
    except (OSError, ValueError):
        return False


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
    n = tot = aliases = 0
    worst = []
    for edir in store_entries(cfg["store"]):
        ix = read_index(edir)
        sz = sum(os.path.getsize(os.path.join(edir, f)) for f in os.listdir(edir))
        n += 1
        tot += sz
        if ix:
            worst.append((sz / max(1.0, ix["duration"] / 7200.0) / 1e6, sz, os.path.relpath(edir, cfg["store"])))
        elif read_alias(edir):
            aliases += 1
    print(f"{n} entries ({n - aliases} with thumbnails, {aliases} sharing another release's), {tot / 1e6:.1f} MB in {cfg['store']}")
    for per2h, sz, rel in sorted(worst, reverse=True)[:5]:
        print(f"  {per2h:5.2f} MB per 2 h  ({sz / 1e6:.2f} MB)  {rel}")
    st = State(cfg["store"])
    print(f"{len(st.d['fail'])} film(s) with recorded failures" + (f"; circuit breaker open {(st.d['breaker_until'] - time.time()) / 60:.0f} more min"
                                                               if st.d["breaker_until"] > time.time() else ""))
    for rel, f in list(st.d["fail"].items())[:5]:
        print(f"  {rel}: {f['why']} (x{f['count']})")


def deep_check(edir, ix):
    """Decodes every thumbnail of an entry (one ffmpeg process for the whole pack) and lists the ones that are likely corrupt: undecodable, or
    near-constant grey/white (what a decoder that lost its reference produces). Returns a list of messages. Black frames are not flagged."""
    w, h = ix["w"], ix["h"]
    present = [i for i, ln in enumerate(ix["lengths"]) if ln]
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-f", "image2pipe", "-c:v", "mjpeg", "-i", os.path.join(edir, "thumbs.pack"),
           "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as ex:
        return [f"could not run ffmpeg: {ex}"]
    out = []
    sz = w * h
    got = len(r.stdout) // sz
    if r.stderr.strip():
        out.append("ffmpeg said: " + r.stderr.decode(errors="replace").strip()[:120])
    if got != len(present):
        out.append(f"{got} of {len(present)} thumbnails decode")
    for k in range(min(got, len(present))):
        fr = r.stdout[k * sz:(k + 1) * sz]
        sample = fr[:: max(1, sz // 2000)]
        if (max(sample) - min(sample)) <= 6 and sum(sample) / len(sample) >= 90:
            out.append(f"thumbnail {present[k]} (about {int((present[k] + 0.5) * ix['interval'])} s) is a flat grey/white picture")
    return out


def cmd_verify(cfg, deep=False):
    need_paths(cfg, library=False)
    bad = n = 0
    for edir in store_entries(cfg["store"]):
        n += 1
        ix = read_index(edir)
        if ix is None:
            al = read_alias(edir)
            if al is not None:
                me = os.path.relpath(edir, cfg["store"])[: -len(SUFFIX)]
                r = resolve_entry(cfg["store"], me)
                if r is None:
                    print("BROKEN alias (target missing, rebuilt since, or a loop):", edir)
                    bad += 1
                elif abs(float(r[0]["duration"]) - float(al.get("duration", 0))) > float(cfg.get("share_tolerance", 2.0)):
                    print(f"ALIAS runtime differs from its target by more than {cfg.get('share_tolerance', 2.0):g} s:", edir)
                    bad += 1
                continue
            print("BAD index/pack:", edir)
            bad += 1
            continue
        sz = os.path.getsize(os.path.join(edir, "thumbs.pack")) + os.path.getsize(os.path.join(edir, "index.json"))
        per2h = sz / max(1.0, ix["duration"] / 7200.0) / 1e6
        if per2h > cfg["budget_mb"]:
            print(f"OVER BUDGET {per2h:.2f} MB per 2 h: {edir}")
            bad += 1
        if deep:
            for msg in deep_check(edir, ix):
                print(f"SUSPECT {edir}: {msg}")
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
    if len(have) == 0:
        sys.exit("no videos found in the library: refusing to prune")
    entries = {os.path.relpath(e, cfg["store"])[: -len(SUFFIX)]: e for e in store_entries(cfg["store"])}
    gone, keep_target = [], {}
    # aliases first: one goes when its film is gone or its link is broken; the ones that stay keep their target (the pack) alive
    for rel, e in entries.items():
        al = read_alias(e)
        if al is None:
            continue
        if rel not in have or resolve_entry(cfg["store"], rel) is None:
            gone.append(e)
        else:
            keep_target.setdefault(resolve_entry(cfg["store"], rel)[2], []).append(rel)
    for rel, e in entries.items():
        if read_alias(e) is not None or rel in have:
            continue
        if rel in keep_target:
            print(f"keeping {e} (its film is gone, but {len(keep_target[rel])} other release(s) still use these thumbnails)")
        else:
            gone.append(e)
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


def set_conf_key(path, key, value):
    """Sets key=value in an mpv script-opts file, keeping every other line (creates the file if needed)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    if os.path.exists(path):
        with open(path) as f:
            lines = f.read().split("\n")
    out, done = [], False
    for ln in lines:
        if re.match(rf"^\s*{re.escape(key)}\s*=", ln):
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(ln)
    if not done:
        while out and out[-1] == "":
            out.pop()
        out.append(f"{key}={value}")
    with open(path, "w") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")


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
    conf = os.path.join(mpv_dir, "script-opts", "seekthumbs.conf")
    set_conf_key(conf, "builder", os.path.join(os.path.dirname(os.path.abspath(__file__)), "seekthumbs.py"))
    print("set builder= in", conf, "(the viewer starts a background build of the playing film through it; build=no there turns that off)")
    print("Needs uosc (or another OSC that talks to thumbfast), ffmpeg on PATH, and library=/store= in ~/.config/seekthumbs/config.ini.")
    print("Check: play a film that has a store entry; mpv's log shows 'seek thumbnails: <path>'. Undo: delete it and rename thumbfast.lua.bak back.")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    def add_settings(parser, default):
        """The settings options. Added to the main parser and to every subcommand, so they work before and after the subcommand
        (`seekthumbs.py --max-films 1 scan` and `seekthumbs.py scan --max-films 1`). On the subcommands the default is SUPPRESS: only what was typed there counts."""
        parser.add_argument("--config", default=default)
        parser.add_argument("--library", default=default)
        parser.add_argument("--store", default=default)
        for k, v in DEFAULTS.items():
            if k not in ("library", "store"):
                parser.add_argument("--" + k.replace("_", "-"), dest=k, type=type(v) if not isinstance(v, str) else str, default=default)

    add_settings(ap, None)
    common = argparse.ArgumentParser(add_help=False)
    add_settings(common, argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", parents=[common])
    p.add_argument("file")
    p = sub.add_parser("build", parents=[common])
    p.add_argument("file")
    for c in ("plan", "scan", "watch", "status"):
        sub.add_parser(c, parents=[common])
    p = sub.add_parser("refine", parents=[common], help="fill in a finer interval for films built coarse (reads only the missing thumbnails)")
    p.add_argument("--dry-run", action="store_true", help="list the films and how many thumbnails each needs, read nothing")
    p = sub.add_parser("verify", parents=[common])
    p.add_argument("--deep", action="store_true", help="also decode every thumbnail and list the ones that look corrupt (flat grey/white, undecodable)")
    p = sub.add_parser("prune", parents=[common])
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("install-service", parents=[common])
    p.add_argument("--enable", action="store_true")
    p = sub.add_parser("serve", parents=[common], help="a local web page: pick films, queue them, start/pause/stop, watch the progress")
    p.add_argument("--port", type=int, default=8781)
    p.add_argument("--open", action="store_true", help="also open the page in your browser")
    p = sub.add_parser("install-viewer", parents=[common])
    p.add_argument("--mpv-config-dir", default="~/.config/mpv-object-audio")
    args = ap.parse_args(argv)
    cfg = load_config(args)
    if args.cmd in ("build", "scan", "watch", "serve", "probe", "refine"):
        prefer_to_be_killed_first()
    if args.cmd == "probe":
        cmd_probe(cfg, args.file)
    elif args.cmd == "build":
        return cmd_build(cfg, args.file)
    elif args.cmd == "plan":
        scan_pass(cfg, dry=True)
    elif args.cmd == "scan":
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)
        scan_pass(cfg)
    elif args.cmd == "refine":
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)
        refine_pass(cfg, dry=args.dry_run)
    elif args.cmd == "watch":
        need_paths(cfg)
        cmd_watch(cfg)
    elif args.cmd == "status":
        cmd_status(cfg)
    elif args.cmd == "verify":
        return cmd_verify(cfg, deep=args.deep)
    elif args.cmd == "prune":
        cmd_prune(cfg, args.yes)
    elif args.cmd == "serve":
        need_paths(cfg)
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)
        import seekthumbs_web
        return seekthumbs_web.serve(sys.modules[__name__], cfg, args.port, args.open)
    elif args.cmd == "install-viewer":
        cmd_install_viewer(args.mpv_config_dir)
    elif args.cmd == "install-service":
        cmd_install_service(cfg, args.enable)
    return 0


def cli():
    """Runs main(); a reader that closes the pipe early (`plan | head`) ends the program quietly instead of with a traceback."""
    try:
        rc = main(sys.argv[1:])
        sys.stdout.flush()
    except BrokenPipeError:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        rc = 0
    sys.exit(rc)


if __name__ == "__main__":
    cli()
