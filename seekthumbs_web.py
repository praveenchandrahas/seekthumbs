"""seekthumbs web page: pick films, queue them, start / pause / resume / stop, and watch the speed, the sizes and the progress.

Started by `seekthumbs.py serve`. It listens on 127.0.0.1 only. Every request needs the random token printed at start-up, the Host header must be this
machine, and the page can only queue files that seekthumbs itself lists from the library: no path comes from the browser. It reads the library and writes the
thumbnail store, nothing else.

The build is the same code the command line uses (build_entry); this module only decides which film is next and shows how it is going.
"""
import collections
import hashlib
import http.server
import json
import os
import re
import secrets
import socketserver
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_EVERY = 2.0          # seconds between speed samples
SAMPLES_KEPT = 1800         # one hour


# ---------------------------------------------------------------- what the files are called

def file_tags(name):
    """Short quality labels read from a release name: resolution, HDR kind, source, audio."""
    n = name.lower()
    out = []
    res = next((r for r in ("2160p", "1080p", "720p", "576p", "480p") if r in n), "")
    if res:
        out.append("4K" if res == "2160p" else res)
    elif re.search(r"\buhd\b|\b4k\b", n):
        out.append("4K")
    if re.search(r"dovi|dolby[ ._-]?vision|(?<![a-z0-9])dv(?![a-z0-9])", n):
        out.append("DV")
    if re.search(r"hdr10\+|hdr10plus", n):
        out.append("HDR10+")
    elif re.search(r"hdr", n):
        out.append("HDR")
    if "remux" in n:
        out.append("Remux")
    elif re.search(r"blu[ .-]?ray|bdrip|brrip", n):
        out.append("BluRay")
    elif re.search(r"web[ ._-]?(dl|rip)|(?<![a-z0-9])web(?![a-z0-9])", n):
        out.append("WEB")
    if "atmos" in n:
        out.append("Atmos")
    elif re.search(r"truehd", n):
        out.append("TrueHD")
    elif re.search(r"dts[ .-]?x", n):
        out.append("DTS:X")
    elif re.search(r"dts[ .-]?hd", n):
        out.append("DTS-HD")
    return out


def pretty_title(key, fallback_name):
    """'tron ares|2025' -> ('Tron Ares', 2025). Without a parsable key the file name is cleaned up instead."""
    if key and not key.startswith("\0") and "|" in key:
        t, y = key.rsplit("|", 1)
        return " ".join(w[:1].upper() + w[1:] for w in t.split()), int(y) if y.isdigit() else None
    base = os.path.splitext(os.path.basename(fallback_name))[0]
    base = re.sub(r"[._]+", " ", base).strip()
    m = re.search(r"\b((?:19|20)\d\d)\b", base)
    return (base[: m.start()].strip(" -([") if m and m.start() > 2 else base), int(m.group(1)) if m else None


def library_groups(st, cfg):
    """One group per film: all its releases (the smallest first, the one that gets built). Returns (groups, hidden): `hidden` counts files left out by skip_tv/exclude."""
    items, hidden = [], 0
    for rel, src, size, mtime in st.walk_library(cfg["library"], cfg["min_size_mb"] * 1e6):
        if st.excluded(cfg, rel):
            hidden += 1
            continue
        items.append((mtime, rel, src, size))
    groups = {}
    for it in items:
        k = st.release_key(it[1]) if cfg.get("share", 1) else None
        groups.setdefault(k if k else "\0" + it[1], []).append(it)
    out = []
    for k, members in groups.items():
        members.sort(key=lambda m: (m[3], m[1]))
        title, year = pretty_title(k, members[0][1])
        out.append(dict(id=hashlib.sha1(k.encode()).hexdigest()[:12], key=k, title=title, year=year, members=members, newest=max(m[0] for m in members)))
    out.sort(key=lambda g: (g["title"].lower(), g["year"] or 0))
    return out, hidden


def store_usage(st, store):
    """(real entries, alias entries, bytes of thumbnails and indexes) of the whole store."""
    real = alias = nbytes = 0
    for edir in st.store_entries(store):
        try:
            nbytes += os.path.getsize(os.path.join(edir, "index.json"))
            if os.path.exists(os.path.join(edir, "thumbs.pack")):
                nbytes += os.path.getsize(os.path.join(edir, "thumbs.pack"))
                real += 1
            else:
                alias += 1
        except OSError:
            pass
    return real, alias, nbytes


