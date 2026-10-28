#!/usr/bin/env python3
"""Tests for the seekthumbs web page (seekthumbs_web.py): the guards, the film list, queue / start / pause / resume / stop, and what ends up in the store.
Synthetic films made with ffmpeg; a real HTTP server on 127.0.0.1; no PyAV needed (extractor=auto)."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import seekthumbs as st  # noqa: E402
import seekthumbs_web as web  # noqa: E402

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ((" - " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails += 1


def make_film(path, seconds=60, size="320x180"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=12", "-t", str(seconds), "-g", "120", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", path], check=True)
    old = time.time() - 7200
    os.utime(path, (old, old))


tmp = tempfile.mkdtemp(prefix="seekthumbs-web-")
srv = engine = None
try:
    lib, store = os.path.join(tmp, "lib"), os.path.join(tmp, "store")
    make_film(os.path.join(lib, "Tron.Ares.2025.2160p.Remux", "Tron.Ares.2025.2160p.Remux.mkv"), 60)
    make_film(os.path.join(lib, "Tron.Ares.2025.1080p.WEB-DL", "Tron.Ares.2025.1080p.WEB-DL.mkv"), 60.4, "640x360")
    make_film(os.path.join(lib, "Tron.Ares.2025.IMAX.2160p.WEB-DL", "Tron.Ares.2025.IMAX.2160p.WEB-DL.mkv"), 65, "640x360")     # a different runtime: must not be read in full
    make_film(os.path.join(lib, "Arrival.2016.1080p.BluRay.mkv"), 40)
    make_film(os.path.join(lib, "Show.Name.S01E02.1080p.mkv"), 30)
    cfg = dict(st.DEFAULTS)
    cfg.update(library=lib, store=store, interval=5.0, min_size_mb=0, settle=60, yield_to_playback=0, workers=2, pause=0.0, extractor="auto", rate_cooldown=0.2, skip_tv=1)
    srv, engine, url = web.start(st, cfg, port=18700, token="tok123")
    port = srv.server_address[1]
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def call(path, body=None, token="tok123", host=None, origin=None, ctype="application/json"):
        h = {}
        if token:
            h["X-Token"] = token
        if host:
            h["Host"] = host
        if origin:
            h["Origin"] = origin
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = ctype
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=h), timeout=20) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw)
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def status():
        return call("/api/status")[1]

    def wait(cond, secs=90):
        t0 = time.time()
        while time.time() - t0 < secs:
            s = status()
            if cond(s):
                return s
            time.sleep(0.2)
        return status()

    # ---- the guards
    check("no token: refused", call("/api/status", token=None)[0] == 403)
    check("wrong token: refused", call("/api/status", token="nope")[0] == 403)
    check("a Host header that is not this computer: refused", call("/api/status", host="evil.example")[0] == 403)
    check("POST from another origin: refused", call("/api/control", {"action": "start"}, origin="http://evil.example")[0] == 403)
    check("POST that is not JSON: refused", call("/api/control", {"action": "start"}, ctype="text/plain")[0] == 415)
    code, html = call("/?t=tok123", token=None)
    check("the page is served with the token in the address", code == 200 and b"seekthumbs" in html)
    others = [u for u in re.findall(rb"https?://[^\s\"']+", html) if u != b"http://www.w3.org/2000/svg"]     # that one is an XML namespace name, not a request
    check("the page asks for nothing from other sites", not others, others[:3])
    check("the page without a token is refused", call("/", token=None)[0] == 403)

    # ---- the film list
    code, L = call("/api/library")
    titles = sorted(f["title"] for f in L["films"])
    check("one row per film, releases grouped, TV left out", titles == ["Arrival", "Tron Ares"] and L["hidden"] == 1, (titles, L["hidden"]))
    tron = next(f for f in L["films"] if f["title"] == "Tron Ares")
    check("a film shows its releases, smallest first, with quality tags", len(tron["releases"]) == 3 and "4K" in tron["releases"][0]["tags"] + tron["releases"][1]["tags"], tron["releases"])
    check("nothing is built yet", all(f["status"] == "new" for f in L["films"]) and L["store"]["bytes"] == 0)

    # ---- queue: only ids the server listed are accepted
    check("a path or an unknown id is not accepted", call("/api/queue", {"add": ["/etc/passwd", "ffffffffffff"]})[1]["added"] == 0)
    ids = [f["id"] for f in L["films"]]
    check("queueing two films", call("/api/queue", {"add": ids})[1]["added"] == 2)
    check("the same film is not queued twice", call("/api/queue", {"add": ids})[1]["added"] == 0)
    check("the queue is saved to the store", os.path.exists(os.path.join(store, ".web-queue.json")))
    call("/api/queue", {"remove": [ids[0]]})
    check("remove takes a film out", len(status()["queue"]) == 1)
    call("/api/queue", {"add": [ids[0]], "top": True})
    check("add with top puts it first", status()["queue"][0]["id"] == ids[0])

    # ---- run to the end
    check("start is refused for an unknown action", call("/api/control", {"action": "explode"})[0] == 400)
    call("/api/control", {"action": "start"})
    s = wait(lambda s: s["state"] == "idle" and not s["queue"])
    check("the queue runs to the end and goes idle", s["state"] == "idle" and not s["queue"] and "finished" in s["note"], (s["state"], s["note"]))
    check("both films are built (the second Tron release shares)", s["totals"]["films"] == 2 and s["totals"]["aliases"] == 1, s["totals"])
    check("the history says which was built and which shared", sorted(h["status"] for h in s["history"]) == ["built", "built", "shared", "skipped"], [h["status"] for h in s["history"]])
    sk = [h for h in s["history"] if h["status"] == "skipped"][0]
    check("the release with another runtime was left out, not read (no thumbnails, no failure mark)", sk["thumbs"] == 0 and sk["n"] == 0 and s["totals"]["failed"] == 0 and "IMAX" in sk["rel"], sk)
    check("the log says why it was left out", any("left out" in l and "IMAX" in l for l in call("/api/log?n=200")[1]["lines"]))
    check("thumbnails and bytes are counted", s["totals"]["thumbs"] > 10 and s["totals"]["bytes"] > 1000, s["totals"])
    code, L = call("/api/library")
    check("the list now shows them done, with thumbnail counts", all(f["status"] == "done" and f["thumbs"] for f in L["films"]), [(f["title"], f["status"]) for f in L["films"]])
    check("the store size is reported", L["store"]["bytes"] > 1000 and L["store"]["real"] == 2 and L["store"]["alias"] == 1, L["store"])
    check("the command line still lists the left-out release (it builds a different cut separately; only the page leaves it out)", st.scan_pass(dict(cfg), dry=True) == 1)

    # ---- pause, resume, stop on a slower film
    slow_dir = os.path.join(tmp, "lib", "Slow.Film.2020.1080p.mkv")
    make_film(os.path.join(slow_dir, "Slow.Film.2020.1080p.mkv"), 120)
    engine.settings.update(workers=1, pause=0.25)
    engine.refresh_library()
    slow = next(f for f in call("/api/library")[1]["films"] if f["title"] == "Slow Film")
    call("/api/queue", {"add": [slow["id"]]})
    call("/api/control", {"action": "start"})
    s = wait(lambda s: s["current"] and s["current"]["phase"] == "reading" and s["current"]["ok"] >= 3)
    check("it is reading", s["state"] == "running" and s["current"]["ok"] >= 3, s["current"])
    call("/api/control", {"action": "pause"})
    time.sleep(0.8)
    a = status()["current"]["ok"]
    time.sleep(1.5)
    s = status()
    check("pause stops the reads", s["state"] == "paused" and s["current"]["ok"] <= a + 1 and s["current"]["paused"], (a, s["current"]["ok"]))
    call("/api/control", {"action": "resume"})
    s = wait(lambda s: s["state"] == "running" and s["current"] and s["current"]["ok"] > a + 3, 30)
    check("resume carries on from where it was", s["state"] == "running" and s["current"]["ok"] > a + 3, s["current"])
    call("/api/control", {"action": "stop"})
    s = wait(lambda s: s["state"] == "idle", 30)
    check("stop ends it at once", s["state"] == "idle" and s["current"] is None, (s["state"], s["note"]))
    check("the stopped film is back at the top of the queue, nothing half-built is kept",
          s["queue"] and s["queue"][0]["id"] == slow["id"] and not os.path.exists(os.path.join(store, "Slow.Film.2020.1080p.mkv" + ".seekthumbs", "index.json"))
          and not any(d.startswith(".work-") for dp, dn, fn in os.walk(store) for d in dn), s["queue"])
    check("a stopped film is not counted as built or failed", s["totals"]["films"] == 2 and s["totals"]["failed"] == 0 and s["history"][-1]["status"] == "stopped", s["history"][-1])

    # ---- stop after this film
    engine.settings.update(workers=2, pause=0.0)
    call("/api/control", {"action": "start"})
    call("/api/control", {"action": "stop_after"})
    s = wait(lambda s: s["state"] == "idle", 60)
    check("stop-after finishes the film and then stops", s["state"] == "idle" and s["totals"]["films"] == 3 and "after the film" in s["note"], (s["state"], s["note"], s["totals"]["films"]))

    # ---- the queue survives a restart
    other = [f["id"] for f in call("/api/library")[1]["films"] if f["status"] != "done"]
    check("nothing else to queue (all done)", not other)
    call("/api/queue", {"add": [ids[0]]})
    check("a done film can be queued again (it is skipped when its turn comes)", status()["queue"] and status()["queue"][0]["id"] == ids[0])
    engine.close()
    e2 = web.Engine(st, cfg)
    check("a new engine reads the saved queue back", [q for q in e2.queue] == [ids[0]], e2.queue)
    e2.close()
    # killed mid-film: the file says a film was in hand and the queue no longer holds it -> it comes back at the top
    with open(os.path.join(store, ".web-queue.json"), "w") as f:
        json.dump({"queue": [], "current": ids[1]}, f)
    e3 = web.Engine(st, cfg)
    check("a film that was being read when the page was killed is back at the top of the queue", e3.queue == [ids[1]], e3.queue)
    e3.close()
    with open(os.path.join(store, ".web-queue.json"), "w") as f:
        json.dump({"queue": [ids[0]], "current": ids[0]}, f)
    e4 = web.Engine(st, cfg)
    check("a film in hand that is also queued is not duplicated", e4.queue == [ids[0]], e4.queue)
    e4.close()
    engine = None

    # ---- status and tags helpers
    check("tags: 4K, DV, HDR10+, remux, Atmos", web.file_tags("Tron.Ares.2025.2160p.UHD.Blu-ray.Remux.HDR10+.DoVi.HEVC.TrueHD.7.1.Atmos-HDT.mkv") == ["4K", "DV", "HDR10+", "Remux", "Atmos"])
    check("tags: 1080p BluRay DTS-HD", web.file_tags("Heat.1995.1080p.BluRay.x264.DTS-HD.MA.5.1.mkv") == ["1080p", "BluRay", "DTS-HD"])
    check("title and year come from the release name", web.pretty_title("the imitation game|2014", "x.mkv") == ("The Imitation Game", 2014))
finally:
    if engine:
        engine.close()
    if srv:
        srv.shutdown()
        srv.server_close()
    shutil.rmtree(tmp, ignore_errors=True)

print("FAILED" if fails else "ALL PASSED", fails)
sys.exit(1 if fails else 0)
