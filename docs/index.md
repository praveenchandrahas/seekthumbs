# seekthumbs

seekthumbs makes Netflix-style previews on the mpv seek bar (called *trickplay* or *seek thumbnails*) for a film library on a **read-only rclone mount**. The thumbnails are made in the background and kept in a local folder; the library itself is only ever read.

It is one part of the [Software Defined Home Theatre](https://github.com/praveenchandrahas/Software-Defined-Home-Theatre) project. The main repository there holds the overall documentation, build log, results and installer checks; sibling repositories cover the DSP/router configs, the player and Atmos wrapper, room measurement/autocal, and a film browser for mpv.

There are three ways to get thumbnails, and you can use any of them:

- **The web page** (`./seekthumbs.py serve`): tick the films you want, press Start, and watch it work.
- **On play** (off by default): once turned on, the first time you play a film with no thumbnails, the viewer starts a gentle background build of that one film.
- **Batch**: `scan` / `watch` build the whole library ahead of time.

In all three, releases of the same film share one set of thumbnails where their runtimes match closely enough — see [Usage](usage.md) for how that works.

See [Install](install.md) to get it onto your machine, and [Usage](usage.md) for the CLI subcommands, `config.ini` options, the web page and the mpv viewer.

!!! warning "Pre-release, experimental"
    Releases are marked pre-release. The hover preview has been confirmed in real mpv with uosc on a finished film; the on-play trigger has not. See the project's [README](https://github.com/praveenchandrahas/seekthumbs#not-verified-yet--known-weak-spots) for the full list of what is and isn't verified yet.
