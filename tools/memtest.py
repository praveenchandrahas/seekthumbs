#!/usr/bin/env python3
"""Where does memory grow? Reads N thumbnails of ONE film the way seekthumbs does and prints the process memory every 10 reads.
Run it on a film that is already warm on the mount, with the web page stopped or idle, once per mode:

  python3 tools/memtest.py FILM --mode chain  --reads 80 --workers 6   # the real thing: decode + scale + HDR tone-mapping
  python3 tools/memtest.py FILM --mode scale  --reads 80 --workers 6   # decode + scale, no tone-mapping
  python3 tools/memtest.py FILM --mode decode --reads 80 --workers 6   # decode only, nothing else
  python3 tools/memtest.py FILM --mode chain  --reads 300 --workers 6 --every 20   # a long run, spaced like a real build: does it grow later?
  MALLOC_ARENA_MAX=2 python3 tools/memtest.py FILM --mode chain --reads 80 --workers 6   # same as the first, with the allocator held to 2 arenas

A mode whose memory keeps rising with the reads is where the growth is. Reads nothing but the film; writes nothing outside a temporary folder."""
import argparse
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import seekthumbs as st  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("film")
    ap.add_argument("--mode", choices=["chain", "scale", "decode"], default="chain")
    ap.add_argument("--reads", type=int, default=80)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--every", type=int, default=10, help="print the memory every this many reads")
    a = ap.parse_args()
    cfg = dict(st.DEFAULTS)
    info = st.ffprobe(a.film, cfg)
    zs = st.have_filter("zscale")
    dur = info["duration"]
    times = [dur * (0.03 + 0.94 * i / max(1, a.reads - 1)) for i in range(a.reads)]
    print(f"{os.path.basename(a.film)}: {dur / 60:.0f} min, {info['width']}x{info['height']}, transfer {info['transfer'] or '?'}, zscale {'yes' if zs else 'NO'}, "
          f"mode {a.mode}, {a.workers} workers, MALLOC_ARENA_MAX={os.environ.get('MALLOC_ARENA_MAX', '-')}", flush=True)
    print(f"start: {st.self_rss_mb():.0f} MB", flush=True)
    tmp = tempfile.mkdtemp(prefix="memtest-")
    lock, done, nxt, errs = threading.Lock(), [0], [0], [0]
    if a.mode == "decode":
        import av
        local = threading.local()

        def grab(t, out):
            if getattr(local, "c", None) is None:
                local.c = av.open(a.film, options={"probesize": str(cfg["probesize"])})
                local.s = local.c.streams.video[0]
                local.s.codec_context.skip_frame = "NONKEY"
            ts = int(t / local.s.time_base) + int(local.s.start_time or 0)
            local.c.seek(ts, stream=local.s, backward=True, any_frame=False)
            for n, pkt in enumerate(local.c.demux(local.s)):
                for fr in pkt.decode():
                    return True
                if n > 300:
                    break
            return False
    else:
        vf = st.vf_chain(info, cfg["width"], zs) if a.mode == "chain" else f"scale={cfg['width']}:-2:flags=bilinear"
        ext = st.AvExtractor(a.film, vf, cfg)

        def grab(t, out):
            ok, why = ext.grab(t, out)
            if os.path.exists(out):
                os.remove(out)
            if not ok:
                print("read failed:", why, flush=True)
            return ok

    def work():
        while True:
            with lock:
                i = nxt[0]
                nxt[0] += 1
            if i >= a.reads:
                return
            try:
                grab(times[i], os.path.join(tmp, f"{i}.ppm"))
            except Exception as ex:                # noqa: BLE001 - a mount I/O error must not end the run: count it and go on
                with lock:
                    errs[0] += 1
                    print(f"  read error ({type(ex).__name__}: {str(ex)[-90:]})", flush=True)
            with lock:
                done[0] += 1
                if done[0] % a.every == 0:
                    print(f"{done[0]:4d} reads  {st.self_rss_mb():6.0f} MB", flush=True)

    t0 = time.time()
    ts = [threading.Thread(target=work) for _ in range(a.workers)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    print(f"done: {done[0]} reads ({errs[0]} with a read error) in {time.time() - t0:.0f} s, {st.self_rss_mb():.0f} MB at the end", flush=True)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
