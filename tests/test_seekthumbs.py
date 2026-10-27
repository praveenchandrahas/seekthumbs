#!/usr/bin/env python3
"""Tests for tools/seekthumbs: generator (synthetic films made with ffmpeg) and the mpv viewer (Lua run under lupa with a mocked mp API).
Needs ffmpeg and, for the viewer part, `pip install lupa` (skipped with a note if missing). Does not need mpv."""
import contextlib
try:
    import av as _av_probe  # noqa: F401
    HAVE_AV = True
except Exception:
    HAVE_AV = False
EXTRACTOR_ARGS = [] if HAVE_AV else ["--play-extractor", "auto"]   # CI runners have no PyAV: allow the ffmpeg fallback in the tests that are not about that rule
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..")
sys.path.insert(0, TOOL)
import seekthumbs as st  # noqa: E402

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ((" - " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails += 1


def make_film(path, seconds=130, size="1280x720", src="testsrc2", gop=48):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{src}=s={size}:r=24", "-t", str(seconds), "-g", str(gop),
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", path], check=True)
    old = time.time() - 7200
    os.utime(path, (old, old))


def cfg(lib, store, **kw):
    c = dict(st.DEFAULTS)
    c.update(library=lib, store=store, pause=0, film_pause=0, interval=10.0, min_size_mb=0, settle=60, yield_to_playback=0, workers=4, rate_cooldown=0.2)
    c.update(kw)
    return c


tmp = tempfile.mkdtemp(prefix="seekthumbs-test-")
lib, store = os.path.join(tmp, "lib"), os.path.join(tmp, "store")
try:
    film = os.path.join(lib, "Movies", "A Film (2020)", "A.Film.2020.mkv")
    make_film(film)
    c = cfg(lib, store)

    # --- plan finds it, scan builds it, second plan finds nothing
    check("plan finds the new film", st.scan_pass(c, dry=True) == 1)
    check("scan builds one film", st.scan_pass(c) == 1)
    edir = st.entry_dir(store, os.path.relpath(film, lib))
    ix = st.read_index(edir)
    check("entry mirrors the library path", ix is not None and os.path.isfile(os.path.join(edir, "thumbs.pack")))
    check("thumbnail count = duration // interval", ix and ix["count"] == 13, ix and ix["count"])
    check("all lengths non-zero, offsets contiguous",
          ix and all(l > 0 for l in ix["lengths"]) and all(ix["offsets"][i + 1] == ix["offsets"][i] + ix["lengths"][i] for i in range(ix["count"] - 1)))
    with open(os.path.join(edir, "thumbs.pack"), "rb") as f:
        f.seek(ix["offsets"][5])
        check("a thumbnail starts with the JPEG marker", f.read(2) == b"\xff\xd8")
    check("nothing left to do after the build", st.scan_pass(c, dry=True) == 0)
    check("verify passes", st.cmd_verify(c) == 0)

    # --- size budget: a tiny budget forces the ladder down but never above the allowance (or the ladder's floor)
    tight = cfg(lib, os.path.join(tmp, "store2"), budget_mb=0.02)
    st.scan_pass(tight)
    ix2 = st.read_index(st.entry_dir(tight["store"], os.path.relpath(film, lib)))
    sz2 = sum(os.path.getsize(os.path.join(st.entry_dir(tight["store"], os.path.relpath(film, lib)), f)) for f in ("thumbs.pack",))
    check("tight budget lowers quality or width", ix2 and (ix2["q"] > ix["q"] or ix2["w"] < ix["w"]), (ix2 and (ix2["q"], ix2["w"])))
    check("tight budget gives a smaller pack", sz2 < os.path.getsize(os.path.join(edir, "thumbs.pack")))

    # --- a changed file (new size) is rebuilt; a settle window skips fresh files
    make_film(film, seconds=140)
    check("a film whose size changed is queued again", st.scan_pass(c, dry=True) == 1)
    new = os.path.join(lib, "Fresh.mkv")
    make_film(new, seconds=60)
    os.utime(new, None)
    check("a file modified just now is skipped (settle)", st.scan_pass(c, dry=True) == 1)
    os.utime(new, (time.time() - 7200,) * 2)
    st.scan_pass(c)
    check("both are built after the settle window", st.scan_pass(c, dry=True) == 0)

    # --- failures: a corrupt file is recorded with a backoff and does not stop the others
    bad = os.path.join(lib, "Broken.mkv")
    with open(bad, "wb") as f:
        f.write(b"not a video" * 1000)
    os.utime(bad, (time.time() - 7200,) * 2)
    st.scan_pass(c)
    state = st.State(store)
    check("corrupt file is recorded as failed", "Broken.mkv" in state.d["fail"])
    check("failed file is not retried at once", st.scan_pass(c, dry=True) == 0)

    # --- prune: orphan removal, and refusal when the library looks unmounted
    os.remove(new)
    out = subprocess.run([sys.executable, os.path.join(TOOL, "seekthumbs.py"), "--library", lib, "--store", store, "--min-size-mb", "0", "prune"],
                         capture_output=True, text=True).stdout
    check("prune lists the orphan but deletes nothing without --yes", "Fresh.mkv" in out and os.path.isdir(st.entry_dir(store, "Fresh.mkv")))
    empty = os.path.join(tmp, "emptylib")
    os.makedirs(empty)
    r = subprocess.run([sys.executable, os.path.join(TOOL, "seekthumbs.py"), "--library", empty, "--store", store, "prune", "--yes"],
                       capture_output=True, text=True)
    check("prune refuses when the library is empty", r.returncode != 0 and os.path.isdir(st.entry_dir(store, "Fresh.mkv")))
    check("scan does nothing when the library is empty", st.scan_pass(cfg(empty, store)) == 0)


    # --- the governor and the parallel build
    import threading
    g = st.Governor(4, 0.05)
    for _ in range(4):
        g.acquire()
    g.release("throttled")
    check("a rate-limit signal halves the parallel reads", g.limit == 2, g.limit)
    g.release("throttled")
    check("a burst of signals acts only once", g.limit == 2 and g.throttles == 2)
    g.release("ok"); g.release("ok")
    time.sleep(0.2)
    for _ in range(45):
        g.acquire(); g.release("ok")
    check("clean reads raise the limit again", g.limit >= 3, g.limit)

    rel2 = os.path.relpath(film, lib)
    gov = st.Governor(4, 0.05)
    cfg2 = cfg(lib, os.path.join(tmp, "store3"), extractor="ffmpeg")
    orig = st.CliExtractor.grab
    calls = {"n": 0}
    cl = threading.Lock()

    def flaky(self, t, out):
        with cl:
            calls["n"] += 1
            k = calls["n"]
        if k <= 6:
            return False, "HTTP error 429 Too Many Requests"
        return orig(self, t, out)

    st.CliExtractor.grab = flaky
    try:
        ok_ = st.build_entry(rel2, film, os.path.getsize(film), cfg2, gov, False)
    finally:
        st.CliExtractor.grab = orig
    ix3 = st.read_index(st.entry_dir(cfg2["store"], rel2))
    check("the build survives a burst of 429s and still reads every thumbnail", ok_ and ix3 and all(l > 0 for l in ix3["lengths"]), calls)
    check("the governor recorded the rate-limit signals and backed off", gov.throttles >= 1)

    st.CliExtractor.grab = lambda self, t, out: (False, "HTTP error 429 Too Many Requests")
    try:
        try:
            st.build_entry("x/" + rel2, film, os.path.getsize(film), cfg(lib, os.path.join(tmp, "store4"), extractor="ffmpeg", rate_cooldown=0.00005),
                           st.Governor(2, 0.00005), False)
            raised = None
        except st.Throttled as ex:
            raised = "Throttled"
        except Exception as ex:
            raised = type(ex).__name__
    finally:
        st.CliExtractor.grab = orig
    check("endless 429s end in Throttled (film not marked as bad)", raised == "Throttled", raised)

    st.CliExtractor.grab = lambda self, t, out: (False, "Invalid data found when processing input")
    try:
        try:
            st.build_entry("y/" + rel2, film, os.path.getsize(film), cfg(lib, os.path.join(tmp, "store5"), extractor="ffmpeg"), st.Governor(2, 0.01), False)
            raised = None
        except st.ReadFailures:
            raised = "ReadFailures"
        except Exception as ex:
            raised = type(ex).__name__
    finally:
        st.CliExtractor.grab = orig
    check("plain read failures end in ReadFailures", raised == "ReadFailures", raised)

    try:
        import av  # noqa: F401
        have_av = True
    except ImportError:
        have_av = False
        print("SKIP PyAV tests (pip install av)")
    if have_av:
        cfg6 = cfg(lib, os.path.join(tmp, "store6"), extractor="pyav")
        okv = st.build_entry(rel2, film, os.path.getsize(film), cfg6, st.Governor(4, 0.05), False)
        ixv = st.read_index(st.entry_dir(cfg6["store"], rel2))
        check("PyAV extractor builds the same number of thumbnails", okv and ixv and ixv["count"] == ix3["count"] and all(l > 0 for l in ixv["lengths"]))
        with open(os.path.join(st.entry_dir(cfg6["store"], rel2), "thumbs.pack"), "rb") as f:
            f.seek(ixv["offsets"][3])
            check("PyAV thumbnails are JPEGs", f.read(2) == b"\xff\xd8")
        # HDR-tagged film: the pip wheel has no zscale, so the PyAV path must fall back to the ffmpeg command and still finish
        hdr = os.path.join(lib, "HDR.Film.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=24", "-t", "60", "-g", "48", "-c:v", "libx264", "-preset",
                        "ultrafast", "-pix_fmt", "yuv420p10le", "-color_trc", "smpte2084", "-color_primaries", "bt2020", "-colorspace", "bt2020nc", hdr], check=True)
        cfg7 = cfg(lib, os.path.join(tmp, "store7"), extractor="auto")
        okh = st.build_entry("HDR.Film.mkv", hdr, os.path.getsize(hdr), cfg7, st.Governor(4, 0.05), st.have_filter("zscale"))
        check("an HDR film builds under extractor=auto (falls back when PyAV lacks zscale)", okh and st.read_index(st.entry_dir(cfg7["store"], "HDR.Film.mkv")))


    # ================================================================ garbage decodes (seen on a rate-limited mount: errors printed, exit code 0, a grey frame)
    import re as _re
    check("decode-error lines are recognised", all(st.decode_error_in(x) for x in (
        "[hevc @ 0x55] Invalid NAL unit size (2147483647 > 10650).", "Error splitting the input into NAL units.", "Invalid data found when processing input",
        "Could not find ref with POC 12", "corrupt decoded frame", "missing reference picture", "Read error at pos. 123")), "")
    check("ordinary ffmpeg lines are not decode errors", not any(st.decode_error_in(x) for x in (
        "", "Changing video frame properties on the fly is not supported by all filters", "deprecated pixel format used", "Past duration 0.99 too large")))

    def ppm(path, fill=None, rnd=False, black_with_dots=False, w=64, h=36):
        import random as _r
        n = w * h * 3
        data = bytes(_r.randrange(256) for _ in range(n)) if rnd else (bytes([0] * n) if black_with_dots else bytes([fill] * n))
        if black_with_dots:
            b = bytearray(data)
            for i in range(0, 60, 3):
                b[i * 50: i * 50 + 3] = b"\xff\xff\xff"
            data = bytes(b)
        with open(path, "wb") as f:
            f.write(b"P6\n%d %d\n255\n" % (w, h) + data)
        return path

    pp = os.path.join(tmp, "f.ppm")
    check("a mid-grey frame is suspicious", st.suspicious_frame(ppm(pp, 128)))
    check("a white frame is suspicious", st.suspicious_frame(ppm(pp, 255)))
    check("a black frame (credits) is not", not st.suspicious_frame(ppm(pp, 0)))
    check("a dark grey frame (fade) is not", not st.suspicious_frame(ppm(pp, 40)))
    check("black with a few white pixels (credits text) is not", not st.suspicious_frame(ppm(pp, black_with_dots=True)))
    check("a busy picture is not", not st.suspicious_frame(ppm(pp, rnd=True)))
    check("only PPM frames are looked at", not st.suspicious_frame(os.path.join(tmp, "x.png")))

    def build_logged(rel, cfg_, src=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok_ = st.build_entry(rel, src or film, os.path.getsize(src or film), cfg_, st.Governor(4, 0.05), False)
        m = _re.search(r"(\d+) reads retried after decode errors, (\d+) near-constant", buf.getvalue())
        return ok_, (int(m.group(1)), int(m.group(2))) if m else None, buf.getvalue()

    # the ffmpeg-command path: exit code 0 and a frame, but decode errors on stderr
    real_run = subprocess.run
    inj = {"n": 0}

    def run_noisy(cmd, *a, **k):
        r = real_run(cmd, *a, **k)
        if isinstance(cmd, list) and "-noaccurate_seek" in cmd and inj["n"] < 3:
            inj["n"] += 1
            r = subprocess.CompletedProcess(r.args, r.returncode, r.stdout, (r.stderr or "") +
                                            "[hevc @ 0x1] Invalid NAL unit size (2147483647 > 10650).\n[hevc @ 0x1] Error splitting the input into NAL units.\n")
        return r

    subprocess.run = run_noisy
    try:
        ok_, counts, out_ = build_logged(rel2, cfg(lib, os.path.join(tmp, "store8"), extractor="ffmpeg"))
    finally:
        subprocess.run = real_run
    ix8 = st.read_index(st.entry_dir(os.path.join(tmp, "store8"), rel2))
    check("ffmpeg command: three reads with decode errors are retried and the film is complete", ok_ and counts and counts[0] == 3 and ix8 and all(l > 0 for l in ix8["lengths"]), (counts, out_[-300:]))

    if have_av:
        import av as _av
        orig_g = st.AvExtractor._grab
        seen_t, inj2 = set(), {"n": 0}

        def noisy_grab(self, t, out):
            if t not in seen_t and inj2["n"] < 4:
                inj2["n"] += 1
                _av.logging.log(_av.logging.ERROR, "hevc", "Invalid NAL unit size (2147483647 > 10650).")
            seen_t.add(t)
            return orig_g(self, t, out)

        st.AvExtractor._grab = noisy_grab
        try:
            ok_, counts, out_ = build_logged(rel2, cfg(lib, os.path.join(tmp, "store9"), extractor="pyav"))
        finally:
            st.AvExtractor._grab = orig_g
        ix9 = st.read_index(st.entry_dir(os.path.join(tmp, "store9"), rel2))
        check("PyAV: libav log lines with decode errors make the read fail, it is retried once, the film is complete",
              ok_ and counts and counts[0] == 4 and ix9 and all(l > 0 for l in ix9["lengths"]), (counts, out_[-300:]))

        # extractor=auto must not answer a decode error with the dearer ffmpeg command
        used_cli = []
        orig_cg = st.CliExtractor.grab
        st.CliExtractor.grab = lambda self, t_, out: used_cli.append(1) or orig_cg(self, t_, out)
        seen_t.clear(); inj2["n"] = 0
        st.AvExtractor._grab = noisy_grab
        try:
            ok_, counts, out_ = build_logged(rel2, cfg(lib, os.path.join(tmp, "store10"), extractor="auto"))
        finally:
            st.AvExtractor._grab = orig_g
            st.CliExtractor.grab = orig_cg
        check("extractor=auto retries a decode error with PyAV (no fall-through to one ffmpeg process per thumbnail)", ok_ and counts and counts[0] == 4 and not used_cli, (counts, used_cli))

        # a grey frame once: read again; the same white frame every time is kept (a real white-out)
        def grey_first(self, t, out):
            if t not in seen_t:
                seen_t.add(t)
                ppm(out, 128, w=64, h=36)
                return True, ""
            return orig_g(self, t, out)

        seen_t.clear()
        st.AvExtractor._grab = grey_first
        try:
            ok_, counts, out_ = build_logged(rel2, cfg(lib, os.path.join(tmp, "store11"), extractor="pyav"))
        finally:
            st.AvExtractor._grab = orig_g
        check("a grey frame is read again and replaced by the real picture", ok_ and counts and counts[1] == 14, (counts, out_[-200:]))
        st.AvExtractor._grab = lambda self, t, out: (ppm(out, 255, w=64, h=36), (True, ""))[1]
        try:
            ok_, counts, out_ = build_logged(rel2, cfg(lib, os.path.join(tmp, "store12"), extractor="pyav"))
        finally:
            st.AvExtractor._grab = orig_g
        check("the same white frame twice is kept (it is real), not retried forever", ok_ and counts and counts[1] == 14, (counts, out_[-200:]))

    # a real corrupt file: a bad NAL length in one keyframe (HEVC in mp4)
    has_x265 = "libx265" in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if has_x265:
        cor = os.path.join(tmp, "corlib", "Corrupt.One.2022.mp4")
        os.makedirs(os.path.dirname(cor))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=24", "-t", "60", "-c:v", "libx265", "-preset", "ultrafast",
                        "-x265-params", "keyint=24:min-keyint=24:log-level=none", "-pix_fmt", "yuv420p", cor], check=True)
        pk = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pos,pts_time,flags", "-of", "json", cor],
                                       capture_output=True, text=True).stdout)["packets"]
        kp = [p_ for p_ in pk if "K" in p_["flags"] and abs(float(p_["pts_time"]) - 35.0) < 0.01][0]
        raw = bytearray(open(cor, "rb").read())
        raw[int(kp["pos"]):int(kp["pos"]) + 4] = bytes([0x7f, 0xff, 0xff, 0xff])
        open(cor, "wb").write(raw)
        for ex_name in ("ffmpeg",) + (("pyav",) if have_av else ()):
            ok_, counts, out_ = build_logged("Corrupt.One.2022.mp4", cfg(lib, os.path.join(tmp, "store13" + ex_name), extractor=ex_name), src=cor)
            ixc = st.read_index(st.entry_dir(os.path.join(tmp, "store13" + ex_name), "Corrupt.One.2022.mp4"))
            check(f"a corrupt keyframe ({ex_name}): the read is retried, then left out; the other thumbnails are good",
                  ok_ and counts and counts[0] >= 1 and ixc and ixc["lengths"][3] == 0 and sum(1 for l in ixc["lengths"] if l == 0) == 1, (counts, ixc and ixc["lengths"], out_[-300:]))
    else:
        print("SKIP corrupt-keyframe test (ffmpeg without libx265)")

    # verify --deep
    cd = cfg(lib, os.path.join(tmp, "store14"), extractor="ffmpeg")
    st.build_entry(rel2, film, os.path.getsize(film), cd, st.Governor(4, 0.05), False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc0 = st.cmd_verify(cd, deep=True)
    check("verify --deep accepts a good entry", rc0 == 0, buf.getvalue())
    edir14 = st.entry_dir(cd["store"], rel2)
    ix14 = st.read_index(edir14)
    wd, hd = ix14["w"], ix14["h"]
    flat = {}
    for nm, colour in (("grey", "gray"), ("black", "black")):
        fp = os.path.join(tmp, f"flat-{nm}.jpg")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s={wd}x{hd}", "-frames:v", "1", "-pix_fmt", "yuvj420p", fp], check=True)
        flat[nm] = open(fp, "rb").read()
    pack = open(os.path.join(edir14, "thumbs.pack"), "rb").read()
    parts = [pack[o:o + l] for o, l in zip(ix14["offsets"], ix14["lengths"])]
    parts[2], parts[4] = flat["grey"], flat["black"]
    offs, pos = [], 0
    for b_ in parts:
        offs.append(pos); pos += len(b_)
    open(os.path.join(edir14, "thumbs.pack"), "wb").write(b"".join(parts))
    ix14.update(offsets=offs, lengths=[len(b_) for b_ in parts])
    json.dump(ix14, open(os.path.join(edir14, "index.json"), "w"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc1 = st.cmd_verify(cd, deep=True)
    check("verify --deep lists a flat grey thumbnail (and not a black one)", rc1 == 1 and "thumbnail 2 " in buf.getvalue() and "thumbnail 4 " not in buf.getvalue(), buf.getvalue())
    check("plain verify does not decode and still passes", st.cmd_verify(cd) == 0)

    # ================================================================ on-play build and sharing between releases
    cases = {
        "Movie.Name.2019.2160p.UHD.BluRay.x265-GRP.mkv": "movie name|2019",
        "Movie Name (2019) [1080p].mkv": "movie name|2019",
        "Blade.Runner.2049.2017.2160p.WEB-DL.mkv": "blade runner 2049|2017",
        "2001.A.Space.Odyssey.1968.1080p.mkv": "2001 a space odyssey|1968",
        "Amélie.2001.720p.mkv": "amelie|2001",
        "Tom & Jerry 2021.mkv": "tom and jerry|2021",
        "Movies/Some Folder/Movie.2019.Extended.2160p.mkv": "movie|2019",
        "www.UIndex.org - The Imitation Game 2014 REMUX 2160p.mkv": "the imitation game|2014",
        "The.Imitation.Game.2014.MULTI.1080p.mkv": "the imitation game|2014",
        "Avatar Fire and Ash 2025 Hybrid 2160p DV HDR10.mkv": "avatar fire and ash|2025",
        "Avatar.Fire.And.Ash.2025.REPACK.MULTi.VF2.2160p.UHD.mkv": "avatar fire and ash|2025",
        "[YTS.MX] Movie Name 2019 1080p.mkv": "movie name|2019",
        "Everest.IMAX.2160p.mkv": None,
        "The.Show.S01E02.2019.1080p.mkv": None,
        "Show.1x05.2019.mkv": None,
        "Show.Season.2.2019.mkv": None,
        "No.Year.Here.1080p.mkv": None,
        "Movie.1080p.2019.mkv": None,
        "2019.mkv": None,
    }
    wrong = {k: st.release_key(k) for k, v in cases.items() if st.release_key(k) != v}
    check("release_key parses titles and years (and refuses what it cannot be sure of)", not wrong, wrong)
    check("different title strings do not match (a separate build is the safe fallback)",
          st.release_key("Hope.2026.1080p.WEB.mkv") != st.release_key("Hopeu - Hope (2026)CZtit 1080p.mkv"))

    lib2, store2 = os.path.join(tmp, "lib2"), os.path.join(tmp, "store2s")
    names = {"a": "Cool.Film.2019.2160p.WEB-DL.mkv", "b": "Cool Film (2019) 1080p BluRay.mkv", "c": "Cool.Film.2019.Extended.mkv",
             "d": "Cool.Film.2020.mkv", "e": "Other.Movie.mkv", "f": "Some.Show.S01E01.2019.mkv"}
    for k, n in names.items():
        make_film(os.path.join(lib2, n), seconds=75 if k == "c" else 60, size="1280x720" if k == "b" else "640x360")
    c2 = cfg(lib2, store2)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        st.scan_pass(c2, dry=True)
    pl = buf.getvalue()
    check("plan marks the release that is built (the smaller file) and the one that only gets an alias",
          "share this" in pl and "alias of the one above" in pl and pl.index(names["a"]) < pl.index(names["b"]), pl)
    check("plan summary counts films and aliases", "6 file(s) need thumbnails = 4 film(s) to build + 2 release(s)" in pl, pl)
    st.scan_pass(c2)
    kinds = {k: ("real" if st.read_index(st.entry_dir(store2, n)) else "alias" if st.read_alias(st.entry_dir(store2, n)) else None) for k, n in names.items()}
    ab = sorted([kinds["a"], kinds["b"]])
    check("two releases of one film: one real entry and one alias", ab == ["alias", "real"], kinds)
    check("a different cut (runtime), a different year, no year and a TV episode each get their own thumbnails",
          all(kinds[k] == "real" for k in "cdef"), kinds)
    check("batch: of two releases the smaller file is the one that is built", kinds["a"] == "real" and kinds["b"] == "alias", kinds)
    alias_k = "a" if kinds["a"] == "alias" else "b"
    real_k = "b" if alias_k == "a" else "a"
    alias_rel, real_rel = names[alias_k], names[real_k]
    adir = st.entry_dir(store2, alias_rel)
    check("an alias folder holds only its small index.json", os.listdir(adir) == ["index.json"] and os.path.getsize(os.path.join(adir, "index.json")) < 1000)
    asize, rsize = os.path.getsize(os.path.join(lib2, alias_rel)), os.path.getsize(os.path.join(lib2, real_rel))
    r_al, r_re = st.resolve_entry(store2, alias_rel, asize), st.resolve_entry(store2, real_rel, rsize)
    check("the alias resolves to the other release's thumbnails", r_al and r_re and r_al[2] == real_rel and r_al[0]["made"] == r_re[0]["made"])
    check("the alias is only valid for its own file size", st.resolve_entry(store2, alias_rel, asize + 1) is None)
    check("nothing is left to build", st.scan_pass(c2, dry=True) == 0)
    check("verify accepts aliases", st.cmd_verify(c2) == 0)
    out = subprocess.run([sys.executable, os.path.join(TOOL, "seekthumbs.py"), "--store", store2, "status"], capture_output=True, text=True).stdout
    check("status counts aliases separately", "1 sharing another release" in out, out)

    # a replaced alias file is queued and shares again
    make_film(os.path.join(lib2, alias_rel), seconds=60, size="320x180")
    check("a replaced file with an alias is queued again", st.scan_pass(c2, dry=True) == 1)
    st.scan_pass(c2)
    asize = os.path.getsize(os.path.join(lib2, alias_rel))
    check("after the rebuild it shares again", st.read_alias(adir) is not None and st.resolve_entry(store2, alias_rel, asize))

    # the target is rebuilt: the alias stops resolving (its pictures may no longer fit), then is re-linked
    make_film(os.path.join(lib2, real_rel), seconds=60, size="480x270")
    st.scan_pass(c2, dry=False) if False else None
    rr = os.path.join(lib2, real_rel)
    lk = st.film_lock(store2, alias_rel)                    # keep the alias out of the next pass so the stale state can be looked at
    st.scan_pass(c2)
    lk.close()
    check("when the target is rebuilt the alias no longer resolves", st.resolve_entry(store2, alias_rel, asize) is None and st.read_index(st.entry_dir(store2, real_rel)))
    st.scan_pass(c2)
    check("the next pass links the alias to the new thumbnails", st.resolve_entry(store2, alias_rel, asize) is not None)

    # loops and broken links
    for nm in ("Loop.A.2000.mkv", "Loop.B.2000.mkv"):
        os.makedirs(st.entry_dir(store2, nm))
    for me, other in (("Loop.A.2000.mkv", "Loop.B.2000.mkv"), ("Loop.B.2000.mkv", "Loop.A.2000.mkv")):
        with open(os.path.join(st.entry_dir(store2, me), "index.json"), "w") as f:
            json.dump(dict(v=2, alias_of=other, src=me, size=5, duration=60, target_made="x", made="x"), f)
    check("an alias loop gives nothing (and does not hang)", st.resolve_entry(store2, "Loop.A.2000.mkv", 5) is None)
    os.makedirs(st.entry_dir(store2, "Dangling.2000.mkv"))
    with open(os.path.join(st.entry_dir(store2, "Dangling.2000.mkv"), "index.json"), "w") as f:
        json.dump(dict(v=2, alias_of="Not.There.2000.mkv", src="Dangling.2000.mkv", size=5, duration=60, target_made="x", made="x"), f)
    check("an alias to a missing entry gives nothing", st.resolve_entry(store2, "Dangling.2000.mkv", 5) is None)
    check("verify reports broken aliases", st.cmd_verify(c2) == 1)
    for nm in ("Loop.A.2000.mkv", "Loop.B.2000.mkv", "Dangling.2000.mkv"):
        shutil.rmtree(st.entry_dir(store2, nm))

    # prune and aliases
    lib3, store3 = os.path.join(tmp, "lib3"), os.path.join(tmp, "store3s")
    for n in ("Real.Film.2018.mkv", "Real Film (2018) 720p.mkv", "Zed.Keep.mkv"):
        make_film(os.path.join(lib3, n), seconds=60, size="640x360" if n.startswith("Real.") else "320x180")
    c3 = cfg(lib3, store3)
    st.scan_pass(c3)
    kinds3 = {n: ("real" if st.read_index(st.entry_dir(store3, n)) else "alias" if st.read_alias(st.entry_dir(store3, n)) else None)
              for n in ("Real.Film.2018.mkv", "Real Film (2018) 720p.mkv")}
    real3 = [n for n, k in kinds3.items() if k == "real"][0]
    alias3 = [n for n, k in kinds3.items() if k == "alias"][0]

    def prune():
        return subprocess.run([sys.executable, os.path.join(TOOL, "seekthumbs.py"), "--library", lib3, "--store", store3, "--min-size-mb", "0", "prune", "--yes"],
                              capture_output=True, text=True).stdout

    os.remove(os.path.join(lib3, real3))
    out = prune()
    check("prune keeps a pack whose film is gone while an alias still uses it", os.path.isfile(os.path.join(st.entry_dir(store3, real3), "thumbs.pack")) and "keeping" in out, out)
    check("the alias still resolves after that prune", st.resolve_entry(store3, alias3, os.path.getsize(os.path.join(lib3, alias3))) is not None)
    os.remove(os.path.join(lib3, alias3))
    prune()
    check("when both films are gone, prune removes the alias and the pack", not os.path.isdir(st.entry_dir(store3, real3)) and not os.path.isdir(st.entry_dir(store3, alias3)))
    # a broken alias whose film still exists is removed
    make_film(os.path.join(lib3, "Orphan.Alias.2011.mkv"), seconds=60)
    os.makedirs(st.entry_dir(store3, "Orphan.Alias.2011.mkv"))
    with open(os.path.join(st.entry_dir(store3, "Orphan.Alias.2011.mkv"), "index.json"), "w") as f:
        json.dump(dict(v=2, alias_of="Gone.2011.mkv", src="Orphan.Alias.2011.mkv", size=5, duration=60, target_made="x", made="x"), f)
    prune()
    check("prune removes an alias whose target is gone", not os.path.isdir(st.entry_dir(store3, "Orphan.Alias.2011.mkv")))
    check("prune leaves entries of films that still exist", st.read_index(st.entry_dir(store3, "Zed.Keep.mkv")) is not None)

    # ---- build FILE (what the viewer starts)
    lib4, store4 = os.path.join(tmp, "lib4"), os.path.join(tmp, "store4s")
    bf = os.path.join(lib4, "Build.Me.2021.mkv")
    make_film(bf, seconds=60, size="640x360")
    BASE = [sys.executable, os.path.join(TOOL, "seekthumbs.py"), "--library", lib4, "--store", store4, "--min-size-mb", "0", "--interval", "10", "--play-pause", "0"] + EXTRACTOR_ARGS

    def run_build(path, extra=()):
        return subprocess.run(BASE + list(extra) + ["build", path], capture_output=True, text=True)

    t0 = time.time()
    r = run_build(bf)
    check("build FILE builds the film's thumbnails", r.returncode == 0 and st.resolve_entry(store4, "Build.Me.2021.mkv", os.path.getsize(bf)), r.stdout + r.stderr)
    check("build FILE said it uses the gentle settings (2 reads, lowest priority)", "2 parallel read(s)" in r.stdout and "lowest priority" in r.stdout, r.stdout)
    r = run_build(bf)
    check("a second build FILE finds the entry and does nothing", r.returncode == 0 and "already has thumbnails" in r.stdout, r.stdout)
    check("build FILE for a path outside the library is refused", run_build(os.path.join(tmp, "elsewhere.mkv")).returncode == 2)
    iso = os.path.join(lib4, "disc.iso")
    open(iso, "wb").write(b"x" * 100000)
    r = run_build(iso)
    check("build FILE skips a non-video file", r.returncode == 0 and "not a video" in r.stdout and not os.path.isdir(st.entry_dir(store4, "disc.iso")), r.stdout)
    r = subprocess.run([sys.executable, os.path.join(TOOL, "seekthumbs.py"), "--library", lib4, "--store", store4, "build", bf.replace("Build.Me", "Build.Me")],
                       capture_output=True, text=True, env=dict(os.environ, HOME=tmp))
    small = os.path.join(lib4, "Small.One.2020.mkv")
    make_film(small, seconds=20, size="320x180")
    r = subprocess.run([sys.executable, os.path.join(TOOL, "seekthumbs.py"), "--library", lib4, "--store", store4, "build", small], capture_output=True, text=True,
                       env=dict(os.environ, HOME=tmp))
    check("build FILE skips a film under the minimum size (300 MB by default)", r.returncode == 0 and "smaller than" in r.stdout and not os.path.isdir(st.entry_dir(store4, "Small.One.2020.mkv")), r.stdout)

    lockme = os.path.join(lib4, "Lock.Me.2022.mkv")
    make_film(lockme, seconds=60, size="320x180")
    lk = st.film_lock(store4, "Lock.Me.2022.mkv")
    r = run_build(lockme)
    check("build FILE does nothing while another instance holds the film's lock", r.returncode == 0 and "already being built" in r.stdout and not os.path.isdir(st.entry_dir(store4, "Lock.Me.2022.mkv")), r.stdout)
    check("scan skips a film whose lock is held", st.scan_pass(cfg(lib4, store4)) >= 0 and not os.path.isdir(st.entry_dir(store4, "Lock.Me.2022.mkv")))
    lk.close()
    r = run_build(lockme)
    check("once the lock is free build FILE builds it", r.returncode == 0 and st.read_index(st.entry_dir(store4, "Lock.Me.2022.mkv")) is not None, r.stdout)

    race = os.path.join(lib4, "Race.Me.2023.mkv")
    make_film(race, seconds=60, size="320x180")
    procs = [subprocess.Popen(BASE + ["build", race], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for _ in range(3)]
    outs = [p_.communicate()[0] for p_ in procs]
    nbuilt = sum(o.count("built Race.Me.2023.mkv") for o in outs)
    check("three builds started together build the film exactly once", nbuilt == 1 and st.read_index(st.entry_dir(store4, "Race.Me.2023.mkv")), outs)

    broken = os.path.join(lib4, "Broken.Build.2020.mkv")
    open(broken, "wb").write(b"not a video" * 1000)
    r1 = run_build(broken)
    r2 = run_build(broken)
    check("a film that cannot be read fails once and is then not retried at once", r1.returncode == 1 and r2.returncode == 0 and "failed recently" in r2.stdout, (r1.stdout, r2.stdout))

    # in-process: workers, no waiting for players, scan still waits
    seen = {}
    orig_build, orig_pr = st.build_entry, st.player_running
    st.build_entry = lambda rel, src, size, c_, gov, zs: seen.update(workers=c_["workers"], jobs=c_["encode_jobs"], limit=gov.max, pause=c_["pause"]) or True
    def boom():
        raise AssertionError("build FILE must not wait for players")
    st.player_running = boom
    newf = os.path.join(lib4, "Workers.Test.2024.mkv")
    make_film(newf, seconds=30, size="320x180")
    try:
        rc = st.cmd_build(cfg(lib4, store4, play_workers=3, play_pause=0.5, yield_to_playback=1), newf, own_process=False)
    finally:
        st.build_entry, st.player_running = orig_build, orig_pr
    check("build FILE uses play_workers (not workers) and does not wait for a running player", rc == 0 and seen.get("workers") == 3 and seen.get("limit") == 3 and seen.get("jobs") == 2 and seen.get("pause") == 0.5, (rc, seen))

    # during playback: never fall back to one ffmpeg process per thumbnail
    strict = os.path.join(lib4, "Strict.Pyav.2025.mkv")
    make_film(strict, seconds=40, size="320x180")
    orig_usable, orig_grab = st.AvExtractor.usable, st.CliExtractor.grab
    st.AvExtractor.usable = staticmethod(lambda vf: (False, "PyAV is not installed"))
    used_cli = []
    st.CliExtractor.grab = lambda self, t_, out: used_cli.append(1) or orig_grab(self, t_, out)
    try:
        rc = st.cmd_build(cfg(lib4, store4, play_pause=0), strict, own_process=False)
        check("build FILE skips (and says why) when PyAV is unusable instead of reading with ffmpeg commands",
              rc == 1 and not used_cli and not os.path.isdir(st.entry_dir(store4, "Strict.Pyav.2025.mkv")), (rc, used_cli))
        check("that skip is not recorded as a failure (installing python3-av must work at once)", "Strict.Pyav.2025.mkv" not in st.State(store4).d["fail"])
        rc = st.cmd_build(cfg(lib4, store4, play_pause=0, play_extractor="auto"), strict, own_process=False)
        check("play_extractor=auto allows the ffmpeg command when PyAV is unusable", rc == 0 and used_cli and st.read_index(st.entry_dir(store4, "Strict.Pyav.2025.mkv")), (rc, used_cli))
    finally:
        st.AvExtractor.usable, st.CliExtractor.grab = orig_usable, orig_grab

    waits = []
    orig_sleep = st.sleep_interruptible
    calls_pr = {"n": 0}
    def fake_pr():
        calls_pr["n"] += 1
        return calls_pr["n"] == 1
    st.player_running, st.sleep_interruptible = fake_pr, lambda sec: waits.append(sec)
    try:
        st.scan_pass(cfg(lib4, store4, yield_to_playback=1))
    finally:
        st.player_running, st.sleep_interruptible = orig_pr, orig_sleep
    check("scan still waits while a player is running", waits == [300] and calls_pr["n"] >= 2, (waits, calls_pr))

    # stale work folders
    stale = os.path.join(store4, "Some", ".work-old"); fresh = os.path.join(store4, "Some", ".work-new")
    os.makedirs(os.path.join(stale, "jpg")); os.makedirs(fresh)
    os.utime(stale, (time.time() - 3 * 86400,) * 2)
    st.clean_stale_work(store4)
    check("a build that was killed leaves a work folder; old ones are removed, fresh ones kept", not os.path.exists(stale) and os.path.isdir(fresh))

    # settings work after the subcommand too (the README writes `scan --max-films 1`)
    lib7, store7 = os.path.join(tmp, "lib7"), os.path.join(tmp, "store7s")
    for n in ("Two.Films.Alpha.mkv", "Two.Films.Beta.mkv"):
        make_film(os.path.join(lib7, n), seconds=30, size="320x180")
    after = [sys.executable, os.path.join(TOOL, "seekthumbs.py")]
    r = subprocess.run(after + ["plan", "--library", lib7, "--store", store7, "--min-size-mb", "0"], capture_output=True, text=True)
    check("options after the subcommand work (plan --library ... --store ...)", r.returncode == 0 and "2 file(s) need thumbnails" in r.stdout, r.stdout + r.stderr)
    r = subprocess.run(after + ["scan", "--library", lib7, "--store", store7, "--min-size-mb", "0", "--interval", "10", "--max-films", "1"], capture_output=True, text=True)
    check("scan --max-films 1 builds exactly one film", r.returncode == 0 and len(list(st.store_entries(store7))) == 1, r.stdout + r.stderr)
    r = subprocess.run(after + ["--library", lib7, "--store", store7, "--min-size-mb", "0", "--interval", "10", "--max-films", "1", "scan"], capture_output=True, text=True)
    check("the options before the subcommand still work, and they are not undone by the subcommand", r.returncode == 0 and len(list(st.store_entries(store7))) == 2, r.stdout + r.stderr)

    # a reader that closes the pipe early ends the program quietly
    r = subprocess.run(["bash", "-c", f"cd '{TOOL}' && python3 -c \"import seekthumbs as st, sys; st.main = lambda a: [print('x' * 200) for _ in range(20000)] and 0; sys.argv = ['x']; st.cli()\" | head -1"],
                       capture_output=True, text=True)
    check("plan | head ends quietly on a closed pipe (no BrokenPipeError traceback)", "Traceback" not in r.stderr and "BrokenPipe" not in r.stderr, r.stderr[-300:])

    # install-viewer records the builder
    mc2 = os.path.join(tmp, "mpvconf2")
    os.makedirs(os.path.join(mc2, "script-opts"))
    with open(os.path.join(mc2, "script-opts", "seekthumbs.conf"), "w") as f:
        f.write("debug=yes\nbuilder=/old/place/seekthumbs.py\n")
    st.cmd_install_viewer(mc2)
    st.cmd_install_viewer(mc2)
    conf = open(os.path.join(mc2, "script-opts", "seekthumbs.conf")).read().split("\n")
    check("install-viewer sets builder= once, keeps the other lines, and repeats cleanly",
          conf.count("debug=yes") == 1 and [l for l in conf if l.startswith("builder=")] == ["builder=" + os.path.join(os.path.abspath(TOOL), "seekthumbs.py")], conf)

    # --- the viewer
    try:
        import lupa
    except ImportError:
        print("SKIP viewer tests (pip install lupa)")
        lupa = None
    if lupa:
        LUA = lupa.LuaRuntime(unpack_returned_tuples=True)
        source = open(os.path.join(TOOL, "thumbfast.lua")).read()

        def viewer(path, file_size, library=lib, store_=store, builder="", build=True, min_size=300):
            """Loads thumbfast.lua with a mocked mp API. Returns (handlers, log, props)."""
            log = []
            props = {"path": path, "file-size": str(file_size) if file_size is not None else None, "working-directory": "/", "pid": "4242"}
            handlers = {"msg": {}, "ev": {}, "timers": [], "killed": [], "clock": {"t": 1000.0}}
            py = {}

            def get_property(name):
                return props.get(name)

            def commandv(*a):
                log.append(("commandv",) + tuple(str(x) for x in a))
                return True

            def command_native(t):
                t = dict(t)
                args = list(dict(t["args"]).values())
                subprocess.run(args, capture_output=True)
                return True

            def command_native_async(t, cb):
                t = dict(t)
                args = list(dict(t["args"]).values())
                r = subprocess.run(args, capture_output=True)
                cb(True, LUA.table(status=r.returncode))

            def reg_msg(name, fn):
                handlers["msg"][name] = fn

            def reg_ev(name, fn):
                handlers["ev"][name] = fn

            LUA.execute("package.loaded['mp']=nil")
            # Stand-ins for the three modules the script requires.
            g = LUA.globals()
            mp = LUA.table(get_property=get_property, commandv=commandv, command_native=command_native, command_native_async=command_native_async,
                           register_script_message=reg_msg, register_event=reg_ev, get_time=lambda: handlers["clock"]["t"],
                           add_periodic_timer=lambda sec, fn: (handlers["timers"].append(fn), LUA.table(kill=lambda *a: handlers["killed"].append(1)))[1],
                           msg=LUA.table(info=lambda *a: log.append(("info",) + tuple(str(x) for x in a))))
            utils = LUA.eval("""function(json_decode, json_encode)
                return { join_path = function(a, b) if b:sub(1,1) == '/' then return b end return (a:gsub('/+$','')) .. '/' .. b end,
                         parse_json = json_decode, format_json = json_encode }
            end""")(lambda s: _to_lua(json.loads(s)), lambda t: json.dumps(_to_py(t)))
            options = LUA.table(read_options=lambda o, name: None)
            g.package.loaded["mp"] = mp
            g.package.loaded["mp.utils"] = utils
            g.package.loaded["mp.options"] = options
            os.environ["XDG_RUNTIME_DIR"] = tmp
            chunk = source.replace('local opts = { library = "", store = "",', f'local opts = {{ library = "{library}", store = "{store_}",')
            chunk = chunk.replace('build = true, builder = "", python = "python3", min_size_mb = 300,',
                                  f'build = {"true" if build else "false"}, builder = "{builder}", python = "python3", min_size_mb = {min_size},')
            LUA.execute(chunk)
            return handlers, log, props

        def _to_lua(v):
            if isinstance(v, dict):
                return LUA.table_from({k: _to_lua(x) for k, x in v.items()})
            if isinstance(v, list):
                return LUA.table_from([_to_lua(x) for x in v], False) if False else LUA.table(*[_to_lua(x) for x in v])
            return v

        def _to_py(t):
            if hasattr(t, "items"):
                keys = list(t.keys())
                if keys and all(isinstance(k, int) for k in keys):
                    return [_to_py(t[k]) for k in sorted(keys)]
                return {k: _to_py(v) for k, v in t.items()}
            return t

        size = os.path.getsize(film)
        # the film was regenerated after the first build; rebuild so the store matches the current file
        st.scan_pass(c)
        h, log, _ = viewer(film, size)
        h["ev"]["file-loaded"]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("viewer finds the entry by library-relative path", infos and infos[-1]["available"] is True, log)
        check("viewer reports the thumbnail size to uosc", infos and infos[-1]["width"] > 0 and infos[-1]["height"] > 0)
        log.clear()
        h["msg"]["thumb"]("37", "100", "50")
        adds = [l for l in log if l[1] == "overlay-add"]
        check("hovering shows an overlay at the asked position", len(adds) == 1 and adds[0][3:5] == ("100", "50") and adds[0][7] == "bgra", log)
        check("overlay uses the thumbfast overlay id (42)", adds and adds[0][2] == "42")
        if adds:
            f, offs, w_, h_, stride = adds[0][5], int(adds[0][6]), int(adds[0][8]), int(adds[0][9]), int(adds[0][10])
            check("raw frame file has the size the overlay claims", os.path.getsize(f) == w_ * h_ * 4 and stride == w_ * 4)
        log.clear()
        h["msg"]["thumb"]("37", "120", "50")
        check("a second hover on the same thumbnail reuses the converted frame", len([l for l in log if l[1] == "overlay-add"]) == 1)
        h["msg"]["clear"]()
        check("leaving the bar removes the overlay", any(l[1] == "overlay-remove" for l in log))
        log.clear()
        h["msg"]["thumb"]("99999", "10", "10")
        check("a time past the end uses the last thumbnail", len([l for l in log if l[1] == "overlay-add"]) == 1)

        h, log, _ = viewer(film, size + 1)
        h["ev"]["file-loaded"]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("a replaced file (different size) shows nothing", infos and infos[-1]["available"] is False)

        mirror = os.path.join(tmp, "elsewhere", "Movies", "A Film (2020)", "A.Film.2020.mkv")
        h, log, _ = viewer(mirror, size)
        h["ev"]["file-loaded"]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("the same film opened through another mount point is matched by path tail and size", infos and infos[-1]["available"] is True)

        h, log, _ = viewer("https://example.com/video.mkv", None)
        h["ev"]["file-loaded"]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("a URL gets no thumbnails and no errors", infos and infos[-1]["available"] is False)


        # ---- aliases in the viewer (the stores were made above)
        h, log, _ = viewer(os.path.join(lib2, alias_rel), os.path.getsize(os.path.join(lib2, alias_rel)), library=lib2, store_=store2)
        h["ev"]["file-loaded"]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("viewer follows an alias to the other release's thumbnails", infos and infos[-1]["available"] is True and infos[-1]["width"] > 0, log)
        log.clear()
        h["msg"]["thumb"]("25", "10", "10")
        check("an alias shows the real entry's pictures", len([l for l in log if l[1] == "overlay-add"]) == 1)
        h, log, _ = viewer(os.path.join(lib2, alias_rel), os.path.getsize(os.path.join(lib2, alias_rel)) + 1, library=lib2, store_=store2)
        h["ev"]["file-loaded"]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("an alias checks its own file size: a replaced file shows nothing", infos and infos[-1]["available"] is False)
        for nm, tgt in (("Vloop.A.2000.mkv", "Vloop.B.2000.mkv"), ("Vloop.B.2000.mkv", "Vloop.A.2000.mkv"), ("Vdangle.2000.mkv", "Not.There.2000.mkv")):
            os.makedirs(st.entry_dir(store2, nm), exist_ok=True)
            with open(os.path.join(st.entry_dir(store2, nm), "index.json"), "w") as f:
                json.dump(dict(v=2, alias_of=tgt, src=nm, size=5, duration=60, target_made="x", made="x"), f)
        for nm in ("Vloop.A.2000.mkv", "Vdangle.2000.mkv"):
            h, log, _ = viewer(os.path.join(lib2, nm), 5, library=lib2, store_=store2)
            h["ev"]["file-loaded"]()
            infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
            check(f"viewer ignores a broken or looping alias ({nm})", infos and infos[-1]["available"] is False)

        # ---- the on-play build
        lib6, store6 = os.path.join(tmp, "lib6"), os.path.join(tmp, "store6s")
        vf = os.path.join(lib6, "View.Me.2023.mkv")
        make_film(vf, seconds=60, size="640x360")
        vsize = os.path.getsize(vf)
        BUILDER = os.path.join(TOOL, "seekthumbs.py")
        h, log, _ = viewer(vf, vsize, library=lib6, store_=store6, builder=BUILDER, min_size=0)
        h["ev"]["file-loaded"]()
        runs = [l for l in log if l[:2] == ("commandv", "run")]
        check("a film without thumbnails starts one background build", len(runs) == 1 and runs[0][2:] == ("python3", BUILDER, "--library", lib6, "--store", store6, "--min-size-mb", "0", "build", vf), runs)
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("until it is built the viewer reports no thumbnails and shows no error", infos and infos[-1]["available"] is False)
        h["ev"]["file-loaded"]()
        check("loading the same film again in the session does not start a second build", len([l for l in log if l[:2] == ("commandv", "run")]) == 1)
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        n_info = len(infos)
        n_killed = len(h["killed"])
        h["timers"][-1]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("the re-check before the build finished changes nothing", len(infos) == n_info)
        env = dict(os.environ, HOME=tmp)
        os.makedirs(os.path.join(tmp, ".config", "seekthumbs"), exist_ok=True)
        with open(os.path.join(tmp, ".config", "seekthumbs", "config.ini"), "w") as f:
            f.write("[seekthumbs]\nmin_size_mb = 0\ninterval = 10\nplay_pause = 0\n" + ("" if HAVE_AV else "play_extractor = auto\n"))
        rr_ = subprocess.run(list(runs[0][2:]), capture_output=True, text=True, env=env)       # the command mpv's `run` would execute
        check("the command the viewer issued really builds the film", rr_.returncode == 0 and st.read_index(st.entry_dir(store6, "View.Me.2023.mkv")), rr_.stdout + rr_.stderr)
        h["timers"][-1]()
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("when the build is done the next re-check tells the OSC the thumbnails are available", infos and infos[-1]["available"] is True and len(infos) == n_info + 1)
        check("and the re-check timer is stopped", len(h["killed"]) == n_killed + 1, (n_killed, h["killed"]))
        log.clear()
        h["msg"]["thumb"]("33", "10", "10")
        check("hovering then shows the new thumbnail", len([l for l in log if l[1] == "overlay-add"]) == 1)

        vf2 = os.path.join(lib6, "Hover.Me.2023.mkv")
        make_film(vf2, seconds=60, size="320x180")
        h, log, _ = viewer(vf2, os.path.getsize(vf2), library=lib6, store_=store6, builder="", min_size=0)
        h["ev"]["file-loaded"]()
        log.clear()
        h["msg"]["thumb"]("10", "5", "5")           # nothing yet: looks (first look), finds nothing
        c6 = cfg(lib6, store6)
        st.scan_pass(c6)                             # built by somebody else meanwhile
        h["clock"]["t"] += 1
        h["msg"]["thumb"]("10", "5", "5")
        check("hover re-checks are throttled (not every hover)", not [l for l in log if l[1] == "overlay-add"])
        h["clock"]["t"] += 6
        h["msg"]["thumb"]("10", "5", "5")
        infos = [json.loads(l[3]) for l in log if len(l) > 3 and l[1] == "script-message" and l[2] == "thumbfast-info"]
        check("a later hover finds an entry built by somebody else and tells the OSC", infos and infos[-1]["available"] is True and [l for l in log if l[1] == "overlay-add"], log)

        # no build in these cases
        def runs_for(path, size, **kw):
            hh, ll, _ = viewer(path, size, **kw)
            hh["ev"]["file-loaded"]()
            return [l for l in ll if l[:2] == ("commandv", "run")]

        fresh_film = os.path.join(lib6, "Not.Built.2024.mkv")
        check("a URL is never built", not runs_for("https://example.com/Not.Built.2024.mkv", None, library=lib6, store_=store6, builder=BUILDER, min_size=0))
        check("a file outside the library is never built", not runs_for(os.path.join(tmp, "elsewhere", "Not.Built.2024.mkv"), 10 ** 9, library=lib6, store_=store6, builder=BUILDER, min_size=0))
        check("a non-video file is never built", not runs_for(os.path.join(lib6, "disc.iso"), 10 ** 9, library=lib6, store_=store6, builder=BUILDER, min_size=0))
        check("a file under the minimum size is never built", not runs_for(fresh_film, 10 * 10 ** 6, library=lib6, store_=store6, builder=BUILDER, min_size=300))
        check("a big enough film is built", len(runs_for(fresh_film, 4 * 10 ** 9, library=lib6, store_=store6, builder=BUILDER, min_size=300)) == 1)
        check("build=no turns the on-play build off", not runs_for(fresh_film, 4 * 10 ** 9, library=lib6, store_=store6, builder=BUILDER, build=False))
        check("without a builder path nothing is started (install-viewer sets it)", not runs_for(fresh_film, 4 * 10 ** 9, library=lib6, store_=store6, builder=""))

    # --- install-viewer moves a real thumbfast aside and is repeatable
    mc = os.path.join(tmp, "mpvconf")
    os.makedirs(os.path.join(mc, "scripts"))
    with open(os.path.join(mc, "scripts", "thumbfast.lua"), "w") as f:
        f.write("-- the real thumbfast\n")
    st.cmd_install_viewer(mc)
    check("install-viewer keeps the original as .bak", os.path.exists(os.path.join(mc, "scripts", "thumbfast.lua.bak")))
    st.cmd_install_viewer(mc)
    with open(os.path.join(mc, "scripts", "thumbfast.lua.bak")) as f:
        check("a second install-viewer does not overwrite the .bak with ours", f.read().startswith("-- the real thumbfast"))
    # ---- skip_tv / exclude
    c_tv = cfg("/x", "/y", skip_tv=1)
    names_tv = ["Game of Thrones S01-S08 Complete UHD/Game.of.Thrones.S08E05.The.Bells.2160p.mkv", "Show Name s02e10 1080p/Show.Name.s02e10.mkv", "Show.S1E2.mkv"]
    names_film = ["Tron.Ares.2025.2160p/Tron.Ares.2025.2160p.mkv", "IMAX - Wolves.2001.1080p.mkv", "Game of Thrones S01-S08 Complete UHD/extras.mkv", "Rise.of.Skywalker.2019.mkv", "Wes.Anderson.S1ND.mkv"]
    check("skip_tv leaves out names with an SxxExx tag", all(st.excluded(c_tv, n) for n in names_tv), [n for n in names_tv if not st.excluded(c_tv, n)])
    check("skip_tv keeps films (and folders that only say S01-S08)", not any(st.excluded(c_tv, n) for n in names_film), [n for n in names_film if st.excluded(c_tv, n)])
    check("nothing is excluded by default", not any(st.excluded(cfg("/x", "/y"), n) for n in names_tv + names_film))
    c_rx = cfg("/x", "/y", exclude="game.of.thrones|IMAX")
    check("exclude is a case-insensitive regex on the relative path, | for several", st.excluded(c_rx, "IMAX - Wolves.mkv") and st.excluded(c_rx, "GAME OF THRONES/x.mkv") and not st.excluded(c_rx, "Tron.mkv"))
    check("a broken exclude regex excludes nothing instead of crashing", not st.excluded(cfg("/x", "/y", exclude="("), "Tron.mkv"))
    lib7, store7 = os.path.join(tmp, "lib7"), os.path.join(tmp, "store7s")
    ep = os.path.join(lib7, "Show.Name.S01E02.1080p.mkv"); make_film(ep, seconds=30, size="320x180")
    fl = os.path.join(lib7, "Some.Film.2020.mkv"); make_film(fl, seconds=30, size="320x180")
    rc = st.cmd_build(cfg(lib7, store7, skip_tv=1, play_pause=0, play_extractor="auto"), ep, own_process=False)
    check("build FILE skips a TV episode when skip_tv=1", rc == 0 and not os.path.isdir(st.entry_dir(store7, "Show.Name.S01E02.1080p.mkv")))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        st.scan_pass(cfg(lib7, store7, skip_tv=1, settle=0), dry=True)
    check("plan lists the film but not the episode, and says how many were left out", "Some.Film.2020.mkv" in out.getvalue() and "S01E02" not in out.getvalue(), out.getvalue())

    # ---- cold files: wake read, probe timeout, skip-and-retry, no failure recorded
    wf = os.path.join(lib7, "Some.Film.2020.mkv")
    check("wake_file reads a file and returns the seconds it took", isinstance(st.wake_file(wf, 10), float))
    fifo = os.path.join(tmp, "stuck.mkv")
    os.mkfifo(fifo)                                              # opening a FIFO blocks until a writer appears: a file that never answers
    try:
        st.wake_file(fifo, 0.5)
        stuck_raised = False
    except st.ProbeTimeout:
        stuck_raised = True
    wr = os.open(fifo, os.O_WRONLY)                              # release the blocked reader thread
    os.close(wr)
    check("wake_file raises ProbeTimeout for a file that never answers", stuck_raised)
    real_run = st.subprocess.run

    def slow_run(*a, **kw):
        raise subprocess.TimeoutExpired("ffprobe", kw.get("timeout", 0))
    st.subprocess.run = slow_run
    try:
        st.ffprobe(wf, cfg("/x", "/y", probe_timeout=7))
        probe_raised = False
    except st.ProbeTimeout as ex:
        probe_raised = "7 s" in str(ex)
    finally:
        st.subprocess.run = real_run
    check("an ffprobe timeout becomes ProbeTimeout (with the configured limit in the message)", probe_raised)

    lib8, store8 = os.path.join(tmp, "lib8"), os.path.join(tmp, "store8")
    slow_film = os.path.join(lib8, "Slow.Film.2020.mkv"); make_film(slow_film, seconds=30, size="320x180")
    fast_film = os.path.join(lib8, "Fast.Film.2021.mkv"); make_film(fast_film, seconds=40, size="320x180")
    real_probe, calls = st.ffprobe, []

    def flaky_probe(path, c):
        calls.append(os.path.basename(path))
        if path == slow_film and calls.count("Slow.Film.2020.mkv") == 1:
            raise st.ProbeTimeout("simulated cold open")
        return real_probe(path, c)
    st.ffprobe = flaky_probe
    try:
        built = st.scan_pass(cfg(lib8, store8, settle=0, share=0, play_extractor="auto", extractor="auto"))
    finally:
        st.ffprobe = real_probe
    st_state = st.State(store8)
    check("a slow-to-open film is retried at the end of the pass and then built", built == 2 and os.path.isdir(st.entry_dir(store8, "Slow.Film.2020.mkv")), (built, calls))
    check("the retry came after the other film (it was skipped, not waited for)", calls.index("Fast.Film.2021.mkv") < len(calls) - 1 and calls[-1] == "Slow.Film.2020.mkv", calls)
    check("a slow open is not recorded as a failure", not st_state.d["fail"], st_state.d["fail"])

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("FAILED" if fails else "ALL PASSED", fails)
sys.exit(1 if fails else 0)
