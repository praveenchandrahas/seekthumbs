#!/usr/bin/env python3
"""Tests for tools/seekthumbs: generator (synthetic films made with ffmpeg) and the mpv viewer (Lua run under lupa with a mocked mp API).
Needs ffmpeg and, for the viewer part, `pip install lupa` (skipped with a note if missing). Does not need mpv."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..", "tools", "seekthumbs")
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
    c.update(library=lib, store=store, pause=0, film_pause=0, interval=10.0, min_size_mb=0, settle=60, yield_to_playback=0)
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

    # --- the viewer
    try:
        import lupa
    except ImportError:
        print("SKIP viewer tests (pip install lupa)")
        lupa = None
    if lupa:
        LUA = lupa.LuaRuntime(unpack_returned_tuples=True)
        source = open(os.path.join(TOOL, "thumbfast.lua")).read()

        def viewer(path, file_size, library=lib, store_=store):
            """Loads thumbfast.lua with a mocked mp API. Returns (handlers, log, props)."""
            log = []
            props = {"path": path, "file-size": str(file_size) if file_size is not None else None, "working-directory": "/", "pid": "4242"}
            handlers = {"msg": {}, "ev": {}}
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
                           register_script_message=reg_msg, register_event=reg_ev,
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
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("FAILED" if fails else "ALL PASSED", fails)
sys.exit(1 if fails else 0)
