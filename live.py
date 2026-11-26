"""Seek thumbnails for a STREAM, built while it plays and never stored (`seekthumbs.py live URL --out DIR`).

The library builder keys every entry by the film's path under the mount, so a link (a Stremio/Comet stream, say) never gets one. This builds the
thumbnails of one link while it plays, into a folder the viewer reads, and nothing is kept once the folder is removed (the viewer removes it when
mpv closes; old folders are cleared at the next start).

How:
  - The link is resolved once (redirects followed with a 1-byte range request), and the final address is read with seekthumbs' bounded range reader,
    PyAV and the keyframe flush, the same reads a library build makes. Comet answers 302 to a Real-Debrid download host; the 302 is not asked for again
    for every thumbnail. If the final address expires (401/403/404/410), the link is resolved again once.
  - Slots: thumbnail i shows the film at (i + 0.5) * interval seconds (interval 10 s by default), the same convention as the store.
  - Order: the gaps are halved. The middle slot first, then the quarter points, the eighths and so on, so the thumbnails are spread evenly at every
    moment and the 10 s grid is complete at the end. No slot is read twice.
  - Output, for a viewer that must never start a program from inside mpv (that made playback stutter, 2026-10-29):
        DIR/frames.bgra   count * w * h * 4 bytes; thumbnail i is raw BGRA at offset i * w * h * 4 (mpv's overlay-add reads it directly)
        DIR/done          count bytes; byte i is 1 once thumbnail i is complete (the frame is written before its byte)
        DIR/meta.json     {v, kind: "live", interval, count, w, h, duration, started}; written after the first thumbnail, so w and h are known
  - Run again with the same DIR and settings, it reads only the slots not done yet (a film loaded again in the same mpv).
  - Stops when: every slot is done; --parent-pid has gone away (mpv closed); SIGTERM; `server_error_stop` reads in a row failed; or the reads
    average more than `max_mb_per_thumb` (a release with no usable seek index), so a bad link cannot read gigabytes in the background.
  - Lowest CPU and I/O priority, and first in line for the kernel's out-of-memory killer.
"""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

LIVE_DEFAULTS = dict(interval=10.0, width=320, workers=8)
STALE_S = 12 * 3600


def host(u):
    return urllib.parse.urlsplit(u).hostname or "?"


def default_base():
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "seekthumbs-live")


def default_out(url):
    """A folder per link (by its address without the query, which may carry a changing signature)."""
    u = urllib.parse.urlsplit(url)
    key = hashlib.sha1(f"{u.scheme}://{u.netloc}{u.path}".encode()).hexdigest()[:16]
    return os.path.join(default_base(), key)


def resolve(url, timeout=30.0):
    """(final url, total size or None, status). Follows redirects with a 1-byte range request."""
    class Keep(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            r = super().redirect_request(req, fp, code, msg, headers, newurl)
            if r is not None:
                r.add_header("Range", "bytes=0-0")
            return r

    req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "mpv"})
    with urllib.request.build_opener(Keep).open(req, timeout=timeout) as r:
        r.read(16)
        cr = r.headers.get("Content-Range", "")
        total = int(cr.rsplit("/", 1)[1]) if "/" in cr and cr.rsplit("/", 1)[1].isdigit() else None
        return r.geturl(), total, r.status


def halving_order(n):
    """0..n-1 in the order that keeps the filled slots evenly spread: the middle, then the middles of the two halves, and so on (breadth first)."""
    order, level = [], [(0, n - 1)]
    while level:
        nxt = []
        for lo, hi in level:
            if lo > hi:
                continue
            m = (lo + hi) // 2
            order.append(m)
            nxt.append((lo, m - 1))
            nxt.append((m + 1, hi))
        level = nxt
    return order


def read_ppm(path):
    """(w, h, rgb bytes) of a binary PPM as write_ppm() writes it."""
    with open(path, "rb") as f:
        data = f.read()
    parts, pos = [], 0
    while len(parts) < 4:                         # magic, width, height, maxval, each followed by one whitespace byte
        while data[pos:pos + 1].isspace():
            pos += 1
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        parts.append(data[pos:end])
        pos = end
    if parts[0] != b"P6" or parts[3] != b"255":
        raise ValueError("not an 8-bit P6 PPM")
    w, h = int(parts[1]), int(parts[2])
    rgb = data[pos + 1: pos + 1 + w * h * 3]
    if len(rgb) != w * h * 3:
        raise ValueError("short PPM")
    return w, h, rgb


def rgb_to_bgra(rgb, w, h):
    out = bytearray(w * h * 4)
    out[0::4] = rgb[2::3]
    out[1::4] = rgb[1::3]
    out[2::4] = rgb[0::3]
    out[3::4] = b"\xff" * (w * h)
    return bytes(out)


def clean_stale(base, keep, now=None):
    """Removes live folders left behind (mpv crashed or was killed) once they are STALE_S old. Never `keep`."""
    now = now or time.time()
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    n = 0
    for name in names:
        d = os.path.join(base, name)
        if os.path.realpath(d) == os.path.realpath(keep) or not os.path.isdir(d):
            continue
        try:
            if now - os.path.getmtime(d) > STALE_S:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            pass
    return n


