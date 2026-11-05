#!/usr/bin/env python3
"""Tests for realdebrid.py and the `source = rd` path in seekthumbs.py.

Everything runs against a local mock of the Real-Debrid REST API and of its direct download URLs: no account, token or
network access is needed. The film used by the end-to-end test is a small synthetic one made with ffmpeg, served with
Range support by the same local server, so the test proves that the bytes really came over the RD link (the mount holds
the same-sized zero-filled placeholder, which ffprobe could never read).
"""
import http.server
import io
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import realdebrid as rd  # noqa: E402
import seekthumbs as st  # noqa: E402

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ((" - " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails += 1


def make_film(path, seconds=40, size="320x180"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=24", "-t", str(seconds),
                    "-g", "24", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", path], check=True)
    old = time.time() - 7200
    os.utime(path, (old, old))


class QuietServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class RdHandler(http.server.BaseHTTPRequestHandler):
    """The small part of the Real-Debrid API these tests use, plus a Range-capable file server."""

    def log_message(self, *a):
        pass

    @property
    def state(self):
        return self.server.state

    def _send(self, code, raw=b"", ctype="application/json", extra=None, head=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            try:
                self.wfile.write(raw)
            except OSError:
                pass

    def _json(self, obj, code=200, extra=None, head=False):
        self._send(code, json.dumps(obj).encode(), "application/json", extra, head)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _api(self):
        self.state["api"] += 1

    def _serve_file(self, name, head=False):
        s = self.state
        if name == "gone":
            self._json({"error": "link expired"}, 410, head=head)
            return
        path = s["files"].get(name)
        if not path or not os.path.isfile(path):
            self._json({"error": "file not found"}, 404, head=head)
            return
        size = os.path.getsize(path)
        rng = self.headers.get("Range") or ""
        lo, hi = 0, size - 1
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            if a:
                lo = int(a)
            if b:
                hi = min(size - 1, int(b))
            if not a and b:
                lo = max(0, size - int(b))
        if lo >= size:
            self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"}, head)
            return
        hi = min(hi, size - 1)
        s["file_hits"] += 1
        if rng:
            s["ranges"].append(rng)
        self.send_response(206 if rng else 200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(hi - lo + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {lo}-{hi}/{size}")
        self.end_headers()
        if not head:
            with open(path, "rb") as f:
                f.seek(lo)
                try:
                    self.wfile.write(f.read(hi - lo + 1))
                except OSError:
                    pass

    def _route(self, head=False):
        u = urllib.parse.urlsplit(self.path)
        path, s = u.path, self.state
        if path.startswith("/file/"):
            self._serve_file(urllib.parse.unquote(path.rsplit("/", 1)[-1]), head=head)
            return
        self._api()
        if path.startswith("/status/"):
            try:
                code = int(path.split("/")[2])
            except (IndexError, ValueError):
                code = 400
            self._json({"error": "mock error"}, code, head=head)
            return
        if path == "/flaky/user":
            s["flaky_calls"] += 1
            if s.get("flaky", 0) > 0:
                s["flaky"] -= 1
                self._json({"error": "too many requests"}, 429, {"Retry-After": "0"}, head)
                return
            self._json(s["user"], head=head)
            return
        if path == "/user":
            self._json(s["user"], head=head)
            return
        if path == "/traffic":
            self._json(s["traffic"], head=head)
            return
        if path == "/torrents":
            q = urllib.parse.parse_qs(u.query)
            page = int(q.get("page", ["1"])[0])
            limit = int(q.get("limit", ["100"])[0])
            self._json(s["torrents"][(page - 1) * limit: page * limit], head=head)
            return
        if path == "/paged/torrents":
            q = urllib.parse.parse_qs(u.query)
            page = int(q.get("page", ["1"])[0])
            limit = int(q.get("limit", ["100"])[0])
            self._json(s["paged"][(page - 1) * limit: page * limit], head=head)
            return
        if path.startswith("/torrents/info/"):
            tid = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            info = s["info"].get(tid)
            if info is None:
                self._json({"error": "torrent not found"}, 404, head=head)
                return
            s["info_calls"][tid] = s["info_calls"].get(tid, 0) + 1
            self._json(info, head=head)
            return
        if path.startswith("/streaming/mediaInfos/"):
            self._json({"duration": 40.0, "nb_streams": 1, "filename": "film.mkv"}, head=head)
            return
        if path.startswith("/streaming/transcode/"):
            self._json({"apple": {"apple": {"id": "a"}}, "dash": {"dash": {"id": "d"}}}, head=head)
            return
        self._json({"error": "not found"}, 404, head=head)

    def do_GET(self):
        self._route(False)

    def do_HEAD(self):
        self._route(True)

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        s = self.state
        self._api()
        if u.path == "/unrestrict/link":
            body = urllib.parse.parse_qs(self._body().decode())
            link = (body.get("link") or [""])[0]
            name = urllib.parse.unquote(urllib.parse.urlsplit(link).path.rsplit("/", 1)[-1]) or "film.mkv"
            s["unrestrict"] += 1
            base = s["base"]
            url = f"{base}/file/{urllib.parse.quote(name)}?n={s['unrestrict']}"
            self._json({"id": "unrestricted-" + str(s["unrestrict"]), "download": url, "filename": name,
                        "filesize": os.path.getsize(s["files"][name]) if name in s["files"] else 0,
                        "host": "real-debrid", "type": "movie", "streamable": 1})
            return
        self._json({"error": "not found"}, 404)


def start_server(tmp):
    state = dict(api=0, file_hits=0, ranges=[], unrestrict=0, flaky=0, flaky_calls=0, info_calls={},
                 user=dict(username="tester", email="t@example.com", type="premium", premium=1, expiration="2030-01-01T00:00:00Z"),
                 traffic={"https://real-debrid.com/": dict(left=100.0, limit=100.0, type="links", extra=0, reset="2030-01-01")},
                 torrents=[], paged=[], info={}, files={}, base="")
    srv = QuietServer(("127.0.0.1", 0), RdHandler)
    srv.state = state
    state["base"] = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


def summary(tid, name="Film", status="downloaded", nbytes=1000, added="2026-01-01T00:00:00Z"):
    return dict(id=str(tid), filename=name, original_filename=name, hash="a" * 40, bytes=nbytes, status=status,
                progress=100, added=added)


def main():
    tmp = tempfile.mkdtemp(prefix="seekthumbs-rd-test-")
    srv, state = start_server(tmp)
    base = state["base"]
    try:
        # ---------------------------------------------------------------- files_from_info
        info = dict(id="1", filename="Film.2020.1080p", original_filename="Film 2020", hash="a" * 40, status="downloaded",
                    files=[dict(id=11, path="Film.2020.1080p.mkv", bytes=1000, selected=1),
                           dict(id=12, path="sample.mkv", bytes=10, selected=0),
                           dict(id=13, path="subs.srt", bytes=5, selected=1)],
                    links=["http://rd.example/link/Film.2020.1080p.mkv", "http://rd.example/link/subs.srt"])
        got = rd.files_from_info(info)
        check("files_from_info pairs links with selected files in order", len(got) == 1 and got[0]["file_id"] == 11
              and got[0]["link"].endswith("Film.2020.1080p.mkv") and got[0]["size"] == 1000, got)
        check("files_from_info leaves out non-video selected files", all(f["file_name"].endswith(".mkv") for f in got))
        rar = dict(info, files=[dict(id=1, path="a.mkv", bytes=1000, selected=1), dict(id=2, path="b.mkv", bytes=900, selected=1)],
                   links=["http://rd.example/link/archive.rar"])
        check("a RAR torrent (one link, several selected files) is skipped", rd.files_from_info(rar) == [])
        small = rd.files_from_info(info, min_size=2000)
        check("min_size filters files by bytes", small == [])

        # ---------------------------------------------------------------- mount names and matching
        item = dict(torrent_filename="Film.2020.1080p", original_filename="Film 2020", hash="b" * 40, file_name="Film.mkv",
                    file_path="Film.mkv")
        cands = rd.mount_candidates(item, "filename")
        check("mount_candidates puts the configured naming first", cands[0] == "Film.2020.1080p/Film.mkv", cands)
        check("mount_candidates tries every Decypharr folder_naming", "Film 2020/Film.mkv" in cands
              and "Film.2020.1080p/Film.mkv" in cands and "b" * 40 + "/Film.mkv" in cands, cands)
        nested = dict(item, file_path="Season 1/Film.mkv")
        check("mount_candidates keeps a nested RD path as a fallback", "Film.2020.1080p/Season 1/Film.mkv" in rd.mount_candidates(nested, "filename"))

        lib = os.path.join(tmp, "lib")
        full = os.path.join(lib, "__all__", "Film.2020.1080p", "Film.mkv")
        os.makedirs(os.path.dirname(full))
        with open(full, "wb") as f:
            f.write(b"x" * 1000)
        check("mount_prefixes finds Decypharr's __all__ folder", rd.mount_prefixes(lib) == ["__all__", ""], rd.mount_prefixes(lib))
        found = rd.find_on_mount(lib, ["Film.2020.1080p/Film.mkv"], 1000)
        check("find_on_mount resolves the __all__ prefix and checks the size",
              found is not None and found[0] == "__all__/Film.2020.1080p/Film.mkv" and found[1].st_size == 1000, found)
        check("find_on_mount refuses a file of another size", rd.find_on_mount(lib, ["Film.2020.1080p/Film.mkv"], 999) is None)
        idx = rd.mount_index(lib, st.VIDEO_EXT, 0)
        check("mount_index keys by normalized name and exact size", idx.get((rd.norm_name("Film.mkv"), 1000)) == ["__all__/Film.2020.1080p/Film.mkv"], idx)

        # ---------------------------------------------------------------- client: auth, pagination, error mapping
        c = rd.RdClient("test-token", base=base, min_interval=0, retries=0, sleep=lambda s: None)
        state["torrents"] = [summary(1), summary(2, status="downloading"), summary(3)]
        state["paged"] = [summary(i) for i in range(1, 6)]
        state["info"] = {"1": info}
        check("the client sends the token and reads /user", c.user().get("username") == "tester")
        check("the client reads /traffic", "https://real-debrid.com/" in c.traffic())
        pages = list(c.torrents(limit=2))
        check("torrents() follows the API's page/limit pagination", [t["id"] for t in pages] == ["1", "2", "3"], pages)
        paged = rd.RdClient("t", base=base + "/paged", min_interval=0, retries=0, sleep=lambda s: None)
        check("...also when the base has a path", [t["id"] for t in paged.torrents(limit=2)] == ["1", "2", "3", "4", "5"])
        check("torrent_info returns the file list", c.torrent_info("1")["files"][0]["id"] == 11)
        try:
            c.torrent_info("missing")
            check("a missing torrent raises RdNotFound", False)
        except rd.RdNotFound:
            check("a missing torrent raises RdNotFound", True)

        for code, cls in ((401, rd.RdAuthError), (403, rd.RdAuthError), (404, rd.RdNotFound), (400, rd.RdBadRequest), (500, rd.RdServerError)):
            bad = rd.RdClient("t", base=f"{base}/status/{code}", min_interval=0, retries=0, sleep=lambda s: None)
            try:
                bad.user()
                check(f"HTTP {code} maps to {cls.__name__}", False)
            except cls:
                check(f"HTTP {code} maps to {cls.__name__}", True)
            except Exception as ex:  # noqa: BLE001
                check(f"HTTP {code} maps to {cls.__name__}", False, type(ex).__name__)

        state["flaky"] = 1
        flaky = rd.RdClient("t", base=base + "/flaky", min_interval=0, retries=2, sleep=lambda s: None)
        check("a 429 with Retry-After is retried, then succeeds", flaky.user().get("username") == "tester" and state["flaky_calls"] == 2,
              state["flaky_calls"])

        # ---------------------------------------------------------------- inventory cache
        state["torrents"] = [summary(1, name="Film.2020.1080p"), summary(2, name="Other", status="downloading")]
        state["info"] = {"1": info}
        cache = os.path.join(tmp, "cache", ".rd-inventory.json")
        inv = rd.Inventory(c, cache, min_size=0, list_ttl=3600, info_ttl=3600, clock=lambda: 1000.0)
        items = inv.items()
        check("Inventory.items returns the selected video files of downloaded torrents", len(items) == 1 and items[0]["file_id"] == 11, items)
        check("Inventory.items reports a RAR/other skip count", inv.stats["downloaded"] == 1 and inv.stats["files"] == 1, inv.stats)
        check("the inventory cache is created mode 0600", oct(os.stat(cache).st_mode & 0o777) == "0o600", oct(os.stat(cache).st_mode & 0o777))
        raw = open(cache).read()
        check("the inventory cache holds links but never the token", "test-token" not in raw and "http://rd.example/link" in raw)
        before = state["api"]
        check("a second items() uses the cache", inv.items() == items and state["api"] == before, (state["api"], before))
        inv2 = rd.Inventory(c, cache, min_size=0, list_ttl=3600, info_ttl=3600, clock=lambda: 1000.0)
        check("a new Inventory reuses the on-disk cache", inv2.items() == items and state["api"] == before, (state["api"], before))
        before = state["api"]
        inv.items(refresh=True)
        check("refresh=True re-reads both the list and the file list", state["api"] == before + 2, (state["api"], before))

        # ---------------------------------------------------------------- link cache
        lc = rd.LinkCache(c, ttl=3600, clock=lambda: 1000.0)
        u1 = lc.get("http://rd.example/link/Film.2020.1080p.mkv")
        u2 = lc.get("http://rd.example/link/Film.2020.1080p.mkv")
        u3 = lc.get("http://rd.example/link/Film.2020.1080p.mkv", force=True)
        check("LinkCache reuses a direct URL until forced", u1 == u2 and u1 != u3 and "n=" in u3, (u1, u2, u3))
        lc.forget("http://rd.example/link/Film.2020.1080p.mkv")
        u4 = lc.get("http://rd.example/link/Film.2020.1080p.mkv")
        check("LinkCache.forget asks again", u4 != u3, (u3, u4))

        # ---------------------------------------------------------------- RangeFile refresh
        served = os.path.join(tmp, "served.mkv")
        with open(served, "wb") as f:
            f.write(bytes(range(256)) * 20)
        state["files"] = {"served.mkv": served}
        refreshed = []

        def refresh():
            refreshed.append(1)
            return base + "/file/served.mkv?fresh=1"

        src = st.HttpSource(base + "/file/gone", refresh=refresh, label="signed link")
        rf = st.RangeFile(src, chunk=4096, timeout=5)
        data = rf.read(512)
        check("RangeFile refreshes an expired direct URL once and reads again", data == open(served, "rb").read(512) and rf.refreshes == 1 and len(refreshed) == 1,
              (len(data), rf.refreshes, len(refreshed)))
        check("a signed link is masked in the log", st.mask_url(src) == "signed link")
        rf.close()
        try:
            st.RangeFile(base + "/file/gone", chunk=4096, timeout=5).read(10)
            check("a 410 without a refresh callback is still an error", False)
        except OSError as ex:
            check("a 410 without a refresh callback is still an error", "410" in str(ex), ex)

        # ---------------------------------------------------------------- end-to-end source=rd
        film = os.path.join(tmp, "source.mkv")
        make_film(film, seconds=40)
        size = os.path.getsize(film)
        name = "Film.2020.1080p.mkv"
        link = "http://rd.example/link/" + name
        info2 = dict(id="7", filename="Film.2020.1080p", original_filename="Film 2020", hash="c" * 40, status="downloaded",
                     added="2026-01-01T00:00:00Z",
                     files=[dict(id=77, path=name, bytes=size, selected=1)], links=[link])
        state["files"] = {name: film}
        state["torrents"] = [summary(7, name="Film.2020.1080p", nbytes=size)]
        state["info"] = {"7": info2}
        state["api"] = state["file_hits"] = state["unrestrict"] = 0
        state["ranges"] = []

        lib2 = os.path.join(tmp, "lib2")
        mount = os.path.join(lib2, "__all__", "Film.2020.1080p", name)
        os.makedirs(os.path.dirname(mount))
        with open(mount, "wb") as f:                   # same size, wrong bytes: only the RD link can be decoded
            f.write(b"\0" * size)
        old = time.time() - 7200
        os.utime(mount, (old, old))
        store2 = os.path.join(tmp, "store2")
        st._RD_STATES.clear()
        cfg = dict(st.DEFAULTS)
        cfg.update(library=lib2, store=store2, source="rd", rd_token="test-token", rd_api_base=base, rd_min_interval=0,
                   rd_timeout=5, rd_cache_ttl=3600, rd_info_ttl=3600, min_size_mb=0, settle=0, interval=10.0,
                   workers=2, yield_to_playback=0, share=0, no_index=0, rate_cooldown=0.2)
        out = io.StringIO()
        olderr, sys.stderr = sys.stderr, out
        try:
            import contextlib
            with contextlib.redirect_stdout(out):
                n = st.scan_pass(cfg)
        finally:
            sys.stderr = olderr
        rel = "__all__/Film.2020.1080p/" + name
        ix = st.read_index(st.entry_dir(store2, rel))
        check("source=rd lists and builds the film", n == 1 and ix is not None, (n, ix, out.getvalue()[-800:]))
        check("the built thumbnails all have bytes", ix is not None and ix["count"] >= 3 and all(l > 0 for l in ix["lengths"]), ix)
        check("the store entry keeps the mount path", os.path.isdir(st.entry_dir(store2, rel)))
        check("the bytes came over the resolved RD link, not the mount", state["file_hits"] > 0 and state["unrestrict"] >= 1,
              (state["file_hits"], state["unrestrict"]))
        check("the RD reads used range requests", any(r.startswith("bytes=") for r in state["ranges"]), state["ranges"][:5])
        check("the signed direct URL is not written to the log", "n=" not in out.getvalue() and "signed link" in out.getvalue(),
              out.getvalue()[-1200:])

        # a naming mismatch is repaired by exact (basename, size), but only when exactly one file matches
        lib3 = os.path.join(tmp, "lib3")
        elsewhere = os.path.join(lib3, "Elsewhere", name)
        os.makedirs(os.path.dirname(elsewhere))
        with open(elsewhere, "wb") as f:
            f.write(b"\0" * size)
        st._RD_STATES.clear()
        cfg3 = dict(cfg, library=lib3, store=os.path.join(tmp, "store3"))
        items3, stats3 = st.rd_state(cfg3).mapped()
        check("a wrong folder name is repaired by exact file name and size",
              len(items3) == 1 and items3[0]["rel"] == "Elsewhere/" + name and stats3["indexed"] == 1, stats3)
        os.makedirs(os.path.join(lib3, "Second"))
        with open(os.path.join(lib3, "Second", name), "wb") as f:
            f.write(b"\0" * size)
        st._RD_STATES.clear()
        items3b, stats3b = st.rd_state(cfg3).mapped()
        check("two same-named, same-sized files are ambiguous, never a guess", items3b == [] and stats3b["ambiguous"] == 1, stats3b)

        # rd_require_mount: a film without a mount file is skipped by default, but kept when asked
        info3 = dict(id="8", filename="Only.On.RD", original_filename="Only On RD", hash="d" * 40, status="downloaded",
                     added="2026-01-01T00:00:00Z", files=[dict(id=88, path="Unmounted.mkv", bytes=1234, selected=1)],
                     links=["http://rd.example/link/Unmounted.mkv"])
        state["torrents"].append(summary(8, name="Only.On.RD", nbytes=1234))
        state["info"]["8"] = info3
        st._RD_STATES.clear()
        items, stats = st.rd_state(cfg).mapped(refresh=True)
        check("rd_require_mount=1 skips an RD film with no mount file", len(items) == 1 and stats["unmatched"] == 1 and stats["skipped"] == 1, stats)
        cfg2 = dict(cfg, rd_require_mount=0)
        items2, stats2 = st.rd_state(cfg2).mapped(refresh=True)
        check("rd_require_mount=0 keeps the expected mount path", len(items2) == 2 and any(i["rel"] == "Only.On.RD/Unmounted.mkv" for i in items2),
              stats2)
        check("the unmatched film is marked as such", stats2["unmatched"] == 1 and stats2["skipped"] == 0, stats2)

        # rd-probe reaches the CLI and reports the account and the match rate
        oldargv, sys.argv = sys.argv, ["seekthumbs.py"]
        out = io.StringIO()
        try:
            import contextlib
            with contextlib.redirect_stdout(out):
                st.cmd_rd_probe(cfg, refresh=True, films=1)
        finally:
            sys.argv = oldargv
        text = out.getvalue()
        check("cmd_rd_probe reports the account, the inventory and a direct link",
              "account: tester" in text and "inventory:" in text and "direct link: Real-Debrid" in text, text[-1500:])
        check("cmd_rd_probe does not print the token or the signed URL", "test-token" not in text and "n=" not in text, text[-800:])
    finally:
        srv.shutdown()
        srv.server_close()
        st._RD_STATES.clear()
        shutil.rmtree(tmp, ignore_errors=True)

    print("FAILED" if fails else "ALL PASSED", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
