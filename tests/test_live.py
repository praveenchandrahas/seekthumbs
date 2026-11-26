#!/usr/bin/env python3
"""Tests for live.py (thumbnails of a streamed link, not stored). The end-to-end parts need PyAV and are skipped without it."""
import http.server
import json
import os
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import seekthumbs as st  # noqa: E402
import live  # noqa: E402

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ((" - " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails += 1


# --- halving order
for n in (1, 2, 3, 13, 60, 943):
    o = live.halving_order(n)
    check(f"halving order covers 0..{n - 1} once", sorted(o) == list(range(n)), o[:20])
o = live.halving_order(60)
check("the middle slot comes first", o[0] == 29, o[:3])
worst = 0.0
for k in range(2, 60):                       # after k slots, the largest gap (counting the ends) against the even spacing n/k
    pts = sorted(o[:k])
    gaps = [pts[0] + 1] + [b - a for a, b in zip(pts, pts[1:])] + [60 - pts[-1]]
    worst = max(worst, max(gaps) / (60 / k))
check("at every moment the largest gap is at most about twice the even spacing", worst <= 2.6, worst)

# --- PPM -> BGRA
tmp = tempfile.mkdtemp(prefix="seekthumbs-live-test-")
try:
    ppm = os.path.join(tmp, "a.ppm")
    st.write_ppm(ppm, 2, 1, [bytes([10, 20, 30, 40, 50, 60])])
    w, h, rgb = live.read_ppm(ppm)
    check("read_ppm reads what write_ppm wrote", (w, h, rgb) == (2, 1, bytes([10, 20, 30, 40, 50, 60])), (w, h, rgb))
    check("rgb_to_bgra swaps red and blue and sets alpha", live.rgb_to_bgra(rgb, 2, 1) == bytes([30, 20, 10, 255, 60, 50, 40, 255]))

    # --- old folders are cleared, the current one never
    base = os.path.join(tmp, "base")
    old, cur, new = (os.path.join(base, x) for x in ("old", "cur", "new"))
    for d in (old, cur, new):
        os.makedirs(d)
    t_old = time.time() - live.STALE_S - 60
    for d in (old, cur):
        os.utime(d, (t_old, t_old))
    n = live.clean_stale(base, cur)
    check("a stale live folder is removed, the current and a fresh one are kept", n == 1 and not os.path.exists(old) and os.path.isdir(cur) and os.path.isdir(new))

    # --- links that are refused before any read
    seek = os.path.join(os.path.dirname(HERE), "seekthumbs.py")

    def run(*args, env_extra=None):
        env = dict(os.environ, XDG_RUNTIME_DIR=tmp, no_proxy="127.0.0.1,localhost", NO_PROXY="127.0.0.1,localhost")
        env.update(env_extra or {})
        return subprocess.run([sys.executable, seek, "live", *args], capture_output=True, text=True, timeout=300, env=env)

    r = run("http://127.0.0.1:11470/abc/0")
    check("Stremio's torrent server is refused", r.returncode == 2 and "torrent" in r.stderr, r.stderr)
    r = run("/some/file.mkv")
    check("a file path is refused (the store handles files)", r.returncode == 2, r.stderr)

    try:
        import av  # noqa: F401
        have_av = True
    except ImportError:
        have_av = False

    if have_av:
        film = os.path.join(tmp, "film.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=24", "-t", "125", "-g", "48",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", film], check=True)
        size = os.path.getsize(film)
        hits = {"redirect": 0, "file": 0}

        class H(http.server.BaseHTTPRequestHandler):
            ranges = True
            delay = 0.0
            reset_at = None           # from this file request on, for reset_s seconds: every request is answered with a TCP reset
            reset_s = 1.5
            reset_t0 = None
            refuse_from = None        # from this file request on: 503

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/playback/"):
                    hits["redirect"] += 1
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/dl/film.mkv?sig=1")
                    self.end_headers()
                    return
                hits["file"] += 1
                time.sleep(self.delay)
                if H.reset_at is not None and hits["file"] >= H.reset_at and H.reset_t0 is None:
                    H.reset_t0 = time.time()
                if H.reset_t0 is not None and time.time() - H.reset_t0 < H.reset_s:
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    self.close_connection = True
                    self.connection.close()
                    return
                if self.refuse_from is not None and hits["file"] >= self.refuse_from:
                    self.send_response(503, "Service Unavailable")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                rng = self.headers.get("Range")
                lo, hi = 0, size - 1
                if rng and self.ranges:
                    a, b = rng.split("=")[1].split("-")
                    lo, hi = int(a), min(int(b) if b else size - 1, size - 1)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {lo}-{hi}/{size}")
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(hi - lo + 1))
                self.end_headers()
                with open(film, "rb") as f:
                    f.seek(lo)
                    try:
                        self.wfile.write(f.read(hi - lo + 1))
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        srv = S(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        link = f"http://127.0.0.1:{port}/playback/KEYSECRET/1"
        try:
            out = os.path.join(tmp, "out")
            r = run(link, "--out", out, "--workers", "3", "--worker-mem-mb", "0")
            check("live exits cleanly", r.returncode == 0, r.stderr[-800:])
            meta = json.load(open(os.path.join(out, "meta.json")))
            done = open(os.path.join(out, "done"), "rb").read()
            frames = os.path.getsize(os.path.join(out, "frames.bgra"))
            check("12 slots of 10 s for a 125 s film, 320 px wide", meta["count"] == 12 and meta["interval"] == 10 and meta["w"] == 320 and meta["h"] == 180, meta)
            check("every slot is done", len(done) == 12 and all(done), done)
            check("frames.bgra holds count * w * h * 4 bytes", frames == 12 * 320 * 180 * 4, frames)
            check("the redirect is asked for once, not per thumbnail", hits["redirect"] == 1 and hits["file"] > 12, hits)
            check("the link (with its key) is never printed", "KEYSECRET" not in r.stderr, r.stderr)
            with open(os.path.join(out, "frames.bgra"), "rb") as f:
                f.seek(5 * 320 * 180 * 4)
                fr = f.read(320 * 180 * 4)
            check("a frame has opaque alpha and real picture content", fr[3::4] == b"\xff" * (320 * 180) and len(set(fr[0::4])) > 20)
            # the frame for slot 5 (55 s) against ffmpeg's own picture at 55 s (keyframes 2 s apart: within 2 s); compare mean brightness
            ref = subprocess.run(["ffmpeg", "-v", "error", "-ss", "55", "-i", film, "-frames:v", "1", "-vf", "scale=320:180", "-f", "rawvideo",
                                  "-pix_fmt", "bgra", "-"], capture_output=True, check=True).stdout
            diff = sum(abs(a - b) for a, b in zip(fr[::97], ref[::97])) / len(fr[::97])
            check("slot 5 looks like the film at about 55 s", diff < 25, diff)

            before = dict(hits)
            r = run(link, "--out", out, "--workers", "3", "--worker-mem-mb", "0")
            check("a second run with the same folder reads no thumbnails", r.returncode == 0 and "(0 read now)" in r.stderr
                  and hits["file"] - before["file"] < 12, (r.stderr[-400:], hits, before))

            r = run(link, "--out", out, "--workers", "3", "--worker-mem-mb", "0", "--interval", "20")
            meta2 = json.load(open(os.path.join(out, "meta.json")))
            check("a different interval starts the folder afresh", r.returncode == 0 and meta2["count"] == 6 and meta2["interval"] == 20, meta2)

            dead = subprocess.Popen(["true"])
            dead.wait()
            out3 = os.path.join(tmp, "out3")
            r = run(link, "--out", out3, "--workers", "3", "--worker-mem-mb", "0", "--parent-pid", str(dead.pid))
            check("with the player gone it stops at once", r.returncode == 0 and "the player closed" in r.stderr, r.stderr[-400:])

            H.ranges = False
            r = run(link, "--out", os.path.join(tmp, "out4"), "--worker-mem-mb", "0")
            check("a server without range requests is refused", r.returncode == 2 and "range requests" in r.stderr, r.stderr[-400:])
            H.ranges = True

            # SIGTERM while running: stops, keeps what is done
            H.delay = 0.05
            out5 = os.path.join(tmp, "out5")
            env = dict(os.environ, XDG_RUNTIME_DIR=tmp, no_proxy="127.0.0.1,localhost", NO_PROXY="127.0.0.1,localhost")
            p = subprocess.Popen([sys.executable, seek, "live", link, "--out", out5, "--workers", "1", "--worker-mem-mb", "0", "--interval", "1"],
                                 stderr=subprocess.PIPE, text=True, env=env)
            t0 = time.time()
            while not os.path.exists(os.path.join(out5, "meta.json")) and time.time() - t0 < 60:
                time.sleep(0.1)
            time.sleep(0.5)
            p.terminate()
            err = p.communicate(timeout=60)[1]
            H.delay = 0.0
            d5 = open(os.path.join(out5, "done"), "rb").read()
            check("SIGTERM stops it and keeps the finished thumbnails", p.returncode == 0 and "asked to stop" in err and 0 < sum(d5) < len(d5),
                  (p.returncode, err[-300:], sum(d5), len(d5)))

            # a burst of dropped connections in the middle: waited out, not a stop
            out6 = os.path.join(tmp, "out6")
            H.reset_at = hits["file"] + 15
            r = run(link, "--out", out6, "--workers", "3", "--worker-mem-mb", "0", env_extra={"SEEKTHUMBS_LIVE_WAIT_S": "0.5"})
            H.reset_at = H.reset_t0 = None
            d6 = open(os.path.join(out6, "done"), "rb").read()
            check("a burst of connection resets is waited out and every slot still gets done",
                  r.returncode == 0 and all(d6) and "waiting" in r.stderr and "burst" in r.stderr, (r.returncode, r.stderr[-600:], sum(d6)))
            check("PyAV's dropped-exception tracebacks do not reach the output", "exception being dropped" not in r.stderr and "Traceback" not in r.stderr,
                  r.stderr[-600:])

            # the source refusing (503 on every read): stops after server_error_stop, exit code 3
            out7 = os.path.join(tmp, "out7")
            H.refuse_from = hits["file"] + 15
            r = run(link, "--out", out7, "--workers", "3", "--worker-mem-mb", "0", "--server-error-stop", "6")
            H.refuse_from = None
            check("a source that keeps refusing stops the run (exit code 3)", r.returncode == 3 and "refused" in r.stderr, (r.returncode, r.stderr[-600:]))

            # --- the viewer side (thumbfast.lua under lupa, a mocked mp API, the real builder)
            try:
                import lupa
            except ImportError:
                lupa = None
                print("SKIP live viewer tests (pip install lupa)")
            if lupa:
                L = lupa.LuaRuntime(unpack_returned_tuples=True)
                src = open(os.path.join(os.path.dirname(HERE), "thumbfast.lua")).read()
                lbase = os.path.join(tmp, "lv")

                def viewer(path, live_on=True):
                    h = {"log": [], "async": [], "native": [], "timers": [], "aborted": [], "ev": {}, "msg": {}, "clock": 1000.0}
                    props = {"path": path, "pid": str(os.getpid()), "working-directory": "/", "file-size": None}

                    def commandv(*a):
                        h["log"].append(("commandv",) + tuple(str(x) for x in a))
                        return True

                    def command_native(t):
                        args = list(dict(dict(t)["args"]).values())
                        h["native"].append(args)
                        subprocess.run(args, capture_output=True)
                        return True

                    def command_native_async(t, cb):
                        args = list(dict(dict(t)["args"]).values())
                        h["async"].append((args, cb))
                        return f"tok{len(h['async'])}"

                    def periodic(sec, fn):
                        e = {"fn": fn, "dead": False, "sec": sec}
                        h["timers"].append(e)
                        return L.table(kill=lambda *a: e.__setitem__("dead", True))

                    def to_lua(v):
                        if isinstance(v, dict):
                            return L.table_from({k: to_lua(x) for k, x in v.items()})
                        if isinstance(v, list):
                            return L.table(*[to_lua(x) for x in v])
                        return v

                    def to_py(t):
                        if hasattr(t, "items"):
                            return {k: to_py(v) for k, v in t.items()}
                        return t

                    L.execute("package.loaded['mp']=nil")
                    g = L.globals()
                    mp = L.table(get_property=lambda n: props.get(n), commandv=commandv, command_native=command_native,
                                 command_native_async=command_native_async, abort_async_command=lambda tok: h["aborted"].append(tok),
                                 register_script_message=lambda n, f: h["msg"].__setitem__(n, f), register_event=lambda n, f: h["ev"].__setitem__(n, f),
                                 get_time=lambda: h["clock"], add_timeout=lambda sec, fn: L.table(kill=lambda *a: None), add_periodic_timer=periodic,
                                 msg=L.table(info=lambda *a: h["log"].append(("info",) + tuple(str(x) for x in a))))
                    utils = L.eval("""function(dec, enc)
                        return { join_path = function(a, b) if b:sub(1,1) == '/' then return b end return (a:gsub('/+$','')) .. '/' .. b end,
                                 parse_json = dec, format_json = enc } end""")(lambda x: to_lua(json.loads(x)), lambda t: json.dumps(to_py(t)))
                    g.package.loaded["mp"] = mp
                    g.package.loaded["mp.utils"] = utils
                    g.package.loaded["mp.options"] = L.table(read_options=lambda o, n: None)
                    os.environ["XDG_RUNTIME_DIR"] = tmp
                    chunk = src.replace('build = false, builder = "", python = "python3",', f'build = false, builder = "{seek}", python = "{sys.executable}",')
                    chunk = chunk.replace('live = false, live_dir = "", live_poll_s = 2,', f'live = {"true" if live_on else "false"}, live_dir = "{lbase}", live_poll_s = 2,')
                    chunk = chunk.replace('local opts = { library = "", store = "",', f'local opts = {{ library = "{tmp}/nolib", store = "{tmp}/nostore",')
                    L.execute(chunk)
                    return h, props

                def tick(h):
                    for e in h["timers"]:
                        if not e["dead"]:
                            e["fn"]()

                def infos(h):
                    return [json.loads(e[3]) for e in h["log"] if e[:3] == ("commandv", "script-message", "thumbfast-info")]

                def overlays(h):
                    return [e for e in h["log"] if e[:2] == ("commandv", "overlay-add")]

                h, props = viewer(link)
                h["ev"]["file-loaded"]()
                check("viewer: a stream starts the live build in the background", len(h["async"]) == 1 and h["async"][0][0][2:4] == ["live", link]
                      and "--parent-pid" in h["async"][0][0] and str(os.getpid()) in h["async"][0][0], h["async"][:1] and h["async"][0][0])
                args, cb = h["async"][0]
                vdir = args[args.index("--out") + 1]
                check("viewer: the folder is under live_dir and named by mpv's pid", vdir.startswith(lbase + "/" + str(os.getpid()) + "-"), vdir)
                check("viewer: until a thumbnail exists the OSC is told none are available", infos(h) and infos(h)[-1]["available"] is False, infos(h))
                h["msg"]["thumb"]("55", "100", "200")
                check("viewer: a hover before anything is built shows nothing", not overlays(h))

                env = dict(os.environ, no_proxy="127.0.0.1,localhost", NO_PROXY="127.0.0.1,localhost")
                r = subprocess.run(args, capture_output=True, text=True, timeout=300, env=env)
                check("viewer: the builder the viewer starts runs to the end", r.returncode == 0, r.stderr[-400:])
                tick(h)
                inf = infos(h)[-1]
                check("viewer: once thumbnails exist the OSC is told their size", inf["available"] is True and inf["width"] == 320 and inf["height"] == 180, inf)
                n_async, n_native = len(h["async"]), len(h["native"])
                h["msg"]["thumb"]("55", "100", "200")
                ov = overlays(h)
                frame = 320 * 180 * 4
                check("viewer: a hover at 55 s shows slot 5 straight from frames.bgra",
                      ov and ov[-1][3:] == ("100", "200", os.path.join(vdir, "frames.bgra"), str(5 * frame), "bgra", "320", "180", "1280"), ov[-1:])
                check("viewer: a hover starts no program", len(h["async"]) == n_async and len(h["native"]) == n_native)

                # only slots 0 and 11 finished: a hover at 55 s (slot 5) gets the nearest finished one (slot 0, 5 away; slot 11 is 6 away)
                dpath = os.path.join(vdir, "done")
                full = open(dpath, "rb").read()
                with open(dpath, "wb") as f:
                    f.write(bytes([1] + [0] * 10 + [1]))
                tick(h)
                h["clock"] += 1                      # the viewer sends at most one picture every 40 ms
                h["msg"]["thumb"]("57", "101", "200")
                ov = overlays(h)
                check("viewer: an unfinished slot shows the nearest finished one", ov and ov[-1][6] == "0", ov[-1:])
                h["clock"] += 1
                h["msg"]["thumb"]("100", "102", "200")
                check("viewer: ...from the other side too", overlays(h)[-1][6] == str(11 * frame), overlays(h)[-1:])
                with open(dpath, "wb") as f:
                    f.write(full)

                cb(True, L.table(status=0))
                check("viewer: the poll stops when the build has ended", all(e["dead"] for e in h["timers"])
                      and any("stream build ended" in " ".join(e) for e in h["log"]), [e for e in h["log"] if e[0] == "info"][-3:])

                link2 = link.replace("/1", "/2")
                props["path"] = link2
                h["ev"]["file-loaded"]()
                check("viewer: the next film stops the build of the one before and starts its own", h["aborted"] == ["tok1"] and len(h["async"]) == 2
                      and h["async"][1][0][3] == link2, (h["aborted"], len(h["async"])))
                h["ev"]["shutdown"]()
                check("viewer: the folders are removed when mpv closes", not os.path.exists(vdir), os.listdir(lbase) if os.path.isdir(lbase) else None)

                h2, _ = viewer(link, live_on=False)
                h2["ev"]["file-loaded"]()
                check("viewer: with live off a stream starts nothing", not h2["async"], h2["async"])
        finally:
            srv.shutdown()
            srv.server_close()
    else:
        print("SKIP live end-to-end tests (no PyAV)")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("FAILED" if fails else "ALL PASSED", fails)
sys.exit(1 if fails else 0)
