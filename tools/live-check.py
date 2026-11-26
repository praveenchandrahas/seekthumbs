#!/usr/bin/env python3
"""live-check: step 0 of the on-the-fly thumbnail plan. Measures, on ONE real stream link, what building seek thumbnails while
the film plays would cost, using seekthumbs' own reader (bounded range reads, PyAV, keyframe flush, HDR tone-map).

    tools/live-check.py                 # link = first web link in $XDG_RUNTIME_DIR/stremio-last.m3u8 (the watcher leaves it there)
    tools/live-check.py URL             # or give the link
    options: --grabs 40 --workers 4 --width 320 --baseline 20 --keep DIR

What it answers:
  1. Does the link redirect, and does the final address answer range requests (206) with the file size?
  2. What one thumbnail costs on this link: MB, seconds, reads per second at --workers.
  3. How far before the target the keyframe lands (decides whether a 10 s grid gives distinct pictures).
  4. If mpv is playing (theater-mpv.sock), whether the reads hurt playback: cache seconds, dropped frames, waits for cache,
     first for --baseline seconds with no reads, then during the reads.
It reads nothing but the link and writes nothing outside a temp folder (or --keep DIR). The link carries keys, so only host names are printed.
"""
import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))      # the seekthumbs repo (this file lives in its tools/ folder)
sys.path.insert(0, HERE)
import seekthumbs as st                        # noqa: E402


def host(u):
    return urllib.parse.urlsplit(u).hostname or "?"


def link_from_playlist():
    p = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "stremio-last.m3u8")
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line.startswith(("http://", "https://")):
                    return line
    except OSError:
        pass
    sys.exit(f"no link given and none found in {p}; pass the link as an argument")


def resolve(url):
    """Follows redirects with a 1-byte range request. Returns (final url, hosts, status, total size, accept-ranges, seconds)."""
    hops = [host(url)]

    class Rec(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            hops.append(f"{code} -> {host(newurl)}")
            r = super().redirect_request(req, fp, code, msg, headers, newurl)
            if r is not None:
                r.add_header("Range", "bytes=0-0")
            return r

    op = urllib.request.build_opener(Rec)
    t0 = time.time()
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "mpv"})
    with op.open(req, timeout=30) as r:
        r.read(16)
        status, final = r.status, r.geturl()
        cr, ar = r.headers.get("Content-Range", ""), r.headers.get("Accept-Ranges", "")
    total = int(cr.rsplit("/", 1)[1]) if "/" in cr and cr.rsplit("/", 1)[1].isdigit() else None
    return final, hops, status, total, ar, time.time() - t0