# ---------------------------------------------------------------- the worker

class Engine:
    """Builds the queued films one after the other, in a thread of this process, and keeps the numbers the page shows."""

    def __init__(self, st, cfg):
        self.st, self.cfg = st, dict(cfg)
        self.cv = threading.Condition(threading.RLock())
        self.state = "idle"                  # idle | running | paused | stopping
        self.note = ""
        self.queue = []                      # group ids, in order
        self.groups, self.hidden, self.scanned_at = {}, 0, 0.0
        self.job, self.job_gid = None, None
        self.history = []
        self.lines = collections.deque(maxlen=400)
        self.totals = dict(films=0, aliases=0, thumbs=0, bytes=0, src_bytes=0, failed=0, slow=0, retried=0, seconds=0.0)
        self.samples = collections.deque(maxlen=SAMPLES_KEPT)
        self.settings = dict(workers=max(1, int(cfg["workers"])), pause=float(cfg["pause"]), yield_to_playback=bool(int(cfg["yield_to_playback"])))
        self.stop_after = False
        self.stop_requested = False
        self.rev = 0
        self._tb, self._tb_rev = 0, -1
        self.retry_once = set()
        self.no_alias = set()                # releases that matched no built release this session: not probed again
        self.bad_streak = 0
        self.closing = False
        self.started = time.time()
        self.run_seconds = 0.0
        self.qfile = os.path.join(cfg["store"], ".web-queue.json")
        self._tee_log()
        self.wake = st.WakeAhead(cfg.get("wake_ahead", 3), float(cfg.get("probe_timeout") or 300.0))
        self.zs = st.have_filter("zscale")
        self.refresh_library()
        self._load_queue()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._sampler, daemon=True).start()

    # -- plumbing
    def _tee_log(self):
        orig = self.st.log

        def tee(*a):
            self.lines.append(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a))
            orig(*a)
        self.st.log = tee
        self._orig_log = orig

    def close(self):
        self.closing = True
        with self.cv:
            if self.job:
                self.job.stop()
            self.cv.notify_all()
        self.wake.close()
        self.st.log = self._orig_log

    def _save_queue(self):
        try:
            tmp = self.qfile + ".tmp"
            os.makedirs(os.path.dirname(self.qfile), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump({"queue": self.queue, "current": self.job_gid}, f)
            os.replace(tmp, self.qfile)
        except OSError:
            pass

    def _load_queue(self):
        try:
            with open(self.qfile) as f:
                saved = json.load(f)
            ids = list(saved.get("queue", []))
            cur = saved.get("current")
            if cur and cur in self.groups and cur not in ids:
                # the film that was being read when the page stopped (killed, rebooted): it goes back to the top, not lost
                ids.insert(0, cur)
                self.st.log(f"RESUMED: {self.groups[cur]['title']} was being read when the page stopped; it is back at the top of the queue")
            self.queue = [i for i in ids if i in self.groups]
        except (OSError, ValueError, AttributeError):
            self.queue = []

    # -- the library and what is done
    def refresh_library(self):
        groups, hidden = library_groups(self.st, self.cfg)
        with self.cv:
            self.groups = {g["id"]: g for g in groups}
            self.hidden, self.scanned_at = hidden, time.time()
            self.queue = [i for i in self.queue if i in self.groups]
            self.rev += 1

    def _members_current(self, g):
        return [self.st.entry_current(self.cfg["store"], rel, size) for _, rel, src, size in g["members"]]

    def library(self):
        """The film list for the page: fixed facts from the last library scan, status read fresh from the store."""
        st, store = self.st, self.cfg["store"]
        state = st.State(store)
        qpos = {gid: i + 1 for i, gid in enumerate(self.queue)}
        films = []
        for g in self.groups.values():
            cur = self._members_current(g)
            status = "done" if any(cur) else "new"        # one set per film: a release that matched no other has none of its own until it is played
            first = g["members"][0]
            failed = None
            for (_, rel, src, size) in g["members"]:
                if state.blocked(rel, size):
                    failed = state.d["fail"][rel].get("why", "")[:160]
                    break
            thumbs = None
            if status != "new":
                r = st.resolve_entry(store, first[1], first[3])
                if r:
                    ix = r[0]
                    thumbs = dict(count=sum(1 for l in ix["lengths"] if l), of=ix["count"], kb=round(sum(ix["lengths"]) / 1000), minutes=round(ix["duration"] / 60),
                                  interval=ix["interval"], w=ix["w"], h=ix["h"], shared=r[2] != first[1])
            films.append(dict(
                id=g["id"], title=g["title"], year=g["year"], status=status, queued=qpos.get(g["id"]), running=g["id"] == self.job_gid, failed=failed,
                releases=[dict(name=os.path.basename(rel), size=size, tags=file_tags(rel), done=cur[i]) for i, (_, rel, src, size) in enumerate(g["members"])],
                size=first[3], newest=g["newest"], thumbs=thumbs))
        real, alias, nbytes = store_usage(st, store)
        return dict(films=films, hidden=self.hidden, scanned_at=self.scanned_at, rev=self.rev,
                    store=dict(real=real, alias=alias, bytes=nbytes, path=store), library=self.cfg["library"])

    # -- queue and controls
    def queue_add(self, ids, top=False):
        with self.cv:
            ids = [i for i in ids if i in self.groups and i not in self.queue and i != self.job_gid]
            self.queue = (ids + self.queue) if top else (self.queue + ids)
            self._save_queue()
            self.rev += 1
            self.cv.notify_all()
            return len(ids)

    def queue_remove(self, ids):
        with self.cv:
            self.queue = [i for i in self.queue if i not in set(ids)]
            self._save_queue()
            self.rev += 1

    def queue_clear(self):
        with self.cv:
            self.queue = []
            self._save_queue()
            self.rev += 1

    def queue_top(self, ids):
        with self.cv:
            ids = [i for i in ids if i in self.queue]
            self.queue = ids + [i for i in self.queue if i not in ids]
            self._save_queue()

    def control(self, action):
        with self.cv:
            if action == "start":
                if self.state == "idle":
                    self.state, self.note, self.stop_after, self.stop_requested = "running", "" if self.queue else "nothing is queued yet", False, False
            elif action == "pause":
                if self.state == "running":
                    self.state = "paused"
                    if self.job:
                        self.job.pause()
            elif action == "resume":
                if self.state == "paused":
                    self.state, self.note = "running", ""
                    if self.job:
                        self.job.resume()
            elif action == "stop":
                if self.job and self.state in ("running", "paused"):
                    self.state, self.stop_requested = "stopping", True
                    self.job.stop()
                elif self.state in ("running", "paused"):
                    self.state = "idle"
            elif action == "stop_after":
                self.stop_after = not self.stop_after
            else:
                return False
            self.cv.notify_all()
            return True

    def set_settings(self, d):
        with self.cv:
            if "workers" in d:
                self.settings["workers"] = max(1, min(16, int(d["workers"])))
            if "pause" in d:
                self.settings["pause"] = max(0.0, min(30.0, float(d["pause"])))
            if "yield_to_playback" in d:
                self.settings["yield_to_playback"] = bool(d["yield_to_playback"])

    # -- the thread that builds
    def _next_group(self):
        st = self.st
        with self.cv:
            while not self.closing:
                if self.state == "running" and self.queue:
                    state = st.State(self.cfg["store"])
                    if state.d["breaker_until"] > time.time():
                        self.state = "idle"
                        self.note = f"the mount pushed back; paused for another {(state.d['breaker_until'] - time.time()) / 60:.0f} min"
                        continue
                    if self.settings["yield_to_playback"] and st.player_running():
                        self.note = "a player is running; waiting"
                        self.cv.wait(5)
                        continue
                    self.note = ""
                    gid = self.queue.pop(0)
                    self.job_gid = gid
                    self._save_queue()                # records the film in hand too, so a kill does not lose it
                    self.rev += 1
                    return gid
                if self.state == "running" and not self.queue:
                    self.state, self.note = "idle", "the queue is finished"
                self.cv.wait(1)
        return None

    def _worker(self):
        while not self.closing:
            gid = self._next_group()
            if gid is None:
                return
            t0 = time.time()
            try:
                self._do_group(self.groups.get(gid), gid)
            except Exception as ex:                    # noqa: BLE001 - the worker must survive anything
                self.st.log(f"web worker: {type(ex).__name__}: {ex}")
            finally:
                with self.cv:
                    self.run_seconds += time.time() - t0
                    self.job, self.job_gid = None, None
                    self._save_queue()
                    if self.state == "stopping":
                        self.state, self.note = "idle", "stopped; the film that was being read was discarded and is back at the top of the queue"
                    elif self.stop_after and self.state in ("running", "paused"):
                        self.state, self.note, self.stop_after = "idle", "stopped after the film, as asked", False
                    self.rev += 1
                    self.cv.notify_all()

    def _next_sources(self, k=3):
        out = []
        for gid in self.queue[:k]:
            g = self.groups.get(gid)
            if g:
                out.append(g["members"][0][2])
        return out

    def _do_group(self, g, gid):
        if g is None:
            return
        st, cfg0 = self.st, self.cfg
        # One set per film: once any release of the film has thumbnails (built, or an alias of one), the other releases may only become aliases.
        # A release whose runtime matches nothing is left alone (it is built when you play it), never read in full a second time.
        covered = any(st.entry_current(cfg0["store"], rel, size) for (_, rel, _s, size) in g["members"])
        for (mtime, rel, src, size) in g["members"]:
            if self.closing or self.stop_requested:
                return
            if st.entry_current(cfg0["store"], rel, size):
                continue
            if rel in self.no_alias:
                continue
            if time.time() - mtime < cfg0["settle"]:
                st.log(f"skipped {rel}: modified {time.time() - mtime:.0f} s ago, it may still be downloading")
                continue
            lk = st.film_lock(cfg0["store"], rel)
            if lk is None:
                st.log(f"{rel} is being built by another seekthumbs; skipping")
                continue
            job = st.Job(rel, g["title"])
            with self.cv:
                if self.state == "paused":
                    job.pause()
                self.job = job
                settings = dict(self.settings)
            self.wake.ahead(self._next_sources())
            cfg = dict(cfg0)
            cfg.update(workers=settings["workers"], pause=settings["pause"], _job=job, _alias_only=covered)
            gov = st.Governor(settings["workers"], cfg0["rate_cooldown"])
            state = st.State(cfg0["store"])
            t0 = time.time()
            rec = dict(title=g["title"], year=g["year"], rel=rel, at=t0, src_gb=round(size / 1e9, 1))
            try:
                st.build_entry(rel, src, size, cfg, gov, self.zs)
                state.ok(rel)
                self.bad_streak = 0
                rec.update(status="shared" if job.shared else "built")
                covered = True
            except st.NoMatchingRelease as ex:
                self.no_alias.add(rel)
                st.log(f"left out {rel}: {ex}; this film already has thumbnails, so this release gets its own set only when you play it")
                rec.update(status="skipped", why=str(ex)[:160])
            except st.JobStopped:
                rec.update(status="stopped")
                if not self.closing:
                    with self.cv:
                        self.queue.insert(0, gid)              # stopped is not removed: it waits at the top for the next Start
                        self._save_queue()
            except st.ProbeTimeout as ex:
                if rel not in self.retry_once:
                    self.retry_once.add(rel)
                    st.log(f"SLOW TO OPEN {rel}: {ex}; it goes to the back of the queue (not a failure)")
                    with self.cv:
                        self.queue.append(gid)
                        self._save_queue()
                    rec.update(status="slow")
                    self.totals["slow"] += 1
                else:
                    st.log(f"FAILED {rel}: still not opening on the second try: {ex}")
                    state.failed(rel, size, str(ex))
                    rec.update(status="failed")
                    self.totals["failed"] += 1
            except st.Throttled as ex:
                st.log(f"STOPPING: {ex}")
                state.d["breaker_until"] = time.time() + cfg0["breaker_hours"] * 3600
                state.save()
                with self.cv:
                    self.queue.insert(0, gid)
                    self._save_queue()
                    self.state, self.note = "idle", "the mount keeps rate-limiting; stopped (it will not be marked as failed)"
                rec.update(status="throttled")
            except Exception as ex:                    # noqa: BLE001
                st.log(f"FAILED {rel}: {ex}")
                state.failed(rel, size, str(ex))
                rec.update(status="failed", why=str(ex)[:160])
                self.totals["failed"] += 1
                if isinstance(ex, st.ReadFailures):
                    self.bad_streak += 1
                    if self.bad_streak >= 3:
                        state.d["breaker_until"] = time.time() + cfg0["breaker_hours"] * 3600
                        state.save()
                        with self.cv:
                            self.state, self.note = "idle", "three films in a row could not be read; stopped (mount trouble?)"
            finally:
                lk.close()
                secs = time.time() - t0
                s = job.stats
                rec.update(seconds=round(secs, 1), wake=round(job.t_wake, 1), probe=round(job.t_probe, 1), thumbs=s["ok"], n=job.n, fail=s["fail"],
                           retried=s["retried"], mb=round(job.bytes / 1e6, 2))
                with self.cv:
                    self.history.append(rec)
                    del self.history[:-200]
                    if rec["status"] == "built":
                        t = self.totals
                        t["films"] += 1
                        t["thumbs"] += s["ok"]
                        t["bytes"] += job.bytes
                        t["src_bytes"] += size
                        t["retried"] += s["retried"]
                        t["seconds"] += secs
                    elif rec["status"] == "shared":
                        self.totals["aliases"] += 1
                    self.job = None
                    self.rev += 1
            if rec["status"] in ("stopped", "throttled") or self.state == "idle":
                return

    def _sampler(self):
        while not self.closing:
            with self.cv:
                j = self.job
                thumbs = self.totals["thumbs"] + (j.stats["ok"] if j and j.phase == "reading" else 0)
            self.samples.append((time.time(), thumbs, self.st.net_rx_bytes()))
            time.sleep(SAMPLE_EVERY)

    # -- numbers for the page
    def _series(self, window=10.0, points=240):
        s = list(self.samples)
        if len(s) < 2:
            return dict(t=[], rate=[], net=[])
        step = max(1, len(s) // points)
        t, rate, net = [], [], []
        j = 0
        for i in range(1, len(s), step):
            while s[i][0] - s[j][0] > window and j < i - 1:
                j += 1
            dt = max(0.001, s[i][0] - s[j][0])
            t.append(round(s[i][0], 1))
            rate.append(round(max(0.0, (s[i][1] - s[j][1]) / dt), 3))
            net.append(round(max(0.0, ((s[i][2] or 0) - (s[j][2] or 0)) / dt / 1e6), 3) if s[i][2] is not None and s[j][2] is not None else None)
        return dict(t=t, rate=rate, net=net)

    def status(self):
        now = time.time()
        with self.cv:
            j, st_ = self.job, self.st
            cur = None
            ser = self._series()
            if j:
                rate = None
                if j.phase == "reading" and len(self.samples) > 3:
                    base = [x for x in self.samples if now - x[0] <= 30]
                    if len(base) >= 2 and base[-1][0] > base[0][0]:
                        rate = (base[-1][1] - base[0][1]) / (base[-1][0] - base[0][0])
                cur = dict(id=self.job_gid, title=j.title, rel=j.rel, phase=j.phase, paused=j.paused, n=j.n, ok=j.stats["ok"], fail=j.stats["fail"],
                           retried=j.stats["retried"], suspect=j.stats["suspect"], elapsed=round(now - j.t0, 1), wake=round(j.t_wake, 1), probe=round(j.t_probe, 1),
                           read_elapsed=round(now - j.t_read0, 1) if j.t_read0 else None, rate=round(rate, 3) if rate is not None else None,
                           left=(round((j.n - j.stats["ok"]) / rate) if rate and rate > 0 and j.n else None))
            built = [h for h in self.history if h["status"] == "built"]
            avg = (sum(h["seconds"] for h in built[-8:]) / len(built[-8:])) if built else None
            if self._tb_rev != self.rev:                       # the store only changes when a film ends, which bumps rev: do not re-read it every second
                self._tb = sum(1 for gid in self.queue if gid in self.groups and not any(self._members_current(self.groups[gid])))
                self._tb_rev = self.rev
            to_build = self._tb
            eta = None
            if avg is not None:
                eta = avg * to_build + (cur["left"] if cur and cur["left"] else 0)
            return dict(state=self.state, note=self.note, stop_after=self.stop_after, rev=self.rev, now=now, current=cur,
                        queue=[dict(id=i, title=self.groups[i]["title"], year=self.groups[i]["year"], size=self.groups[i]["members"][0][3]) for i in self.queue if i in self.groups],
                        totals=dict(self.totals, avg_seconds=avg, eta_seconds=eta, to_build=to_build, run_seconds=self.run_seconds),
                        history=self.history[-60:], series=ser, settings=dict(self.settings),
                        interval=self.cfg["interval"])


# ---------------------------------------------------------------- the server

def make_handler(engine, token, allowed_hosts):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "seekthumbs"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype == "application/json" else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, query):
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(403, {"error": "bad Host header"})
                return False
            given = self.headers.get("X-Token") or (query.get("t", [""])[0] if self.command == "GET" and self.path.split("?")[0] == "/" else "")
            if not given or not secrets.compare_digest(given, token):
                self._send(403, {"error": "missing or wrong token: open the address that `seekthumbs.py serve` printed"})
                return False
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if origin and origin.split("://", 1)[-1] not in allowed_hosts:
                    self._send(403, {"error": "bad Origin"})
                    return False
                if "application/json" not in self.headers.get("Content-Type", ""):
                    self._send(415, {"error": "JSON only"})
                    return False
            return True

        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._guard(q):
                return
            try:
                if u.path == "/":
                    with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                        self._send(200, f.read(), "text/html")
                elif u.path == "/api/library":
                    if q.get("refresh"):
                        engine.refresh_library()
                    self._send(200, engine.library())
                elif u.path == "/api/status":
                    self._send(200, engine.status())
                elif u.path == "/api/log":
                    self._send(200, {"lines": list(engine.lines)[-int(q.get("n", ["120"])[0]):]})
                else:
                    self._send(404, {"error": "not found"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as ex:                    # noqa: BLE001
                self._send(500, {"error": f"{type(ex).__name__}: {ex}"})

        def do_POST(self):
            if not self._guard({}):
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n > 65536:
                    self._send(413, {"error": "too large"})
                    return
                body = json.loads(self.rfile.read(n) or b"{}")
                p = self.path
                if p == "/api/queue":
                    added = 0
                    if body.get("clear"):
                        engine.queue_clear()
                    if body.get("remove"):
                        engine.queue_remove(list(map(str, body["remove"])))
                    if body.get("add"):
                        added = engine.queue_add(list(map(str, body["add"])), top=bool(body.get("top")))
                    if body.get("top_ids"):
                        engine.queue_top(list(map(str, body["top_ids"])))
                    self._send(200, {"ok": True, "added": added})
                elif p == "/api/control":
                    ok = engine.control(str(body.get("action", "")))
                    self._send(200 if ok else 400, {"ok": ok})
                elif p == "/api/settings":
                    engine.set_settings(body)
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": "not found"})
            except (ValueError, TypeError) as ex:
                self._send(400, {"error": str(ex)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as ex:                    # noqa: BLE001
                self._send(500, {"error": f"{type(ex).__name__}: {ex}"})

    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start(st, cfg, port=8781, token=None):
    """Starts the engine and the HTTP server (not yet serving). Returns (server, engine, url)."""
    engine = Engine(st, cfg)
    token = token or secrets.token_urlsafe(18)
    last = None
    for p in range(port, port + 20):
        try:
            hosts = {f"127.0.0.1:{p}", f"localhost:{p}", f"[::1]:{p}"}
            srv = Server(("127.0.0.1", p), make_handler(engine, token, hosts))
            return srv, engine, f"http://127.0.0.1:{p}/?t={token}"
        except OSError as ex:
            last = ex
    engine.close()
    raise last


def serve(st, cfg, port=8781, open_browser=False):
    srv, engine, url = start(st, cfg, port)
    st.log("seekthumbs page:", url)
    print("\nOpen this address in your browser (it carries a one-time token; the page listens on this computer only):\n  " + url + "\n", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:                              # noqa: BLE001
            pass
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        while t.is_alive() and not st.STOP:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    st.log("closing the page; the film being read (if any) is discarded")
    engine.close()
    srv.shutdown()
    srv.server_close()
    return 0
