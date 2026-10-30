-- seekthumbs viewer for mpv: shows the seek-bar thumbnails that seekthumbs.py made, without ever opening the film a second time.
--
-- It is installed under the NAME thumbfast.lua, because uosc (and the stock OSC variants that support thumbfast) talk to a script called
-- "thumbfast": they send `script-message-to thumbfast thumb <seconds> <x> <y>` while you hover the seek bar and `... clear` when you leave,
-- and they listen for a `thumbfast-info` message that says how big the picture is. Stock thumbfast starts a second mpv that opens the film
-- itself, which on an rclone mount means more cloud reads on every hover. This replacement reads the local store instead. It cannot be
-- used together with the real thumbfast (one of them has to be named thumbfast.lua); `seekthumbs.py install-viewer` handles that.
--
-- How the playing film is linked to its thumbnails: by path. The film's path relative to the library folder, plus ".seekthumbs", is the
-- folder in the store. As a safeguard the recorded file size must equal the playing file's size, so a replaced film never shows
-- the old film's pictures. If the path does not start with the library folder (opened through another mount point, say), shorter and shorter
-- tails of the path are tried against the store, and the size check decides.
--
-- Aliases: a release that is the same film as one already built has only a small index.json with alias_of=<entry of the other release>; this script
-- follows it (the size check uses the alias's own recorded size, so a replaced file still shows nothing; broken links, loops and a target that was
-- rebuilt since are ignored).
--
-- On play: when the playing file is a video under `library`, at least min_size_mb big, and has no entry, the script starts `seekthumbs.py build FILE`
-- in the background (mpv's detached `run`, so it never blocks mpv and finishes even if you stop watching), once per file per session. IT IS OFF BY DEFAULT
-- (a build of a 4K remux read about 66 MB/s through the mount for as long as it ran, 2026-10-30): `install-viewer --on-play` records the path of
-- seekthumbs.py as builder= and sets build=yes in script-opts/seekthumbs.conf; plain `install-viewer` sets build=no. Until the entry exists nothing is shown;
-- the script looks again every recheck_s seconds and tells the OSC as soon as it is there.
--
-- Settings: ~/.config/seekthumbs/config.ini (the same file the generator reads: library=, store=), overridable in script-opts/seekthumbs.conf.
local mp = require "mp"
local utils = require "mp.utils"
local options = require "mp.options"

local opts = { library = "", store = "", overlay_id = 42, ffmpeg = "ffmpeg", cache_items = 80, debug = false,
              build = false, builder = "", python = "python3", min_size_mb = 300, recheck_s = 10,
              bulk = true, bulk_max_mb = 256,            -- bulk: convert the whole pack with ONE ffmpeg run when the film loads (see below)
              no_overlay = false, no_convert = false }   -- the last two are for finding a cost: no_convert answers hovers with nothing, no_overlay converts but never shows
options.read_options(opts, "seekthumbs")

local SUFFIX = ".seekthumbs"
local ix, edir, tmpdir = nil, nil, nil
local want = nil            -- the thumbnail index most recently asked for
local shown = nil           -- the index currently on screen
local pending = {}          -- index -> true while its conversion runs
local cache, order = {}, {} -- index -> {file=, w=, h=}, in the order they were made
-- Hover cost control. uosc sends a `thumb` message for every mouse move (a hundred or more a second), and each overlay-add makes mpv read
-- the picture again and redraw, on the thread that also feeds the audio. A sweep along the bar also starts one ffmpeg per new picture.
-- Measured 2026-10-29 on a 4K stream: mpv's CPU went from 26 % to 76-101 % of a core while hovering, with 14 dropped frames and 16 audio
-- underruns in 9 s, and none in the 108 s before. So: a picture is sent at most every SHOW_GAP seconds and only when it or its place changed,
-- and at most MAX_CONV conversions run at once (the newest wanted one goes next; the ones passed over are not made).
local SHOW_GAP, MAX_CONV = 0.04, 2
local shown_x, shown_y = nil, nil
local last_show, show_timer, show_args = -1e9, nil, nil
local running, waiting = 0, nil
local last_x, last_y = 0, 0
local gen = 0              -- bumped when another film is loaded: a conversion finishing after that belongs to the old film and is dropped
-- Why one ffmpeg run for the whole pack (2026-10-29, run at 18:59 with the throttled viewer, which still stuttered): while hovering, mpv's
-- "worker" thread (the pool that runs `subprocess`) went from nothing to 13-41 % of a core, the whole computer used 1.7 cores more than
-- everything we watch could explain, an ffmpeg was alive in 18 of 34 samples, and the audio ran dry 110 times and the display dropped 21
-- frames, all inside the hover and none outside it. Starting a program from inside a 1 GB, many-threaded mpv is the one thing the hover
-- did that plain mouse seeking does not. So the hover starts nothing now: the pack is turned into one raw file when the film loads, and a
-- hover only points overlay-add at the right offset in it. (If that cannot be done, the one-ffmpeg-per-picture path below still works.)
local bulk = { state = nil, file = nil, token = nil }     -- state: nil | "running" | "ready" | "failed"
local start_bulk, convert                              -- defined further down

local function dbg(...) if opts.debug then mp.msg.info(...) end end

local function expand(p)
    if not p or p == "" then return "" end
    local home = os.getenv("HOME") or ""
    p = p:gsub("^~", home):gsub("/+$", "")
    return p
end

local function read_ini()
    local f = io.open(expand("~/.config/seekthumbs/config.ini"), "r")
    if not f then return end
    local sect
    for line in f:lines() do
        local s = line:match("^%s*%[(.-)%]")
        if s then sect = s
        elseif sect == "seekthumbs" then
            local k, v = line:match("^%s*([%w_]+)%s*[=:]%s*(.-)%s*$")
            if k == "library" and opts.library == "" then opts.library = v end
            if k == "store" and opts.store == "" then opts.store = v end
            if k == "min_size_mb" and tonumber(v) and opts.min_size_mb == 300 then opts.min_size_mb = tonumber(v) end
        end
    end
    f:close()
end

local function json_file(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local s = f:read("*a")
    f:close()
    return utils.parse_json(s)
end

local function absolute(p)
    if p:sub(1, 1) == "/" then return p end
    local wd = mp.get_property("working-directory") or ""
    return utils.join_path(wd, p)
end

local function valid(i)
    return i and i.v == 1 and i.interval and i.count and i.offsets and i.lengths and i.w and i.h
end

-- The entry for `rel` in the store: a real entry, or an alias followed to the real one. Returns index, folder of the real entry, or nil.
-- `size` (if known) must equal the size recorded for `rel` itself.
local function load_entry(store, rel, size)
    local seen, cur, expect = {}, rel, nil
    for hop = 0, 4 do
        if seen[cur] or ("/" .. cur .. "/"):find("/%.%./") then return nil end
        seen[cur] = true
        local d = utils.join_path(store, cur .. SUFFIX)
        local i = json_file(utils.join_path(d, "index.json"))
        if type(i) ~= "table" then return nil end
        if hop == 0 and size and i.size ~= size then return nil end
        if expect and i.made ~= expect then return nil end
        if valid(i) then return i, d end
        if i.v == 2 and type(i.alias_of) == "string" and i.target_made then
            expect, cur = i.target_made, i.alias_of
        else
            return nil
        end
    end
    return nil
end

-- Returns the index table and its folder for the film that is playing, or nil.
local function locate()
    local path = mp.get_property("path")
    if not path or path:match("^%a[%w+.-]*://") then return nil end        -- streams have no local file
    path = absolute(path)
    local lib, store = expand(opts.library), expand(opts.store)
    if store == "" then return nil end
    local size = tonumber(mp.get_property("file-size"))
    local candidates = {}
    if lib ~= "" and path:sub(1, #lib + 1) == lib .. "/" then
        candidates[#candidates + 1] = path:sub(#lib + 2)
    end
    -- fallback: progressively shorter tails of the path
    local tail = path:gsub("^/", "")
    for _ = 1, 12 do
        candidates[#candidates + 1] = tail
        tail = tail:match("^[^/]*/(.+)$")
        if not tail then break end
    end
    for _, rel in ipairs(candidates) do
        local i, d = load_entry(store, rel, size)
        if i then return i, d, rel end
    end
    return nil
end

local function send_info()
    local avail = ix ~= nil
    local info = { width = avail and ix.w or 0, height = avail and ix.h or 0, disabled = not avail, available = avail,
                   socket = "", thumbnail = "", overlay_id = tonumber(opts.overlay_id) or 42 }
    mp.commandv("script-message", "thumbfast-info", utils.format_json(info))
end

local function clear()
    if show_timer then show_timer:kill(); show_timer = nil end
    show_args, waiting = nil, nil
    if shown ~= nil then mp.commandv("overlay-remove", tostring(opts.overlay_id)) end
    shown, shown_x, shown_y = nil, nil, nil
end

local VIDEO = { mkv = true, mp4 = true, m4v = true, avi = true, mov = true, ts = true, m2ts = true, webm = true, wmv = true }
local ini_done = false
local started = {}          -- path -> true: a build was started for it in this session
local timer, ticks = nil, 0
local last_check = -1e9

local function stop_timer()
    if timer then timer:kill(); timer = nil end
end

-- Looks for the entry of the playing film and, if it is there, starts using it. Returns true if it found one.
local function adopt()
    local i, d, rel = locate()
    if not i then return false end
    ix, edir = i, d
    if not tmpdir then
        local base = os.getenv("XDG_RUNTIME_DIR") or "/tmp"
        tmpdir = utils.join_path(base, "seekthumbs-" .. tostring(mp.get_property("pid") or os.time()))
        mp.command_native({ name = "subprocess", args = { "mkdir", "-p", tmpdir } })
    end
    mp.msg.info(string.format("seek thumbnails: %s (%d x %d, every %gs)", rel, ix.w, ix.h, ix.interval))
    start_bulk()
    return true
end

-- Starts the background build for the playing film when it has none (see the header). Everything about the decision is here.
local function maybe_build()
    if not opts.build or opts.builder == "" then return end
    local path = mp.get_property("path")
    if not path or path:match("^%a[%w+.-]*://") then return end
    path = absolute(path)
    local lib, store = expand(opts.library), expand(opts.store)
    if lib == "" or store == "" or path:sub(1, #lib + 1) ~= lib .. "/" then return end
    local ext = path:match("%.([^./]+)$")
    if not ext or not VIDEO[ext:lower()] then return end
    local size = tonumber(mp.get_property("file-size"))
    if size and size < (tonumber(opts.min_size_mb) or 300) * 1e6 then return end
    if not started[path] then
        started[path] = true
        -- min_size_mb is passed along so the build uses the same threshold as this check
        local args = { "run", opts.python, expand(opts.builder), "--library", lib, "--store", store, "--min-size-mb", tostring(tonumber(opts.min_size_mb) or 300), "build", path }
        mp.commandv((table.unpack or unpack)(args))
        mp.msg.info("seek thumbnails: none yet for this film; building them in the background (they appear when it is done)")
    end
    -- (a film loaded again while its build may still run keeps looking, without starting a second build)
    ticks = 0
    timer = mp.add_periodic_timer(tonumber(opts.recheck_s) or 10, function()
        ticks = ticks + 1
        if adopt() then stop_timer(); send_info() elseif ticks > 720 then stop_timer() end
    end)
end

local function remember(i, entry)
    cache[i] = entry
    order[#order + 1] = i
    while #order > opts.cache_items do
        local old = table.remove(order, 1)
        if cache[old] then os.remove(cache[old].file); cache[old] = nil end
    end
end

local function now() return mp.get_time and mp.get_time() or os.time() end

local function show_now(i, x, y)
    local e = cache[i]
    if not e or opts.no_overlay then return end
    mp.commandv("overlay-add", tostring(opts.overlay_id), tostring(x), tostring(y), e.file, string.format("%d", e.off or 0), "bgra",
                tostring(e.w), tostring(e.h), tostring(e.w * 4))
    shown, shown_x, shown_y, last_show = i, x, y, now()
end

local function show(i, x, y)
    if not cache[i] then return end
    x, y = math.floor(x), math.floor(y)
    if shown == i and shown_x == x and shown_y == y and not show_args then return end      -- nothing changed: do not make mpv redraw
    local wait = SHOW_GAP - (now() - last_show)
    if wait <= 0 then
        if show_timer then show_timer:kill(); show_timer = nil end
        show_args = nil
        show_now(i, x, y)
        return
    end
    show_args = { i, x, y }            -- too soon after the last one: keep only the newest and send it when the gap is over
    if not show_timer then
        show_timer = mp.add_timeout(wait, function()
            show_timer = nil
            local a = show_args
            show_args = nil
            if a and want == a[1] and cache[a[1]] then show_now(a[1], a[2], a[3]) end
        end)
    end
end

-- index of the thumbnail to use for a time; walks back over thumbnails that could not be made
local function pick(t)
    local i = math.floor(t / ix.interval)
    if i < 0 then i = 0 end
    if i > ix.count - 1 then i = ix.count - 1 end
    while i > 0 and (ix.lengths[i + 1] or 0) == 0 do i = i - 1 end
    if (ix.lengths[i + 1] or 0) == 0 then
        i = math.floor(t / ix.interval)
        if i > ix.count - 1 then i = ix.count - 1 end
        while i < ix.count - 1 and (ix.lengths[i + 1] or 0) == 0 do i = i + 1 end
    end
    if (ix.lengths[i + 1] or 0) == 0 then return nil end
    return i
end

-- One ffmpeg run turns the whole pack (JPEGs laid end to end) into one raw file of equal-sized frames. A picture that failed to build has
-- no bytes in the pack, so its frame number is the count of the pictures before it that have some. If the result is not exactly the size that
-- implies, nothing in it is trusted and the viewer falls back to one ffmpeg per picture.
function start_bulk()
    bulk.state = nil
    if not opts.bulk or opts.no_convert or not ix or not edir or not tmpdir then return end
    local frames, n = {}, 0
    for i = 0, ix.count - 1 do
        if (ix.lengths[i + 1] or 0) > 0 then frames[i] = n; n = n + 1 end
    end
    if n == 0 then return end
    local limit = (tonumber(opts.bulk_max_mb) or 256) * 1048576
    if n * ix.w * ix.h * 4 > limit then
        mp.msg.info(string.format("seek thumbnails: %d pictures would take more than %g MB raw; converting them one at a time instead", n, limit / 1048576))
        return
    end
    local out = utils.join_path(tmpdir, string.format("all-%d.bgra", gen))
    local mygen, w = gen, ix.w
    bulk.state, bulk.file = "running", out
    bulk.token = mp.command_native_async({ name = "subprocess", playback_only = false, capture_stdout = true, capture_stderr = true,
        args = { opts.ffmpeg, "-nostdin", "-v", "error", "-threads", "1", "-y", "-f", "image2pipe", "-c:v", "mjpeg",
                 "-i", utils.join_path(edir, "thumbs.pack"), "-f", "rawvideo", "-pix_fmt", "bgra", out } },
        function(ok, res)
            if mygen ~= gen then os.remove(out); return end
            local function fail(why)
                dbg("bulk conversion failed:", why)
                bulk.state = "failed"
                os.remove(out)
                if want ~= nil and ix and not cache[want] then convert(want, last_x, last_y) end
            end
            if not ok or not res or res.status ~= 0 then fail("ffmpeg"); return end
            local r = io.open(out, "rb")
            if not r then fail("no output"); return end
            local total = r:seek("end")
            r:close()
            local per = n * w * 4                         -- bytes of one "row" of frames, so the height is total / per
            local h = total and total >= per and math.floor(total / per) or 0
            if h < 1 or h * per ~= total then fail("size " .. tostring(total)); return end
            bulk.state = "ready"
            for i, fno in pairs(frames) do
                cache[i] = { file = out, off = fno * w * h * 4, w = w, h = h, shared = true }
            end
            mp.msg.info(string.format("seek thumbnails: %d pictures ready", n))
            if want ~= nil and cache[want] then show(want, last_x, last_y) end
        end)
end

function convert(i, x, y)
    if pending[i] then return end
    if running >= MAX_CONV then waiting = { i, x, y }; return end         -- only the newest one waits; the rest are passed over
    pending[i] = true
    local f = io.open(utils.join_path(edir, "thumbs.pack"), "rb")
    if not f then pending[i] = nil; return end
    f:seek("set", ix.offsets[i + 1])
    local bytes = f:read(ix.lengths[i + 1])
    f:close()
    local jpg = utils.join_path(tmpdir, string.format("%d.jpg", i))
    local raw = utils.join_path(tmpdir, string.format("%d.bgra", i))
    local o = io.open(jpg, "wb")
    if not o then pending[i] = nil; return end
    o:write(bytes)
    o:close()
    running = running + 1
    local mygen = gen
    mp.command_native_async({ name = "subprocess", playback_only = false, capture_stdout = true, capture_stderr = true,
        args = { opts.ffmpeg, "-nostdin", "-v", "error", "-threads", "1", "-y", "-i", jpg, "-f", "rawvideo", "-pix_fmt", "bgra", raw } },
        function(ok, res)
            running = running - 1
            pending[i] = nil
            os.remove(jpg)
            if mygen ~= gen then os.remove(raw); return end
            local function next_waiting()
                local w = waiting
                waiting = nil
                if w and want == w[1] and not cache[w[1]] then convert(w[1], last_x, last_y) end
            end
            if not ok or not res or res.status ~= 0 then dbg("ffmpeg failed for thumbnail", i); next_waiting(); return end
            local r = io.open(raw, "rb")
            if not r then next_waiting(); return end
            local n = r:seek("end")
            r:close()
            local w = ix and ix.w
            local h = w and math.floor(n / (w * 4)) or 0
            if h < 1 then next_waiting(); return end
            remember(i, { file = raw, w = w, h = h })
            if want == i then show(i, last_x, last_y) end        -- where the mouse is now, not where it was when this was asked for
            next_waiting()
        end)
end

local function on_thumb(t, x, y)
    if not ix then
        -- an OSC that still sends hovers: look again now and then (a build may have finished, started by this session or another program)
        local now = mp.get_time and mp.get_time() or os.time()
        if now - last_check >= 5 then
            last_check = now
            if adopt() then send_info() end
        end
        if not ix then return end
    end
    t, x, y = tonumber(t), tonumber(x), tonumber(y)
    if not (t and x and y) then return end
    local i = pick(t)
    if i == nil then return end
    want, last_x, last_y = i, x, y
    if opts.no_convert then return end
    if cache[i] then show(i, x, y)
    elseif bulk.state == "running" then return          -- the one conversion is on its way and shows what is wanted when it is done
    else convert(i, x, y) end
end

local function on_clear()
    want = nil
    clear()
end

local function on_file_loaded()
    clear()
    stop_timer()
    for k in pairs(cache) do if not cache[k].shared then os.remove(cache[k].file) end end
    if bulk.token and mp.abort_async_command then pcall(mp.abort_async_command, bulk.token) end     -- stop a conversion of the film that is leaving
    if bulk.file then os.remove(bulk.file) end
    bulk = { state = nil, file = nil, token = nil }
    cache, order, pending, want = {}, {}, {}, nil
    gen = gen + 1
    ix, edir = nil, nil
    if not ini_done then ini_done = true; read_ini() end     -- library=, store= (if not set in script-opts) and min_size_mb from the generator's config.ini
    if not adopt() then
        mp.msg.info("seek thumbnails: none for this file")
        maybe_build()
    end
    send_info()
end

mp.register_script_message("thumb", on_thumb)
mp.register_script_message("clear", on_clear)
mp.register_script_message("seekthumbs-reload", on_file_loaded)
mp.register_event("file-loaded", on_file_loaded)
mp.register_event("end-file", function() on_clear() end)
mp.register_event("shutdown", function()
    if tmpdir then mp.command_native({ name = "subprocess", args = { "rm", "-rf", tmpdir } }) end
end)
-- uosc asks for the state when it starts after this script
mp.register_script_message("thumbfast-query", send_info)