class Mpv:
    """Samples the playing mpv over its IPC socket once a second."""
    PROPS = ("demuxer-cache-duration", "frame-drop-count", "decoder-frame-drop-count", "paused-for-cache", "pause", "time-pos")

    def __init__(self, path):
        self.path, self.rows, self.phase, self.stop = path, [], "baseline", False
        self.s = socket.socket(socket.AF_UNIX)
        self.s.settimeout(2)
        self.s.connect(path)
        self.f = self.s.makefile("rb")
        self.rid = 0

    def get(self, prop):
        self.rid += 1
        self.s.sendall((json.dumps({"command": ["get_property", prop], "request_id": self.rid}) + "\n").encode())
        while True:
            m = json.loads(self.f.readline() or b"{}")
            if m.get("request_id") == self.rid:
                return m.get("data") if m.get("error") == "success" else None

    def run(self):
        while not self.stop:
            try:
                row = {p: self.get(p) for p in self.PROPS}
            except (OSError, ValueError):
                break
            row["phase"] = self.phase
            self.rows.append(row)
            time.sleep(1)

    def summary(self, phase):
        rows = [r for r in self.rows if r["phase"] == phase and not r.get("pause")]
        if len(rows) < 2:
            return f"{phase}: not enough playing samples ({len(rows)})"
        cache = [r["demuxer-cache-duration"] for r in rows if r["demuxer-cache-duration"] is not None]

        def delta(k):
            v = [r[k] for r in rows if r[k] is not None]
            return (v[-1] - v[0]) if len(v) >= 2 else None
        waits = sum(1 for r in rows if r.get("paused-for-cache"))
        return (f"{phase}: {len(rows)} s playing, cache min {min(cache):.1f} s / median {sorted(cache)[len(cache) // 2]:.1f} s, "
                f"dropped frames +{delta('frame-drop-count')}, decoder drops +{delta('decoder-frame-drop-count')}, seconds waiting for cache {waits}"
                if cache else f"{phase}: mpv gave no cache figures")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?")
    ap.add_argument("--grabs", type=int, default=40)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--width", type=int, default=320)
    ap.add_argument("--baseline", type=float, default=20.0, help="seconds of mpv sampling before any read")
    ap.add_argument("--keep", help="keep the thumbnails (PPM) in this folder, to look at them")
    ap.add_argument("--socket", default=os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "theater-mpv.sock"))
    a = ap.parse_args()
    url = a.url or link_from_playlist()
    if host(url) in ("127.0.0.1", "localhost") and ":11470" in url:
        print("note: this is Stremio's local torrent server, which the plan leaves out; measuring it anyway")

    print("1. Link")
    final, hops, status, total, ar, dt = resolve(url)
    print(f"   {' '.join(hops)}   status {status}, {dt:.2f} s")
    print(f"   range requests: {'yes (206)' if status == 206 else 'NO (status ' + str(status) + ')'}; size {total / 1e9:.2f} GB" if total else
          f"   range requests: {'yes (206)' if status == 206 else 'NO (status ' + str(status) + ')'}; size not reported")
    if status != 206:
        sys.exit("   the final address does not answer range requests: the plan needs another reader for this kind of link")

    cfg = dict(st.DEFAULTS)
    cfg.update(width=a.width, workers=a.workers, extractor="pyav", strict_extractor=True)
    info = st.ffprobe(final, cfg)
    print(f"   {info['duration'] / 60:.0f} min, {info['width']}x{info['height']}, transfer {info['transfer'] or '?'}")

    mpv = None
    if os.path.exists(a.socket):
        try:
            mpv = Mpv(a.socket)
            threading.Thread(target=mpv.run, daemon=True).start()
            print(f"\n2. mpv is up: sampling {a.baseline:.0f} s with no reads first (keep it playing, not paused)")
            time.sleep(a.baseline)
        except OSError as ex:
            print(f"\n2. mpv socket not usable ({ex}); measuring the reads alone")
            mpv = None
    else:
        print("\n2. mpv not running (no theater-mpv.sock): measuring the reads alone. For the playback test, play the same link first.")

    vf = st.vf_chain(info, a.width, st.have_filter("zscale"))
    ok, why = st.AvExtractor.usable(vf)
    if not ok:
        sys.exit("PyAV not usable: " + why)
    ex = st.AvExtractor(final, vf, cfg)
    dur = info["duration"]
    times = [dur * (i + 0.5) / a.grabs for i in range(a.grabs)]
    out = a.keep or tempfile.mkdtemp(prefix="live-check-")
    os.makedirs(out, exist_ok=True)
    per = []

    def one(i):
        t0 = time.time()
        try:
            good, why_ = ex.grab(times[i], os.path.join(out, f"t{i:03d}_{int(times[i])}s.ppm"))
        except Exception as e:                  # noqa: BLE001
            good, why_ = False, str(e)
        per.append(time.time() - t0)
        if not good:
            print(f"   grab at {times[i]:.0f} s failed: {why_[:120]}")
        return good

    print(f"\n3. {a.grabs} thumbnails, {a.workers} at a time, {a.width} px wide")
    if mpv:
        mpv.phase = "reads"
    t0 = time.time()
    with ThreadPoolExecutor(a.workers) as pool:
        okc = sum(pool.map(one, range(a.grabs)))
    wall = time.time() - t0
    if mpv:
        mpv.phase = "after"
        time.sleep(3)
        mpv.stop = True
    rate = okc / wall if wall else 0
    note = ex.diag_note() or ""
    s = ex.stats.get("http") or {}
    mb = s["mb_sum"] / s["n"] if s.get("n") else None
    ex.close()
    print(f"   {okc}/{a.grabs} ok in {wall:.1f} s: {rate:.2f} thumbnails/s, {sorted(per)[len(per) // 2]:.2f} s per read (median)")
    print("   " + note)
    gap = ex.grab_seek_gap_max
    print(f"   worst keyframe landed {gap:.1f} s before its target: " +
          ("a 10 s grid gives distinct pictures" if gap < 10 else "a 10 s grid would repeat pictures here; the builder must skip duplicates"))

    print("\n4. Projection for this film (estimates from the figures above)")
    for step in (30, 20, 10):
        n = int(dur // step)
        new = n if step == 30 else (int(dur // 20) - int(dur // 60) if step == 20 else n - int(dur // 20) - int(dur // 30) + int(dur // 60))
        print(f"   {step:>2} s grid: {n} thumbnails" + (f", {new} new at this tier" if step != 30 else "") +
              (f", about {new / rate / 60:.1f} min" if rate else "") + (f", {new * mb / 1000:.2f} GB" if mb else ""))
    w, h = a.width, int(round(a.width * info["height"] / info["width"] / 2) * 2)
    print(f"   raw frames for the viewer: {w}x{h}, {w * h * 4 / 1e3:.0f} KB each, {int(dur // 10) * w * h * 4 / 1e6:.0f} MB of RAM at the 10 s grid")

    if mpv:
        print("\n5. Playback")
        for ph in ("baseline", "reads"):
            print("   " + mpv.summary(ph))
    print(f"\nthumbnails: {out}" + ("" if a.keep else " (temporary; use --keep DIR to keep them)"))


if __name__ == "__main__":
    main()
