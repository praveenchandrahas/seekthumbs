#!/usr/bin/env bash
# Smoke test of the INSTALLED seekthumbs package. Throw-away HOME; a 20-second synthetic film made with ffmpeg; the page on 127.0.0.1 only.
#   scripts/ci-smoke.sh
set -euo pipefail
H=$(mktemp -d); SRV=
cleanup() { if [ -n "$SRV" ]; then kill "$SRV" 2>/dev/null || true; fi; rm -rf "$H"; }
trap cleanup EXIT
export HOME=$H XDG_CONFIG_HOME=$H/.config SDHT_ALLOW_ROOT=1   # the clean-environment test runs as root, with a throw-away HOME
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
for c in seekthumbs seekthumbs-setup; do command -v "$c" >/dev/null || fail "$c is not on PATH"; done
[ "$(command -v seekthumbs)" = /usr/bin/seekthumbs ] || fail "seekthumbs resolves to $(command -v seekthumbs)"
LIB=$H/lib STORE=$H/store
mkdir -p "$LIB" "$STORE"
ffmpeg -v error -y -f lavfi -i testsrc2=s=320x180:r=12 -t 20 -g 120 -c:v libx264 -preset ultrafast -pix_fmt yuv420p "$LIB/Smoke.Film.2020.1080p.mkv"
touch -d '2 hours ago' "$LIB/Smoke.Film.2020.1080p.mkv"
OPTS=(--library "$LIB" --store "$STORE" --interval 5 --min-size-mb 0 --settle 0 --yield-to-playback 0 --workers 1 --extractor ffmpeg)

echo "== plan, scan, status, verify on a synthetic film"
seekthumbs "${OPTS[@]}" plan | tee "$H/plan.txt"
seekthumbs "${OPTS[@]}" scan --max-films 1 >"$H/scan.txt" 2>&1 || { tail -20 "$H/scan.txt"; fail "scan"; }
IDX=$(find "$STORE" -name index.json | head -1)
[ -n "$IDX" ] || { tail -20 "$H/scan.txt"; fail "no index.json written"; }
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["count"]>=3, d; print("thumbnails:", d["count"], "interval", d["interval"])' "$IDX"
seekthumbs "${OPTS[@]}" status
seekthumbs "${OPTS[@]}" verify || fail "verify"

echo "== the web page: loopback only, token required"
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
seekthumbs "${OPTS[@]}" serve --port "$PORT" >"$H/serve.txt" 2>&1 &
SRV=$!
for _ in $(seq 1 50); do grep -q 'http://127.0.0.1' "$H/serve.txt" && break; sleep 0.2; done
URL=$(grep -o 'http://127.0.0.1:[0-9]*/?t=[A-Za-z0-9_-]*' "$H/serve.txt" | head -1)
[ -n "$URL" ] || { cat "$H/serve.txt"; fail "no page address printed"; }
python3 - "$URL" <<'PY' || { cat "$H/serve.txt"; fail "web page"; }
import http.client, sys, urllib.parse
u = urllib.parse.urlparse(sys.argv[1]); tok = urllib.parse.parse_qs(u.query)["t"][0]
def get(path, host=None, token=None):
    c = http.client.HTTPConnection("127.0.0.1", u.port, timeout=5)
    h = {"Host": host or f"127.0.0.1:{u.port}"}
    if token: h["X-Token"] = token
    c.request("GET", path, headers=h); r = c.getresponse(); return r.status, r.read()
st, body = get(f"/?t={tok}"); assert st == 200 and b"seekthumbs" in body, ("page", st)
assert get("/")[0] == 403, "page served without a token"
assert get("/api/status", token=tok)[0] == 200, "api with token"
assert get("/api/status", host="evil.example", token=tok)[0] == 403, "foreign Host accepted"
print("page ok; no token refused; foreign Host refused")
PY
python3 - "$SRV" <<'PY' || fail "the page listens on something other than loopback"
import os, sys
pid = sys.argv[1]
inodes = set()
for fd in os.listdir(f"/proc/{pid}/fd"):
    try: t = os.readlink(f"/proc/{pid}/fd/{fd}")
    except OSError: continue
    if t.startswith("socket:["): inodes.add(t[8:-1])
bad, seen = [], 0
for fn, loop in (("/proc/net/tcp", {"0100007F"}), ("/proc/net/tcp6", {"00000000000000000000000001000000", "0000000000000000FFFF00000100007F"})):
    try: lines = open(fn).read().splitlines()[1:]
    except OSError: continue
    for l in lines:
        f = l.split()
        if f[3] == "0A" and f[9] in inodes:              # LISTEN sockets of this process only
            seen += 1
            addr, port = f[1].split(":")
            if addr not in loop: bad.append((fn, addr, int(port, 16)))
if bad or not seen: print("listeners of the page process:", seen, "non-loopback:", bad); sys.exit(1)
print(f"the page process has {seen} listening socket(s), all on 127.0.0.1 or ::1")
PY
kill "$SRV"; wait "$SRV" 2>/dev/null || true; SRV=

echo "== setup command (viewer into a temp folder; then dry runs)"
seekthumbs-setup install --mpv-config-dir "$H/mpvconf" | tail -4
[ -f "$H/mpvconf/scripts/thumbfast.lua" ] || fail "viewer not installed"
grep -q '^build=no' "$H/mpvconf/script-opts/seekthumbs.conf" || fail "on-play build is not off by default"
seekthumbs-setup uninstall --dry-run --mpv-config-dir "$H/mpvconf" | tee "$H/un.txt"
[ -f "$H/mpvconf/scripts/thumbfast.lua" ] || fail "dry-run uninstall removed the viewer"
seekthumbs-setup --dry-run install --service | grep -q 'would run: systemctl --user enable --now seekthumbs.service' || fail "dry-run text"
[ ! -e "$H/.config/mpv-object-audio" ] || fail "dry run created the default mpv folder"
seekthumbs-setup uninstall --mpv-config-dir "$H/mpvconf" >/dev/null 2>&1 || true
[ ! -e "$H/mpvconf/scripts/thumbfast.lua" ] || fail "uninstall left the viewer"
echo "SMOKE OK (seekthumbs)"
