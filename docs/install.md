# Install

## Package install (pre-release, experimental)

Releases are marked **pre-release** on the [releases page](https://github.com/praveenchandrahas/seekthumbs/releases). Download the `.deb` and `SHA256SUMS` from one release, check it, then install:

```sh
sha256sum -c --ignore-missing SHA256SUMS
sudo apt install ./seekthumbs_0.1.0_all.deb
seekthumbs-setup --dry-run   # then: seekthumbs-setup install
```

Needs ffmpeg and Python 3 (apt installs them). The web page listens on 127.0.0.1 only.

The package puts files under `/usr` only. Its install and remove scripts do not enable or start services, do not write into any home folder, and do not touch CamillaDSP or PipeWire configuration; `seekthumbs-setup` does the per-user part, and `--dry-run` shows what it would do first. Details, the other repositories' packages, and the one-command installer are in the main repository's [packaging notes](https://github.com/praveenchandrahas/Software-Defined-Home-Theatre/blob/main/docs/packaging.md).

What was checked: written with an AI assistant; used and checked by the author in his own room (one room, one author). No external or second-person validation yet. The packages are unsigned. CI installs and removes them in a clean Ubuntu 24.04 container; the packaging itself has not been run on the author's own rig.

## Setup (once the paths are known)

```bash
sudo apt install python3-av                      # recommended: opens each film once instead of once per thumbnail; the pip wheel lacks zscale (HDR)
mkdir -p ~/.config/seekthumbs
cat > ~/.config/seekthumbs/config.ini <<'INI'
[seekthumbs]
library = /path/to/the/rclone/mount/Movies
store   = /path/to/local/thumbnail/folder
INI
cd seekthumbs      # this repository
./seekthumbs.py probe "/path/to/one/film.mkv"     # seconds and MB received per thumbnail for each extractor, and the projected time for 100 such films
./seekthumbs.py plan                              # lists what would be built (directory listings only)
./seekthumbs.py scan --max-films 1                # build one film, look at the result (settings work before or after the subcommand)
./seekthumbs.py build "/path/to/one/film.mkv"     # what the viewer starts when you play a film with no thumbnails: one film, low priority, 2 reads at a time
./seekthumbs.py scan                              # the whole library, as fast as the mount allows (run it overnight; Ctrl+C is safe, it resumes)
./seekthumbs.py status                            # size per 2 h against the 10 MB budget
./seekthumbs.py install-service --enable          # afterwards: the background watcher picks up new films (systemd user service)
./seekthumbs.py install-viewer                    # the mpv side, into ~/.config/mpv-object-audio/scripts (see Usage); sets build=no (the viewer only shows thumbnails)
./seekthumbs.py install-viewer --on-play          # same, and turns the on-play build on (records builder= and build=yes); read the warning in Usage first
```

`min_size_mb` (default 300; the author sets 20000 in `config.ini`) decides which files count as films: everything smaller is ignored, by `plan`/`scan`/`watch`, by `build` and by the viewer's on-play check alike. Non-video files (`.iso`, `.rar`, `.nfo`, ...) are never considered whatever their size.

Adapting it to your system: the examples above use the author's mount style (an rclone mount, read-only) and paths; set `library` and `store` in `~/.config/seekthumbs/config.ini` to your own.

## Tests

`bash tests/run.sh` runs `tests/test_seekthumbs.py` (generator, viewer under a mocked mpv) and `tests/test_web.py` (the web page: its token and Host checks, the film list, queue, start, pause, resume, stop, and what ends up in the store). Needs ffmpeg; `pip install lupa av` adds the viewer and PyAV tests.

Next: see [Usage](usage.md) for the CLI subcommands, `config.ini` settings, the web page and the mpv viewer.
