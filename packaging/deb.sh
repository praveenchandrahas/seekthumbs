# shellcheck shell=bash disable=SC2034  # sourced by build-deb.sh, which uses these variables
# Package definition for kit/build-deb.sh (sourced; see Software-Defined-Home-Theatre/packaging/kit).
PKG=seekthumbs
HOMEPAGE=https://github.com/praveenchandrahas/seekthumbs
SUMMARY="seek-bar preview thumbnails for a film library, for mpv"
DESCRIPTION="Builds small seek-bar preview thumbnails for a film library into a local
store and shows them in mpv. Built for a read-only network mount with rate
limits: the library is only read, the thumbnails go to a separate folder.
Includes a local web page (127.0.0.1 only, one-time token) to pick films and
watch progress, and a background watcher for new films.
.
This package does not enable or start any service and does not write into any
home folder; run seekthumbs-setup for the mpv viewer and the watcher. The
on-play build is off unless you ask for it with seekthumbs-setup --on-play."
DEPENDS="python3, ffmpeg"
RECOMMENDS="python3-av, mpv"
SUGGESTS="mpv-shelf"
PYDIRS="/usr/lib/seekthumbs"

stage() {
  local L=/usr/lib/seekthumbs
  inst 755 seekthumbs.py "$L/seekthumbs.py"; pyshebang "$L/seekthumbs.py"
  inst 644 seekthumbs_web.py "$L/seekthumbs_web.py"
  inst 644 noindex.py "$L/noindex.py"
  inst 644 thumbfast.lua "$L/thumbfast.lua"
  inst 644 web/index.html "$L/web/index.html"
  script /usr/bin/seekthumbs <<'X'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/seekthumbs/seekthumbs.py "$@"
X
  inst 755 packaging/seekthumbs-setup /usr/bin/seekthumbs-setup
  inst 644 packaging/seekthumbs.service /usr/lib/systemd/user/seekthumbs.service
  inst 644 tools/mount-test.sh /usr/share/doc/seekthumbs/examples/mount-test.sh
  manpage seekthumbs "build seek-bar thumbnails for a film library (probe, plan, build, scan, watch, serve, status, verify)"
  manpage seekthumbs-setup "install the seekthumbs mpv viewer and the background watcher for one user" "Subcommands: install, uninstall, check. Options: --dry-run, --mpv-config-dir DIR, --on-play, --service."
}