def alive(pid):
    if not pid:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Live:
    def __init__(self, st, cfg, url, out, parent_pid=None, log=None):
        self.st, self.cfg, self.url, self.out, self.parent_pid = st, cfg, url, out, parent_pid
        self.log = log or (lambda *a: print("seekthumbs live:", *a, file=sys.stderr, flush=True))
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.fails_in_row = 0
        self.failed = {}                          # slot -> tries
        self.why_stopped = ""
        self.bad = False                          # stopped by a guard (failures in a row, too many MB per thumbnail): exit code 3
        self.grabbed = 0
        self.fd = self.done_fd = None
        self.w = self.h = None

    # --- output files
    def _paths(self):
        return (os.path.join(self.out, "meta.json"), os.path.join(self.out, "frames.bgra"), os.path.join(self.out, "done"))

    def _resume(self, count, interval):
        """The done map of a matching earlier run in this folder (same count, interval and width), or None."""
        meta, frames, done = self._paths()
        try:
            with open(meta) as f:
                m = json.load(f)
            if m.get("count") != count or abs(float(m.get("interval", 0)) - interval) > 1e-6 or m.get("req_width") != int(self.cfg["width"]):
                return None
            with open(done, "rb") as f:
                d = f.read()
            if len(d) != count or os.path.getsize(frames) != count * m["w"] * m["h"] * 4:
                return None
            self.w, self.h = m["w"], m["h"]
            return d
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _open_files(self, count, interval, duration, fresh):
        meta, frames, done = self._paths()
        if fresh:
            for p in (meta, frames, done):
                try:
                    os.remove(p)
                except OSError:
                    pass
            with open(frames, "wb") as f:
                f.truncate(count * self.w * self.h * 4)
            with open(done, "wb") as f:
                f.write(b"\0" * count)
        self.fd = os.open(frames, os.O_WRONLY)
        self.done_fd = os.open(done, os.O_WRONLY)
        if fresh:
            tmp = meta + ".tmp"
            with open(tmp, "w") as f:
                json.dump(dict(v=1, kind="live", interval=interval, count=count, w=self.w, h=self.h, duration=duration,
                               req_width=int(self.cfg["width"]), started=time.strftime("%Y-%m-%d %H:%M:%S%z")), f)
            os.replace(tmp, meta)              # meta.json appears last: a viewer that sees it can trust the other two files

    def _put(self, i, bgra):
        os.pwrite(self.fd, bgra, i * self.w * self.h * 4)
        os.pwrite(self.done_fd, b"\1", i)

    # --- reading
    def _grab(self, ext, t, tmpdir, i):
        out = os.path.join(tmpdir, f"{i}.ppm")
        try:
            ok, why = ext.grab(t, out)
        except Exception as ex:                 # noqa: BLE001
            ok, why = False, str(ex)
        if not ok:
            return None, why
        try:
            w, h, rgb = read_ppm(out)
        except (OSError, ValueError) as ex:
            return None, f"bad frame: {ex}"
        finally:
            try:
                os.remove(out)
            except OSError:
                pass
        return (w, h, rgb), ""

    def _should_stop(self, ext):
        if self.stop.is_set():
            return True
        if not alive(self.parent_pid):
            self.why_stopped = self.why_stopped or "the player closed"
            self.stop.set()
            return True
        s = ext.stats.get("http") or {}
        cap = float(self.cfg.get("max_mb_per_thumb") or 0)
        if cap > 0 and s.get("n", 0) >= 8 and s["mb_sum"] / s["n"] > cap:
            self.why_stopped = f"reads average {s['mb_sum'] / s['n']:.0f} MB per thumbnail, above max_mb_per_thumb {cap:.0f} (no usable seek index?)"
            self.bad = True
            self.stop.set()
            return True
        return False

    def run(self):
        st, cfg = self.st, self.cfg
        t0 = time.time()
        os.makedirs(self.out, exist_ok=True)
        cleaned = clean_stale(os.path.dirname(os.path.abspath(self.out)), self.out)
        try:
            final, total, status = resolve(self.url, timeout=float(cfg["seek_timeout"]))
        except Exception as ex:                 # noqa: BLE001
            self.log(f"cannot open the link ({host(self.url)}): {type(ex).__name__}: {ex}")
            return 2
        if status != 206:
            self.log(f"{host(final)} does not answer range requests (status {status}); no thumbnails for this link")
            return 2
        src = st.HttpSource(final, refresh=lambda: resolve(self.url, timeout=float(cfg["seek_timeout"]))[0], label=host(final))
        try:
            info = st.ffprobe(src, cfg)
        except Exception as ex:                 # noqa: BLE001
            self.log(f"cannot probe the stream at {host(final)}: {ex}")
            return 2
        interval = float(cfg["interval"])
        count = max(1, int(info["duration"] // interval))
        vf = st.vf_chain(info, int(cfg["width"]), st.have_filter("zscale"))
        ok, why = st.AvExtractor.usable(vf)
        if not ok:
            self.log("PyAV is not usable (" + why + "); live thumbnails need it (apt install python3-av)")
            return 2
        ext = st.AvExtractor(src, vf, cfg)
        order = halving_order(count)
        prev = self._resume(count, interval)
        todo = [i for i in order if not (prev and prev[i])]
        workers, note = st.memory_limited_workers(int(cfg["workers"]), cfg)
        self.log(f"{host(self.url)} -> {host(final)}: {info['duration'] / 60:.0f} min, {info['width']}x{info['height']}"
                 + (f", {total / 1e9:.1f} GB" if total else "") + f"; {count} thumbnails every {interval:g} s, "
                 + (f"{count - len(todo)} already done, " if prev else "") + f"{workers} parallel reads"
                 + (f" ({note})" if note else "") + (f"; removed {cleaned} old folder(s)" if cleaned else ""))
        tmpdir = tempfile.mkdtemp(prefix="seekthumbs-live-")
        try:
            # The first thumbnail decides the picture size; the files are made after it.
            if prev is None:
                first = None
                while todo and first is None and not self._should_stop(ext):
                    i = todo.pop(0)
                    res, why = self._grab(ext, (i + 0.5) * interval, tmpdir, i)
                    if res is None:
                        self.fails_in_row += 1
                        self.failed[i] = 1
                        self.log(f"thumbnail at {(i + 0.5) * interval:.0f} s failed: {why[:160]}")
                        if self.fails_in_row >= int(cfg["server_error_stop"]):
                            self.why_stopped = f"{self.fails_in_row} reads in a row failed"
                            self.bad = True
                            return self._finish(ext, count, t0, 3)
                        continue
                    first = (i, res)
                if first is None:
                    return self._finish(ext, count, t0, 3 if self.bad else 0)
                i, (w, h, rgb) = first
                self.w, self.h = w, h
                self._open_files(count, interval, info["duration"], fresh=True)
                self._put(i, rgb_to_bgra(rgb, w, h))
                self.grabbed += 1
                self.fails_in_row = 0
            else:
                self._open_files(count, interval, info["duration"], fresh=False)
            todo = todo + [i for i in order if i in self.failed and i not in todo]      # a slot that failed before the files existed: once more at the end
            self._run_pool(ext, todo, interval, tmpdir, workers)
            return self._finish(ext, count, t0, 3 if self.bad else 0)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            ext.close()
            for fd in (self.fd, self.done_fd):
                if fd is not None:
                    os.close(fd)

    def _run_pool(self, ext, todo, interval, tmpdir, workers):
        queue, qlock = list(todo), threading.Lock()
        limit = int(self.cfg["server_error_stop"])

        def worker():
            while True:
                if self._should_stop(ext):
                    return
                with qlock:
                    if not queue:
                        return
                    i = queue.pop(0)
                res, why = self._grab(ext, (i + 0.5) * interval, tmpdir, i)
                with self.lock:
                    if res is None:
                        self.fails_in_row += 1
                        tries = self.failed.get(i, 0) + 1
                        self.failed[i] = tries
                        if tries < 2:
                            with qlock:
                                queue.append(i)      # once more, after everything else
                        if self.fails_in_row >= limit:
                            self.why_stopped = f"{self.fails_in_row} reads in a row failed (last: {why[:160]})"
                            self.bad = True
                            self.stop.set()
                        continue
                    w, h, rgb = res
                    if (w, h) != (self.w, self.h):
                        self.failed[i] = 2
                        continue
                    self.fails_in_row = 0
                    self.failed.pop(i, None)
                self._put(i, rgb_to_bgra(rgb, w, h))
                with self.lock:
                    self.grabbed += 1

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _finish(self, ext, count, t0, rc):
        done = 0
        try:
            with open(self._paths()[2], "rb") as f:
                done = sum(1 for b in f.read() if b)
        except OSError:
            pass
        s = ext.stats.get("http") or {}
        mb = f", {s['mb_sum'] / s['n']:.1f} MB per thumbnail" if s.get("n") else ""
        self.log(f"{done}/{count} thumbnails ({self.grabbed} read now) in {time.time() - t0:.0f} s{mb}"
                 + (f"; stopped: {self.why_stopped}" if self.why_stopped else ""))
        return rc


def cmd_live(st, cfg, url, out=None, parent_pid=None):
    if not url.startswith(("http://", "https://")):
        print("seekthumbs live: only http(s) links; a file on disk uses the store (build FILE)", file=sys.stderr)
        return 2
    u = urllib.parse.urlsplit(url)
    if u.hostname in ("127.0.0.1", "localhost", "::1") and u.port == 11470:
        print("seekthumbs live: Stremio's own torrent server is left out (every seek would fetch new torrent pieces)", file=sys.stderr)
        return 2
    try:
        os.nice(19)
    except OSError:
        pass
    if shutil.which("ionice"):
        subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], capture_output=True)
    st.prefer_to_be_killed_first()
    live = Live(st, cfg, url, out or default_out(url), parent_pid=parent_pid)

    def on_term(signum, frame):
        live.why_stopped = live.why_stopped or "asked to stop"
        live.stop.set()
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    return live.run()
