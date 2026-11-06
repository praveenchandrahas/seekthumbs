#!/usr/bin/env python3
"""A small Real-Debrid REST client and a cache of the account's torrent library, standard library only.

This module is the source layer behind `source = rd` in seekthumbs.py. It does NOT download or decode anything: it
lists the account's torrents, picks the selected video files, maps each one to the path Decypharr gives the same file on
its mount, and resolves a short-lived direct URL for reading. The bytes still come from Real-Debrid's CDN and are read
with seekthumbs.RangeFile, exactly as they are from the mount.

Why the API at all: `/torrents` and `/torrents/info/{id}` are an authoritative inventory (file id, path, byte size,
selected flag), `/user` and `/traffic` say explicitly whether the account is usable, and `/unrestrict/link` can be
called again when a direct URL expires. That removes the directory walk and the guesswork from a flaky FUSE mount.
It does not change what a seek costs; see the README.

The token is read from RD_API_TOKEN first, then `rd_token` in the config. Never write it to a log, a store file, or a
commit. The on-disk inventory cache holds restricted links (not the token) and is created mode 0600.
"""
import datetime
import hashlib
import json
import os
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.real-debrid.com/rest/1.0"
DEFAULT_VIDEO_EXT = frozenset({".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".wmv"})
# Decypharr's folder_naming values, in the order tried after the configured one. Trying all of them costs only a few
# stat calls per film and makes the probe useful even before the owner has copied the right value into the config.
FOLDER_NAMINGS = ("filename", "original", "filename_no_ext", "original_no_ext", "infohash")


class RdError(Exception):
    """A Real-Debrid request failed, or its answer could not be used."""


class RdAuthError(RdError):
    """401/403: the token is invalid, expired, or the account is locked/unusable. Not a transient read error."""


class RdThrottle(RdError):
    """429: the API's 250 requests/minute limit (or a stronger temporary block) was reached."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class RdServerError(RdError):
    """5xx: Real-Debrid itself is failing. Retried, then treated as a provider pause, not a bad film."""


class RdNotFound(RdError):
    """404/410: the torrent/file/link no longer exists (or a download link has expired)."""


class RdBadRequest(RdError):
    """Any other 4xx: the request was understood but refused."""


def token_from(cfg):
    """The API token: RD_API_TOKEN wins over the config value, so a service can keep it out of the ini."""
    return (os.environ.get("RD_API_TOKEN") or (cfg or {}).get("rd_token") or "").strip()


def mask_token(token):
    if not token:
        return "(none)"
    return token[:4] + "..." + token[-4:] if len(token) > 12 else "***"


def parse_date(text):
    """Real-Debrid's JSON dates -> epoch seconds; 0 when the value is missing or not a date."""
    if not text:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _error_message(raw):
    try:
        d = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        return raw.decode("utf-8", "replace")[:200]
    return str(d.get("error") or d.get("error_code") or raw.decode("utf-8", "replace")[:200])


class RdClient:
    """Paced Real-Debrid REST calls. One instance can be shared by threads; the pacing lock only serializes the wait,
    not the request itself (the API's limit is per account, and a handful of concurrent thumbnail builds is small)."""

    def __init__(self, token, base=API_BASE, timeout=30.0, min_interval=0.26, retries=4, sleep=time.sleep, clock=time.monotonic, log=None):
        self.token = token or ""
        self.base = (base or API_BASE).rstrip("/")
        self.timeout = float(timeout)
        self.min_interval = max(0.0, float(min_interval))
        self.retries = max(0, int(retries))
        self.sleep, self.clock, self.log = sleep, clock, log or (lambda *a: None)
        self.last_request = 0.0
        self._pace_lock = threading.Lock()
        self.requests = 0          # for the probe: how many API calls this process made
        self.last_headers = {}

    def _pace(self):
        if self.min_interval <= 0:
            return
        with self._pace_lock:
            wait = self.min_interval - (self.clock() - self.last_request)
            if wait > 0:
                self.sleep(wait)
            self.last_request = self.clock()

    def _request(self, method, path, params=None, data=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json",
                   "User-Agent": "seekthumbs/realdebrid"}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        last = None
        for attempt in range(self.retries + 1):
            self._pace()
            self.requests += 1
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    self.last_headers = dict(r.headers)
                    status = getattr(r, "status", 200)
            except urllib.error.HTTPError as ex:
                raw, status = ex.read(), ex.code
                self.last_headers = dict(ex.headers)
            except (urllib.error.URLError, TimeoutError, OSError) as ex:
                last = RdServerError(f"{method} {path}: {ex}")
                if attempt < self.retries:
                    self.sleep(min(30.0, 1.5 * (2 ** attempt)))
                    continue
                raise last from None
            if status == 429:
                ra = self.last_headers.get("Retry-After")
                try:
                    wait = float(ra) if ra is not None else min(60.0, 2.0 * (2 ** attempt))
                except ValueError:
                    wait = min(60.0, 2.0 * (2 ** attempt))
                last = RdThrottle(_error_message(raw), retry_after=wait)
                if attempt < self.retries:
                    self.sleep(max(0.0, wait))
                    continue
                raise last
            if 500 <= status < 600:
                last = RdServerError(f"HTTP {status}: {_error_message(raw)}")
                if attempt < self.retries:
                    self.sleep(min(30.0, 1.5 * (2 ** attempt)))
                    continue
                raise last
            if status in (401, 403):
                raise RdAuthError(f"HTTP {status}: {_error_message(raw)}")
            if status in (404, 410):
                raise RdNotFound(f"HTTP {status}: {_error_message(raw)}")
            if status < 200 or status >= 300:
                raise RdBadRequest(f"HTTP {status}: {_error_message(raw)}")
            if not raw:
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError as ex:
                raise RdError(f"{method} {path}: answer is not JSON ({ex})") from None
        raise last or RdError(f"{method} {path}: failed")

    # -- account state
    def user(self):
        return self._request("GET", "/user") or {}

    def traffic(self):
        return self._request("GET", "/traffic") or {}

    # -- inventory
    def torrents_page(self, page=1, limit=100):
        return self._request("GET", "/torrents", params={"page": int(page), "limit": int(limit)}) or []

    def torrents(self, limit=100, max_pages=1000):
        """Every torrent, following the API's page/limit pagination. Stops on an empty or short page."""
        page = 1
        while page <= max_pages:
            batch = self.torrents_page(page, limit)
            if not batch:
                return
            for t in batch:
                yield t
            if len(batch) < int(limit):
                return
            page += 1

    def torrent_info(self, torrent_id):
        return self._request("GET", "/torrents/info/" + urllib.parse.quote(str(torrent_id), safe="")) or {}

    def unrestrict(self, link):
        return self._request("POST", "/unrestrict/link", data={"link": link}) or {}

    # -- optional metadata (used by rd-probe; not needed to read a file)
    def media_infos(self, file_id):
        return self._request("GET", "/streaming/mediaInfos/" + urllib.parse.quote(str(file_id), safe="")) or {}

    def transcode(self, file_id):
        return self._request("GET", "/streaming/transcode/" + urllib.parse.quote(str(file_id), safe="")) or {}


def downloadable(t):
    """True when a torrent in /torrents has bytes that can be read now. `downloaded` is the only status whose links
    are stable; uploading/compressing still change, and the rest cannot be read at all."""
    return str(t.get("status") or "").lower() == "downloaded"


def _summary(t):
    return dict(id=str(t.get("id") or ""), filename=str(t.get("filename") or ""), original_filename=str(t.get("original_filename") or t.get("filename") or ""),
                hash=str(t.get("hash") or ""), bytes=int(t.get("bytes") or 0), status=str(t.get("status") or ""),
                progress=float(t.get("progress") or 0), added=t.get("added") or "")


def _strip_ext(name):
    return os.path.splitext(name)[0]


def files_from_info(info, video_ext=DEFAULT_VIDEO_EXT, min_size=0):
    """The readable video files of one /torrents/info answer, with the restricted link that belongs to each.

    Real-Debrid returns `files` in torrent order and `links` for the files whose `selected` flag is 1, in the same
    order (this is also how Decypharr pairs them). A RAR torrent (one link, several selected files) is left out: its
    link is the archive, not any of the files, and reading it as if it were the film would decode nothing."""
    selected = [f for f in (info.get("files") or []) if int(f.get("selected") or 0) == 1]
    links = [u for u in (info.get("links") or []) if isinstance(u, str) and u]
    if not selected or not links:
        return []
    if len(links) == 1 and len(selected) > 1:
        return []
    out = []
    for i, f in enumerate(selected):
        if i >= len(links):
            break
        path = str(f.get("path") or "").replace("\\", "/")
        name = path.rsplit("/", 1)[-1]
        if os.path.splitext(name)[1].lower() not in video_ext:
            continue
        size = int(f.get("bytes") or 0)
        if size < int(min_size):
            continue
        out.append(dict(torrent_id=str(info.get("id") or ""), torrent_filename=str(info.get("filename") or ""),
                        original_filename=str(info.get("original_filename") or info.get("filename") or ""),
                        hash=str(info.get("hash") or ""), file_id=f.get("id"), file_path=path, file_name=name,
                        size=size, link=links[i], added=info.get("added") or "", status=str(info.get("status") or "")))
    return out


def _clean_rel(text):
    """A torrent/file name made safe as a mount-relative path: no absolute start, no . or .. component."""
    parts = [p for p in str(text or "").replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


def mount_candidates(item, naming="filename"):
    """Where Decypharr may put this file under `__all__`, in the order most likely first.

    Decypharr's RD mount is `<mount>/__all__/<folder_naming value>/<file basename>`. `filename` (the RD torrent
    filename) is its default. `original` uses RD's original_filename, the `*_no_ext` variants drop the extension, and
    `infohash` uses the torrent hash. Every variant is returned, configured one first, because a wrong guess costs a
    stat and a wrong guess is otherwise invisible (the store entry exists but the viewer never finds it)."""
    folder_names = {}
    folder_names["filename"] = item.get("torrent_filename") or ""
    folder_names["original"] = item.get("original_filename") or item.get("torrent_filename") or ""
    folder_names["filename_no_ext"] = _strip_ext(folder_names["filename"])
    folder_names["original_no_ext"] = _strip_ext(folder_names["original"])
    folder_names["infohash"] = item.get("hash") or ""
    order = ([naming] if naming in folder_names else []) + [n for n in FOLDER_NAMINGS if n != naming]
    file_name = _clean_rel(item.get("file_name") or "")
    file_rel = _clean_rel(item.get("file_path") or "")
    if not file_name:
        return []
    out = []
    for key in order:
        folder = _clean_rel(folder_names.get(key))
        if folder:
            out.append(folder + "/" + file_name)       # Decypharr puts the file directly under the item folder
            if file_rel and file_rel != file_name:
                out.append(folder + "/" + file_rel)    # ...but keep the nested path for versions that preserve it
        if file_rel:
            out.append(file_rel)
    out.append(file_name)
    seen, unique = set(), []
    for rel in out:
        if rel and rel not in seen:
            seen.add(rel)
            unique.append(rel)
    return unique


def mount_prefixes(library):
    """The prefixes to try under `library`. Decypharr's tree is `<mount>/__all__/...`, while seekthumbs' `library` may
    already point at `__all__`. Try the actual child first when it exists, then the library itself."""
    if library and os.path.isdir(os.path.join(library, "__all__")):
        return ["__all__", ""]
    return [""]


def find_on_mount(library, candidates, size=None):
    """The first (rel, stat) whose file exists and, when `size` is given, has exactly that size. None otherwise.
    An exact size is required because the store uses it to tell a replaced film from the one whose thumbnails exist."""
    if not library:
        return None
    for prefix in mount_prefixes(library):
        for rel in candidates:
            full = os.path.join(library, *([prefix] if prefix else []), *rel.split("/"))
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not os.path.isfile(full):
                continue
            if size is None or st.st_size == int(size):
                return ((prefix + "/" + rel) if prefix else rel), st
    return None


def norm_name(name):
    return unicodedata.normalize("NFC", str(name or ""))


def mount_index(library, video_ext=DEFAULT_VIDEO_EXT, min_size=0):
    """(normalised basename, size) -> [rel, ...] for every video under `library`. Only built when some RD item could
    not be mapped by its expected path: it lets a naming mismatch be repaired by identity (same file name and byte
    size) instead of silently leaving thumbnails the viewer cannot find. Directory listings only, no file is opened."""
    idx = {}
    if not library:
        return idx
    for dp, dn, fn in os.walk(library):
        dn[:] = sorted(d for d in dn if not d.startswith("."))
        for name in fn:
            if name.startswith(".") or os.path.splitext(name)[1].lower() not in video_ext:
                continue
            full = os.path.join(dp, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_size < int(min_size):
                continue
            rel = os.path.relpath(full, library)
            idx.setdefault((norm_name(name), st.st_size), []).append(rel)
    return idx


class Inventory:
    """The account's torrents, cached in the store so a scan does not re-ask 1000 questions every 30 minutes.

    `/torrents` is fetched when the cached list is older than `list_ttl`. `/torrents/info/{id}` is fetched once per
    torrent and reused for `info_ttl`; a downloaded torrent's file list does not normally change. A 404 for one
    torrent drops it; a 429/5xx/auth error propagates so seekthumbs can pause instead of marking films failed."""

    def __init__(self, client, cache_path, video_ext=DEFAULT_VIDEO_EXT, min_size=0, list_ttl=900.0, info_ttl=86400.0,
                 log=None, clock=time.time):
        self.client, self.cache_path = client, cache_path
        self.video_ext, self.min_size = video_ext, int(min_size)
        self.list_ttl, self.info_ttl, self.clock = float(list_ttl), float(info_ttl), clock
        self.log = log or (lambda *a: None)
        self.lock = threading.RLock()
        self.d = self._load()
        self.stats = dict(torrents=0, downloaded=0, info_calls=0, files=0, rar=0)

    def _load(self):
        try:
            with open(self.cache_path) as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("list"), list) and isinstance(d.get("torrents"), dict):
                return d
        except (OSError, ValueError, TypeError):
            pass
        return dict(listed=0.0, list=[], torrents={})

    def save(self):
        d = os.path.dirname(self.cache_path)
        try:
            os.makedirs(d, exist_ok=True)
            tmp = self.cache_path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.d, f, separators=(",", ":"))
            os.replace(tmp, self.cache_path)
            try:
                os.chmod(self.cache_path, 0o600)
            except OSError:
                pass
        except OSError as ex:
            self.log(f"Real-Debrid: could not save the inventory cache ({ex}); continuing without it")

    def _refresh_list(self, now):
        summaries = [_summary(t) for t in self.client.torrents()]
        self.d["list"] = [t for t in summaries if t["id"]]
        self.d["listed"] = now
        ids = {t["id"] for t in self.d["list"]}
        self.d["torrents"] = {k: v for k, v in self.d["torrents"].items() if k in ids}
        self.stats["torrents"] = len(self.d["list"])
        self.log(f"Real-Debrid: {len(self.d['list'])} torrent(s) in the account, "
                 f"{sum(1 for t in self.d['list'] if downloadable(t))} downloaded")

    def _ensure_info(self, now, force=False):
        got = 0
        for t in self.d["list"]:
            if not downloadable(t):
                continue
            ent = self.d["torrents"].get(t["id"])
            if not force and ent and ent.get("info") and now - float(ent.get("seen") or 0) <= self.info_ttl:
                continue
            try:
                info = self.client.torrent_info(t["id"])
            except RdNotFound:
                self.log(f"Real-Debrid: torrent {t['id']} disappeared while listing; skipped")
                continue
            self.d["torrents"][t["id"]] = dict(seen=now, info=info)
            got += 1
            if got % 25 == 0:
                self.log(f"Real-Debrid: read the file list of {got} torrent(s) so far...")
            self.stats["info_calls"] = self.stats.get("info_calls", 0) + 1
        if got:
            self.save()

    def items(self, refresh=False):
        """All readable selected video files, as dicts ready for mount_candidates()."""
        with self.lock:
            now = self.clock()
            listed = float(self.d.get("listed") or 0)
            if refresh or listed <= 0 or now - listed > self.list_ttl:   # listed <= 0: a new/empty cache must ask once
                self._refresh_list(now)
            self._ensure_info(now, force=refresh)
            out, rar = [], 0
            for t in self.d["list"]:
                if not downloadable(t):
                    continue
                info = (self.d["torrents"].get(t["id"]) or {}).get("info")
                if not info:
                    continue
                files = files_from_info(info, self.video_ext, self.min_size)
                if not files and len(info.get("links") or []) == 1 and \
                        sum(1 for f in (info.get("files") or []) if int(f.get("selected") or 0) == 1) > 1:
                    rar += 1
                out.extend(files)
            self.stats.update(files=len(out), rar=rar, downloaded=sum(1 for t in self.d["list"] if downloadable(t)))
            return out

    def refresh_torrent(self, torrent_id):
        """Re-read one torrent's file list (after a link stopped working), update the cache and return its info."""
        with self.lock:
            info = self.client.torrent_info(torrent_id)
            self.d["torrents"][str(torrent_id)] = dict(seen=self.clock(), info=info)
            self.save()
            return info


class LinkCache:
    """Restricted host link -> direct download URL. Direct URLs are pre-signed and expire; `force=True` asks for a new
    one. Nothing is written to disk: a restart simply resolves again."""

    def __init__(self, client, ttl=1500.0, clock=time.time):
        self.client, self.ttl, self.clock = client, float(ttl), clock
        self.lock = threading.Lock()
        self.cache = {}

    def get(self, link, force=False):
        if not link:
            raise RdError("no Real-Debrid link to resolve")
        now = self.clock()
        with self.lock:
            ent = self.cache.get(link)
            if ent and not force and ent[1] > now:
                return ent[0]
        data = self.client.unrestrict(link)
        url = data.get("download")
        if not url:
            raise RdError("Real-Debrid returned no download URL")
        with self.lock:
            self.cache[link] = (url, self.clock() + self.ttl)
        return url

    def forget(self, link):
        with self.lock:
            self.cache.pop(link, None)


def cache_name(token, store):
    """The inventory cache path for one account. The token itself never appears in the name."""
    tag = hashlib.sha1((token or "").encode()).hexdigest()[:12]
    return os.path.join(store, f".rd-inventory-{tag}.json")
