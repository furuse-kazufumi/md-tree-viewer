# -*- coding: utf-8 -*-
"""md_tree_viewer — a local, mostly read-only web viewer that lists the Markdown /
PDF / SVG files under a directory tree and renders the selected one.

Usage:
    mdtree                      # scan the current directory, port 8765, open browser
    mdtree path/to/dir
    mdtree --port 9000 --no-browser
    mdtree --ext ".md,.rst"     # override the viewable extensions
    mdtree --enable-open        # allow OS-association launch of non-viewable files

Features:
- Left pane: a collapsible tree of just the viewable files under the root (with a
  search filter) plus a settings panel.
- Each .md shows its title + opening description (so the filename alone is not
  the only clue to its content).
- Right pane: .md is rendered (GFM tables/code/Mermaid), .pdf is embedded, .svg
  is shown as an image.
- Dependency dirs, virtualenvs and .git are skipped while scanning (fast, no noise).
- Settings (viewable extensions, per-project icons, theme, enable-open) persist to
  a single config file (``<root>/.mdtree.json`` or ``~/.md_tree_viewer.json``).
- Mostly read-only. GET serves only viewable files under the root, outside pruned
  dirs (.git/node_modules/…), with path traversal and symlink escape prevented.
  The ONLY write endpoint, POST /api/config, writes that one config file (never a
  symlink) and nothing else. POST /api/open launches a root-confined, non-pruned,
  non-executable file with its OS association, and is disabled by default (opt-in
  via --enable-open / config). Both POSTs require a per-process CSRF token and a
  loopback Host/Origin (fail-closed), so a browser page cannot forge them.
- Dependencies: Python standard library only (rendering uses marked.js +
  mermaid.js loaded from a CDN).
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import secrets
import sys
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import webbrowser


def _gh_base(repo: Path) -> str | None:
    """Return the repo's GitHub blob base URL
    (https://github.com/<owner>/<repo>/blob/<branch>) or None.

    Returns None when there is no remote / it is not GitHub / git is absent.
    Push state is irrelevant (the URL is built even for unpushed branches).
    """
    try:
        url = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if not url:
            return None
        m = re.search(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?$", url)
        if not m:
            return None
        branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, timeout=5).stdout.strip() or "main"
        return f"https://github.com/{m.group(1)}/{m.group(2)}/blob/{branch}"
    except (OSError, subprocess.SubprocessError):
        return None


# Scan root. Defaults to the current working directory; override with the
# positional `root` argument. main() resolves the actual value.
ROOT = Path.cwd()

# Directories that are never scanned (=never shown in the tree). Dependency dirs,
# virtualenvs, caches and hidden dirs are skipped whole.
NOISE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", "envs", "virtualenv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".idea",
    ".vscode", "site-packages", "target", ".cache", ".eggs", ".next", ".gradle", "htmlcov",
}

# Default viewable extensions. The active set lives in VIEW_EXT, which the config
# file and the --ext CLI flag may override at startup (see load_config / main).
DEFAULT_VIEW_EXT = (".md", ".markdown", ".pdf", ".svg")
VIEW_EXT: tuple[str, ...] = DEFAULT_VIEW_EXT

# Extensions that the viewer renders inline (everything else, even if listed in
# VIEW_EXT, is "non-viewable" and can only be opened via OS association).
RENDERABLE_EXT = (".md", ".markdown", ".pdf", ".svg")

# Extensions that the OS "open" association would EXECUTE rather than view
# (ShellExecute on Windows runs these). POST /api/open refuses them so the
# OS-association feature cannot become a one-click code-execution primitive for a
# malicious file that happens to sit under the root. This is a hard server-side
# deny-list independent of VIEW_EXT (config cannot widen it).
EXECUTABLE_EXT = frozenset({
    ".exe", ".com", ".scr", ".pif", ".cpl", ".msi", ".msp", ".mst",
    ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".ps1xml", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".wsh", ".ws", ".hta", ".sct",
    ".lnk", ".url", ".reg", ".inf", ".scf", ".jar", ".gadget", ".application",
    ".msc", ".sh", ".bash", ".zsh", ".command", ".app", ".dll", ".sys", ".drv",
})

# Per-project (top-level dir) emoji icons baked into a distribution. The OSS
# package ships an EMPTY map so it never hard-codes anyone's project names; a
# private/local build may seed this with its own defaults. config.project_icons
# always takes precedence, and an unset project falls back to a colour dot.
REPO_ICON: dict[str, str] = {}

# Whether POST /api/open (OS-association launch of non-viewable files) is allowed.
# Default OFF for the OSS package; enabled only via --enable-open or config.
ENABLE_OPEN = False

# A per-process random token, regenerated on every start, embedded in the served
# HTML and required as the X-CSRF-Token header on every POST. Because a custom
# header is not a CORS "simple request", a cross-origin page cannot set it without
# a pre-flight (which same-origin policy will block), so this defeats the classic
# form/fetch CSRF against the loopback server. Same-origin requests (the viewer's
# own UI) read the token from the page and send it back.
CSRF_TOKEN = secrets.token_urlsafe(32)

# Active config (in memory). Mirrors the on-disk config file 1:1.
CONFIG: dict = {}
# Resolved path of the single config file this process reads/writes (set by
# load_config). The ONLY path POST /api/config is ever allowed to write.
CONFIG_PATH: Path | None = None

# The complete set of keys the config file is allowed to carry. POST bodies are
# filtered to these keys so an attacker cannot stash arbitrary data in the file.
CONFIG_KEYS = ("view_ext", "project_icons", "enable_open", "theme", "ignore")

# Extra directory names to skip while scanning, merged with NOISE_DIRS at lookup
# time. Populated from config `ignore: [...]` (v0.3). Names only (no path
# separators); compared case-insensitively against each directory component.
IGNORE_DIRS: frozenset[str] = frozenset()

# Whether the persistent scan cache (~/.md_tree_viewer/cache/<roothash>.json) is
# used. Disabled with --no-cache. The cache stores a tree snapshot plus per-dir
# mtimes so startup re-scans only the directories that changed.
USE_CACHE = True


def _config_candidates(root: Path) -> list[Path]:
    """Config search/write order: <root>/.mdtree.json first, then the per-user
    ~/.md_tree_viewer.json. These are the ONLY two locations ever touched."""
    cands = [root / ".mdtree.json"]
    try:
        cands.append(Path.home() / ".md_tree_viewer.json")
    except (RuntimeError, OSError):
        pass
    return cands


def _normalise_ext_list(value) -> list[str]:
    """Coerce an extension list/string into a clean list of '.ext' tokens
    (lower-cased, dot-prefixed, de-duplicated, order preserved)."""
    if isinstance(value, str):
        items = re.split(r"[,\s]+", value)
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []
    out: list[str] = []
    for it in items:
        e = str(it).strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        if e not in out:
            out.append(e)
    return out


def _coerce_config(raw: dict) -> dict:
    """Validate/sanitise a config dict, keeping only known keys with the right
    shapes. Unknown keys and malformed values are dropped (fail-closed)."""
    cfg: dict = {}
    if not isinstance(raw, dict):
        return cfg
    ext = _normalise_ext_list(raw.get("view_ext"))
    if ext:
        cfg["view_ext"] = ext
    icons = raw.get("project_icons")
    if isinstance(icons, dict):
        cfg["project_icons"] = {
            str(k): str(v) for k, v in icons.items() if str(k).strip() and str(v).strip()
        }
    if isinstance(raw.get("enable_open"), bool):
        cfg["enable_open"] = raw["enable_open"]
    theme = raw.get("theme")
    if isinstance(theme, str) and theme in ("light", "dark"):
        cfg["theme"] = theme
    ignore = _normalise_ignore_list(raw.get("ignore"))
    if ignore:
        cfg["ignore"] = ignore
    return cfg


def _normalise_ignore_list(value) -> list[str]:
    """Coerce an `ignore` value into a clean list of bare directory NAMES.

    Only simple names are accepted — any token containing a path separator
    (``/``, ``\\``) or path-traversal (``..``) is dropped, so the ignore list can
    only ever *exclude* directories from the scan and can never be turned into a
    path/escape primitive. Lower-cased, de-duplicated, order preserved."""
    if isinstance(value, str):
        items = re.split(r"[,\s]+", value)
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []
    out: list[str] = []
    for it in items:
        n = str(it).strip().lower()
        if not n or n in ("..", "."):
            continue
        if "/" in n or "\\" in n:
            continue
        if n not in out:
            out.append(n)
    return out


def _read_config_file() -> dict:
    """Read CONFIG_PATH if it exists; return a sanitised dict (empty on any
    error or absence — fail-safe, never raises)."""
    if CONFIG_PATH is None or not CONFIG_PATH.is_file():
        return {}
    try:
        return _coerce_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _write_config_file(cfg: dict) -> None:
    """Write the sanitised config to CONFIG_PATH (the single permitted write
    target). Raises OSError on failure; callers map that to a 500.

    Symlink hardening: the read side (`_safe_resolve`) resolves symlinks and
    re-checks containment, so the write side does the same here. If CONFIG_PATH
    is (or resolves through) a symlink, the write is refused — otherwise a
    symlink pre-planted at `<root>/.mdtree.json` pointing outside the root could
    redirect the only write target to an arbitrary victim file. Refusing keeps
    the write confined to a real regular file at the fixed config path."""
    if CONFIG_PATH is None:
        raise OSError("config path not initialised")
    if CONFIG_PATH.is_symlink():
        raise OSError("config path is a symlink; refusing to write")
    # If the file already exists it must be a plain regular file (not a symlink,
    # FIFO, device, etc.); a fresh write to a non-existent path is fine.
    if CONFIG_PATH.exists() and not CONFIG_PATH.is_file():
        raise OSError("config path is not a regular file; refusing to write")
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _apply_config(cfg: dict) -> None:
    """Apply a sanitised config to the in-memory globals (VIEW_EXT, REPO_ICON
    precedence is resolved at lookup time, ENABLE_OPEN)."""
    global VIEW_EXT, ENABLE_OPEN, CONFIG, IGNORE_DIRS
    CONFIG = cfg
    ext = cfg.get("view_ext")
    VIEW_EXT = tuple(ext) if ext else DEFAULT_VIEW_EXT
    if "enable_open" in cfg:
        ENABLE_OPEN = bool(cfg["enable_open"])
    IGNORE_DIRS = frozenset(cfg.get("ignore") or ())


def load_config(root: Path) -> dict:
    """Locate and load the config file for this root, set CONFIG_PATH to the
    file that will be read/written, and apply it to the globals. The first
    existing candidate is used for reads; if none exists, the first candidate
    (``<root>/.mdtree.json``) is where a future POST will write."""
    global CONFIG_PATH
    cands = _config_candidates(root)
    CONFIG_PATH = cands[0]
    for c in cands:
        if c.is_file():
            CONFIG_PATH = c
            break
    cfg = _read_config_file()
    _apply_config(cfg)
    return cfg


def project_icon(name: str) -> str:
    """Resolve the emoji icon for a top-level project dir: config.project_icons
    first, then the baked-in REPO_ICON default, else '' (client uses a colour
    dot fallback)."""
    icons = CONFIG.get("project_icons") or {}
    if name in icons:
        return icons[name]
    return REPO_ICON.get(name, "")


def config_payload() -> dict:
    """The config object served by GET /api/config and consumed by the UI. Always
    reports the effective view_ext / enable_open even when the file is sparse."""
    return {
        "view_ext": list(VIEW_EXT),
        "project_icons": dict(CONFIG.get("project_icons") or REPO_ICON),
        "enable_open": bool(ENABLE_OPEN),
        "theme": CONFIG.get("theme", "light"),
        "ignore": sorted(IGNORE_DIRS),
        "default_view_ext": list(DEFAULT_VIEW_EXT),
        "renderable_ext": list(RENDERABLE_EXT),
        "config_path": str(CONFIG_PATH) if CONFIG_PATH else "",
    }


def _skip_dir(name: str) -> bool:
    n = name.lower()
    if n in NOISE_DIRS or n in IGNORE_DIRS or n.startswith("."):
        return True
    # virtualenvs / package metadata are matched by substring/suffix.
    if "venv" in n or n.endswith(".egg-info") or n.endswith(".dist-info"):
        return True
    return False


_MD_INLINE = re.compile(r"[*_`>#\[\]]|\!\[|\]\([^)]*\)")
# A short label line (e.g. "Created: ...", "Status: x") is metadata, deprioritised
# as the description.
_META_LINE = re.compile(r"^\**\s*[^:：\n]{1,16}[:：]\s*\S")


def _extract_meta(path: Path) -> tuple[str, str]:
    """Extract the title (first heading) and opening description (first paragraph)
    from the start of a .md file."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return path.stem, ""
    lines = head.splitlines()
    i, fm_desc = 0, None
    # Prefer the YAML frontmatter `description:` when present.
    if lines and lines[0].strip() == "---":
        j = 1
        while j < len(lines) and lines[j].strip() != "---":
            m = re.match(r"\s*description\s*:\s*(.+)", lines[j])
            if m:
                fm_desc = m.group(1).strip().strip("\"'")
            j += 1
        i = j + 1
    body = lines[i:]
    title, rest = path.stem, []
    for k, ln in enumerate(body):
        s = ln.strip()
        if not s:
            continue
        title = s.lstrip("#").strip() if s.startswith("#") else s
        rest = body[k + 1:]
        break
    if fm_desc:
        desc = fm_desc
    else:
        desc = ""
        fallback = ""
        for ln in rest:
            s = ln.strip()
            if not s or s.startswith("#") or s.startswith("```") or s.startswith("---") \
               or s.startswith("|") or s.startswith("- [") or s.startswith(">"):
                continue
            clean = _MD_INLINE.sub("", s).strip()
            if not clean:
                continue
            if not fallback:
                fallback = clean              # fallback if there is no prose line
            if _META_LINE.match(s):
                continue                      # skip "Created: ..." style label lines
            if len(clean) >= 12:
                desc = clean                  # first prose line (>= 12 chars) wins
                break
        if not desc:
            desc = fallback
    return _MD_INLINE.sub("", title).strip()[:140], desc[:200]


# --------------------------------------------------------------------------- #
# Persistent scan cache (v0.3). The ONLY directory this process writes to beyond
# the single config file: ~/.md_tree_viewer/cache/. The cache stores, per root, a
# snapshot of every scanned directory keyed by that directory's mtime, so startup
# re-scans only the directories that changed.
# --------------------------------------------------------------------------- #

# Bumped when the on-disk cache JSON shape changes, so an old cache is ignored.
_CACHE_VERSION = 1


def _cache_dir() -> Path | None:
    """The single directory the scan cache may ever write to:
    ``~/.md_tree_viewer/cache``. Returns None if the home dir is unavailable."""
    try:
        return Path.home() / ".md_tree_viewer" / "cache"
    except (RuntimeError, OSError):
        return None


def _scan_signature() -> str:
    """A short, stable hash of the inputs that change what the scan would emit
    (active VIEW_EXT + ignore set). Mixed into the cache file name so a config
    change does not silently serve a stale tree built under a different config."""
    payload = json.dumps(
        {"ext": sorted(VIEW_EXT), "ignore": sorted(IGNORE_DIRS)},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _root_hash(root: Path) -> str:
    """A stable filesystem-safe hash for a root path (so two roots never collide
    on one cache file). Uses the resolved, normalised absolute path."""
    try:
        key = str(root.resolve()).lower()
    except OSError:
        key = str(root).lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _cache_path(root: Path) -> Path | None:
    """Resolve the cache file for `root` under the cache dir. The name embeds both
    the root hash and the scan signature (view_ext + ignore)."""
    cdir = _cache_dir()
    if cdir is None:
        return None
    return cdir / f"{_root_hash(root)}-{_scan_signature()}.json"


def _load_cache(root: Path) -> dict[str, dict]:
    """Load the per-directory snapshot map for `root` (``rel -> {mtime, files,
    dirs}``). Returns ``{}`` on any error/absence/version mismatch — fail-safe, so
    a corrupt cache simply triggers a full rescan rather than an error."""
    cpath = _cache_path(root)
    if cpath is None or not cpath.is_file():
        return {}
    try:
        raw = json.loads(cpath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != _CACHE_VERSION:
        return {}
    dirs = raw.get("dirs")
    return dirs if isinstance(dirs, dict) else {}


def _save_cache(root: Path, dir_map: dict[str, dict]) -> None:
    """Persist the per-directory snapshot map for `root`. Best-effort: any write
    failure is swallowed (the cache is an optimisation, never required for
    correctness). Writes ONLY inside the cache dir."""
    cpath = _cache_path(root)
    if cpath is None:
        return
    try:
        cpath.parent.mkdir(parents=True, exist_ok=True)
        # Refuse to follow a symlinked cache file (defensive; matches the config
        # write hardening), so the cache cannot be redirected to an outside file.
        if cpath.is_symlink() or (cpath.exists() and not cpath.is_file()):
            return
        payload = {"version": _CACHE_VERSION, "root": str(root), "dirs": dir_map}
        tmp = cpath.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cpath)
    except OSError:
        return


# --------------------------------------------------------------------------- #
# Per-directory scanning (the cacheable unit) and tree assembly (v0.3).
# --------------------------------------------------------------------------- #

def _file_entry(f: str, rel: str, full: Path) -> dict | None:
    """Build a file node for one viewable file (or None if its extension is not in
    the active VIEW_EXT). Title/description for .md is filled later (in parallel)."""
    ext = Path(f).suffix.lower()
    if ext not in VIEW_EXT:
        return None
    try:
        mtime = full.stat().st_mtime
    except OSError:
        mtime = 0.0
    renderable = ext in RENDERABLE_EXT
    if ext == ".pdf":
        kind = "pdf"
    elif ext == ".svg":
        kind = "svg"
    elif ext in (".md", ".markdown"):
        kind = "md"
    else:
        kind = "other"   # listed via config but not rendered inline
    entry = {
        "name": f,
        "path": (f if rel == "" else f"{rel}/{f}"),
        "type": "file",
        "ext": kind,
        "renderable": renderable,
        "mtime": mtime,
    }
    if ext == ".pdf":
        entry["title"], entry["desc"] = f, "(PDF)"
    elif ext == ".svg":
        entry["title"], entry["desc"] = f, "(SVG image)"
    elif kind != "md":
        entry["title"], entry["desc"] = f, f"({ext.lstrip('.').upper() or 'file'})"
    return entry


def _scan_one_dir(root: Path, rel: str) -> tuple[list[dict], list[str], float]:
    """Scan a single directory's immediate contents (NOT recursive). Returns
    ``(file_entries, child_dir_names, dir_mtime)``:

    - ``file_entries`` — viewable file nodes directly in this dir (.md metadata
      filled), sorted by name.
    - ``child_dir_names`` — names of non-pruned sub-directories, sorted.
    - ``dir_mtime`` — the directory's own mtime, used as the cache invalidation
      key (changes when files are added/removed/renamed in it).

    This is the unit the persistent cache stores and re-scans incrementally."""
    dpath = root if rel == "" else root / rel
    try:
        dir_mtime = dpath.stat().st_mtime
    except OSError:
        dir_mtime = 0.0
    files: list[dict] = []
    child_dirs: list[str] = []
    md_jobs: list[tuple[dict, Path]] = []
    try:
        with os.scandir(dpath) as it:
            entries = list(it)
    except OSError:
        return [], [], dir_mtime
    for de in entries:
        name = de.name
        try:
            is_dir = de.is_dir()
        except OSError:
            is_dir = False
        if is_dir:
            if not _skip_dir(name):
                child_dirs.append(name)
            continue
        entry = _file_entry(name, rel, Path(de.path))
        if entry is None:
            continue
        files.append(entry)
        if entry["ext"] == "md":
            md_jobs.append((entry, Path(de.path)))
    if md_jobs:
        from concurrent.futures import ThreadPoolExecutor

        def _fill(job: tuple[dict, Path]) -> None:
            e, full = job
            e["title"], e["desc"] = _extract_meta(full)

        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(_fill, md_jobs))
    files.sort(key=lambda c: c["name"].lower())
    child_dirs.sort(key=str.lower)
    return files, child_dirs, dir_mtime


def _build_tree(root: Path, max_depth: int | None = None,
                base_rel: str = "", use_cache: bool | None = None) -> dict:
    """Walk the root with pruning and return a tree of only the directories that
    contain a viewable file (per the active VIEW_EXT).

    v0.3:
    - ``max_depth`` limits how deep the walk descends from ``base_rel`` (None =
      unlimited, the original full-tree behaviour). When the limit truncates a
      directory that has further content, the node is marked ``lazy: true`` with
      empty ``children`` so the client can fetch it on demand via
      ``GET /api/tree?path=<dir>``.
    - A persistent per-directory cache (keyed by each dir's mtime) lets startup
      re-scan only the directories that changed. Pass ``use_cache=False`` (or run
      with ``--no-cache``) to bypass it; the default follows the module
      ``USE_CACHE`` flag.

    ``base_rel`` lets callers build only a subtree (used by the lazy endpoint).
    The returned node always carries the aggregated ``mtime``; the ROOT-level node
    additionally carries ``gh_repos`` and ``project_icons``."""
    if use_cache is None:
        use_cache = USE_CACHE
    cache = _load_cache(root) if use_cache else {}
    new_cache: dict[str, dict] = {}

    def scan(rel: str) -> tuple[list[dict], list[str]]:
        """Return (file_entries, child_dir_names) for `rel`, reusing the cache when
        the directory's mtime is unchanged."""
        dpath = root if rel == "" else root / rel
        try:
            mtime = dpath.stat().st_mtime
        except OSError:
            mtime = 0.0
        cached = cache.get(rel)
        if cached is not None and cached.get("mtime") == mtime:
            files = [dict(e) for e in cached.get("files", [])]
            child_dirs = list(cached.get("dirs", []))
        else:
            files, child_dirs, mtime = _scan_one_dir(root, rel)
        new_cache[rel] = {"mtime": mtime, "files": files, "dirs": child_dirs}
        return files, child_dirs

    def build(rel: str, depth: int) -> dict | None:
        """Build the node for `rel`, recursing into children unless the depth
        limit is hit. Returns None for a directory with no viewable content at or
        below it (so empty branches are pruned), except the base node is always
        returned."""
        files, child_dirs = scan(rel)
        children: list[dict] = []
        has_deeper = False
        truncate = max_depth is not None and depth >= max_depth
        for d in child_dirs:
            child_rel = d if rel == "" else f"{rel}/{d}"
            if truncate:
                # Don't descend; emit a lazy stub only if it actually has content.
                if _dir_has_content(root, child_rel, cache, new_cache):
                    cm = new_cache.get(child_rel, {}).get("mtime", 0.0)
                    children.append({
                        "name": d, "type": "dir", "children": [],
                        "lazy": True, "mtime": cm,
                    })
                    has_deeper = True
                continue
            child_node = build(child_rel, depth + 1)
            if child_node is not None:
                children.append(child_node)
        children.extend(files)
        if not children and rel != base_rel:
            return None
        children.sort(key=lambda c: (c["type"] == "file", c["name"].lower()))
        mt = 0.0
        for c in children:
            cm = c.get("mtime", 0.0) or 0.0
            if cm > mt:
                mt = cm
        name = root.name if rel == "" else Path(rel).name
        node = {"name": name, "type": "dir", "children": children, "mtime": mt}
        return node

    node = build(base_rel, 0) or {
        "name": root.name if base_rel == "" else Path(base_rel).name,
        "type": "dir", "children": [], "mtime": 0.0,
    }

    if use_cache:
        _save_cache(root, new_cache)

    if base_rel == "":
        node["gh_repos"] = _gh_repos_map(root)   # repo name -> GitHub blob base URL
        # Per-project icon map (config.project_icons over baked-in REPO_ICON) for
        # every top-level dir, so the client shows emoji icons (colour-dot fallback).
        icons: dict[str, str] = {}
        for child in node["children"]:
            if child.get("type") == "dir":
                ic = project_icon(child["name"])
                if ic:
                    icons[child["name"]] = ic
        node["project_icons"] = icons
    return node


def _dir_has_content(root: Path, rel: str, cache: dict, new_cache: dict) -> bool:
    """True if `rel` (or any descendant, outside pruned dirs) holds a viewable
    file. Used to decide whether a truncated (lazy) directory is worth showing.
    Populates ``new_cache`` with every directory it scans so the work is not
    repeated when the dir is later expanded."""
    stack = [rel]
    found = False
    while stack:
        cur = stack.pop()
        dpath = root if cur == "" else root / cur
        try:
            mtime = dpath.stat().st_mtime
        except OSError:
            mtime = 0.0
        cached = cache.get(cur)
        if cached is not None and cached.get("mtime") == mtime:
            files = cached.get("files", [])
            child_dirs = cached.get("dirs", [])
        else:
            files, child_dirs, mtime = _scan_one_dir(root, cur)
        if cur not in new_cache:
            new_cache[cur] = {"mtime": mtime,
                              "files": [dict(e) for e in files],
                              "dirs": list(child_dirs)}
        if files:
            found = True
            # keep scanning so the cache is filled, but we already know the answer
        for d in child_dirs:
            stack.append(d if cur == "" else f"{cur}/{d}")
    return found


_GH_REPOS: dict | None = None


def _gh_repos_map(root: Path | None = None) -> dict:
    """Resolve the GitHub blob base URL for each immediate sub-repo of the root
    once and cache it (git is invoked per repo)."""
    global _GH_REPOS
    if _GH_REPOS is None:
        _GH_REPOS = {}
        base_dir = root if root is not None else ROOT
        try:
            children = list(base_dir.iterdir())
        except OSError:
            children = []
        for p in children:
            if p.is_dir() and not _skip_dir(p.name):
                base = _gh_base(p)
                if base:
                    _GH_REPOS[p.name] = base
    return _GH_REPOS


# Default depth of the initial (lazy) tree the client loads at startup. Only the
# top ~2 levels are walked; deeper dirs arrive as lazy stubs and are fetched on
# expansion, so startup cost is bounded by the breadth of the shallow levels
# rather than the total file count.
SHALLOW_DEPTH = 2

# Two independent in-memory caches of rendered JSON:
#   _tree_cache       → the shallow startup tree (GET /api/tree)
#   _full_tree_cache  → the complete tree (GET /api/tree?full=1, used for search)
# Both expire after _TREE_TTL so reloads pick up new files; ?fresh=1 forces a
# rebuild. The persistent per-dir cache underneath makes those rebuilds cheap.
_tree_cache = {"json": None, "ts": 0.0}
_full_tree_cache = {"json": None, "ts": 0.0}
_TREE_TTL = 5.0  # rescan after this many seconds → new/updated files show up on reload


def _reset_tree_cache() -> None:
    """Invalidate both in-memory rendered-tree caches (e.g. after a config POST
    that may change view_ext / ignore)."""
    _tree_cache["json"] = None
    _full_tree_cache["json"] = None


def _tree_json(force: bool = False, full: bool = False) -> str:
    """Rendered JSON for GET /api/tree. ``full`` returns the complete tree (for
    client-side search); otherwise the shallow startup tree (lazy beyond
    SHALLOW_DEPTH). ``force`` (=?fresh=1) bypasses the TTL."""
    import time
    now = time.monotonic()
    cache = _full_tree_cache if full else _tree_cache
    if force or cache["json"] is None or (now - cache["ts"]) > _TREE_TTL:
        depth = None if full else SHALLOW_DEPTH
        cache["json"] = json.dumps(_build_tree(ROOT, max_depth=depth), ensure_ascii=False)
        cache["ts"] = now
    return cache["json"]


def _subtree_json(rel: str) -> str | None:
    """Rendered JSON for the lazy GET /api/tree?path=<dir>: the immediate children
    of one root-confined, non-pruned directory (its own grandchildren arrive as
    lazy stubs). Returns None when the path is outside the root, missing, or inside
    a pruned/hidden directory (caller maps that to 404), so this endpoint honours
    the exact same boundary as _safe_resolve."""
    base = _safe_resolve_dir(rel)
    if base is None:
        return None
    norm = str(base.relative_to(ROOT.resolve())).replace("\\", "/")
    if norm == ".":
        norm = ""
    node = _build_tree(ROOT, max_depth=1, base_rel=norm)
    return json.dumps(node, ensure_ascii=False)


def _safe_resolve_dir(rel: str) -> Path | None:
    """Resolve a request path to an existing DIRECTORY under ROOT that is not a
    pruned/hidden dir. Returns None on traversal, a missing/non-directory target,
    or any pruned component (including the directory's own name). Used by the lazy
    subtree endpoint. The empty string resolves to ROOT itself."""
    try:
        target = (ROOT / rel).resolve()
        relpath = target.relative_to(ROOT.resolve())
    except (ValueError, OSError):
        return None
    if not target.is_dir():
        return None
    # Check EVERY component (including the leaf dir name), unlike _in_pruned_dir
    # which treats the leaf as a filename.
    for part in relpath.parts:
        if _skip_dir(part):
            return None
    return target


def _in_pruned_dir(target: Path) -> bool:
    """True if any directory component of `target` (relative to ROOT) is one the
    tree scan prunes (`.git`, `node_modules`, dotdirs, virtualenvs, …).

    The tree only ever lists files outside these dirs, so the read / open
    endpoints must enforce the same boundary. Otherwise widening `view_ext` via
    config would let GET reach secrets the tree hides (e.g. `.git/credentials`,
    `node_modules/**/.npmrc`), turning tree-pruning — which is NOT a security
    boundary on its own — into a false sense of one. `target` is assumed already
    resolved and confined to ROOT by the caller."""
    try:
        rel = target.relative_to(ROOT.resolve())
    except ValueError:
        return True   # outside root → treat as pruned (defensive; caller also checks)
    # Every component except the final filename is a directory on the path.
    for part in rel.parts[:-1]:
        if _skip_dir(part):
            return True
    return False


def _safe_resolve(rel: str) -> Path | None:
    """Resolve a request path to an existing file under ROOT whose extension is in
    the active VIEW_EXT. Returns None on traversal, missing file, bad extension,
    or a path inside a pruned/hidden directory. Used by the read endpoints
    (/api/file, /api/raw)."""
    try:
        target = (ROOT / rel).resolve()
        target.relative_to(ROOT.resolve())
    except (ValueError, OSError):
        return None
    if not target.is_file() or target.suffix.lower() not in VIEW_EXT:
        return None
    if _in_pruned_dir(target):
        return None
    return target


def _safe_open_resolve(rel: str) -> Path | None:
    """Resolve a request path for OS-association launch: confined to ROOT, must be
    an existing regular file, must NOT live in a pruned/hidden dir, and must NOT
    have an executable extension. It is intentionally NOT limited to VIEW_EXT (the
    point is to open non-viewable types), but the executable deny-list keeps the
    OS association from being abused to *run* code: combined with ENABLE_OPEN being
    opt-in, the executable deny-list, the pruned-dir check, and the launcher
    passing a single path argument (never a shell string), this avoids both a
    shell-injection surface and a one-click ShellExecute code-execution surface."""
    try:
        target = (ROOT / rel).resolve()
        target.relative_to(ROOT.resolve())
    except (ValueError, OSError):
        return None
    if not target.is_file():
        return None
    if _in_pruned_dir(target):
        return None
    if target.suffix.lower() in EXECUTABLE_EXT:
        return None
    return target


def _os_open(target: Path) -> None:
    """Launch a file with its OS association. Caller must have already validated
    the path with _safe_open_resolve and checked ENABLE_OPEN."""
    if sys.platform.startswith("win"):
        os.startfile(str(target))  # type: ignore[attr-defined]  # noqa: S606 — path is root-confined
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="__CSRF_TOKEN__">
<title>Markdown Tree Viewer — __ROOT__</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  window.__mermaid = mermaid;
  mermaid.initialize({ startOnLoad: false, theme: 'neutral' });
</script>
<style>
  :root { --bg:#fff; --fg:#24292f; --side:#f6f8fa; --border:#d0d7de; --accent:#0969da; --muted:#8b949e; }
  body.dark { --bg:#0d1117; --fg:#c9d1d9; --side:#161b22; --border:#30363d; --accent:#58a6ff; --muted:#8b949e; }
  body.dark { background:var(--bg); }
  body.dark #content pre, body.dark #content th { background:#161b22; }
  body.dark #content code { background:rgba(110,118,129,.4); }
  * { box-sizing: border-box; }
  /* Kill scrolling on the outer html/body and confine vertical scroll to the
     inner panes (#tree / #content). Avoids a second scrollbar when 100vh is
     slightly larger than the real viewport. */
  html, body { height:100%; }
  body { margin:0; overflow:hidden; font-family:-apple-system,"Segoe UI",Meiryo,sans-serif; color:var(--fg); }
  #wrap { display:flex; height:100%; }
  #side { width:400px; min-width:220px; max-width:70vw; background:var(--side);
          border-right:1px solid var(--border); display:flex; flex-direction:column; }
  #side h1 { font-size:13px; padding:10px 12px; margin:0; border-bottom:1px solid var(--border); color:#57606a; }
  #filter { margin:8px; padding:6px 8px; border:1px solid var(--border); border-radius:6px; font-size:13px; }
  #tree { overflow:auto; flex:1 1 0; min-height:0; padding:4px 6px 20px; font-size:13px; }
  #tree ul { list-style:none; margin:0; padding-left:14px; }
  #tree > ul { padding-left:2px; }
  #tree ul ul { border-left:1px solid #e4e8ec; }   /* nesting guide line */
  .dir > .label { cursor:pointer; font-weight:600; color:#57606a; user-select:none; }
  .dir > .label::before { content:"▸ 📁 "; }
  .dir.open > .label::before { content:"▾ 📂 "; }
  .dir.collapsed > ul { display:none; }
  /* Highlight folders that contain recently modified files (server aggregates the
     max mtime of the contents). Keep the folder emoji and add a freshness badge
     (yellow = within 3 days / orange = within 6 hours). */
  .dir.fresh  > .label { color:#9a6700; }
  .dir.fresh  > .label::before { content:"▸ 📁🟡 "; }
  .dir.fresh.open  > .label::before { content:"▾ 📂🟡 "; }
  .dir.fresher > .label { color:#bc4c00; font-weight:700; }
  .dir.fresher > .label::before { content:"▸ 📁🟠 "; }
  .dir.fresher.open > .label::before { content:"▾ 📂🟠 "; }
  .file { cursor:pointer; display:block; padding:3px 5px; border-radius:5px; margin:1px 0; }
  .file:hover { background:#eaeef2; }
  .file.active { background:#dbeafe; }
  .fname { color:var(--accent); display:block; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .repoicon { font-style:normal; }
  .file.pdf .fname { font-style:italic; }           /* PDFs in italic */
  .fdesc { display:block; color:var(--muted); font-size:11px; line-height:1.35; margin-top:1px;
           white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }  /* 1-line ellipsis = no reflow */
  #results { padding:4px 6px; }
  .hidden { display:none; }
  .when { color:var(--muted); font-size:10.5px; }
  .fpath { display:block; color:#8b949e; font-size:10px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .repodot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; vertical-align:middle; }
  #content a.coderef { background:rgba(9,105,218,.10); color:var(--accent); padding:.1em .35em;
                       border-radius:5px; font-family:ui-monospace,Consolas,monospace; font-size:85%;
                       text-decoration:none; border-bottom:1px dotted var(--accent); cursor:pointer; }
  #content a.coderef.gh { color:#6e40c9; border-bottom-color:#6e40c9; }
  #content a.coderef.gh::after { content:" ↗"; font-size:80%; }
  #recentWrap { margin:0 0 4px !important; border-bottom:1px solid var(--border); padding-bottom:4px; }
  .dir.special > .label { color:#7a5cff; }                 /* recent-section heading */
  .dir.special > .label::before { content:"▸ "; }          /* no folder emoji */
  .dir.special.open > .label::before { content:"▾ "; }
  #content { flex:1; overflow:auto; padding:24px 40px; }
  #content .doc { max-width:940px; }
  #content img { max-width:100%; }
  #content pre { background:var(--side); padding:12px; border-radius:6px; overflow:auto; }
  #content code { background:rgba(175,184,193,.2); padding:.15em .35em; border-radius:5px; font-size:85%; }
  #content pre code { background:none; padding:0; }
  #content table { border-collapse:collapse; margin:12px 0; }
  #content th,#content td { border:1px solid var(--border); padding:6px 12px; }
  #content th { background:var(--side); }
  #content blockquote { border-left:4px solid var(--border); margin:0; padding:0 1em; color:#57606a; }
  #content h1,#content h2 { border-bottom:1px solid var(--border); padding-bottom:.3em; }
  #path { font-size:12px; color:#57606a; margin-bottom:16px; word-break:break-all; }
  #drag { width:5px; cursor:col-resize; background:transparent; }
  .empty { color:var(--muted); padding:40px; }
  #count { font-size:11px; color:var(--muted); padding:0 12px 6px; }
  #settings { border-bottom:1px solid var(--border); padding:8px 12px 10px; font-size:12px;
              max-height:55vh; overflow:auto; }
  #settings.hidden { display:none; }
  .setgrp { margin-bottom:10px; padding-bottom:8px; border-bottom:1px dashed var(--border); }
  .settitle { font-weight:600; color:#57606a; margin-bottom:4px; }
  .setrow { display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin:4px 0; }
  .setnote { color:var(--muted); font-size:10.5px; margin-top:3px; }
  #settings input[type=text], #settings input:not([type]) , #settings select {
    border:1px solid var(--border); border-radius:5px; padding:3px 5px; font-size:12px;
    background:var(--bg); color:var(--fg); }
  #settings button { border:1px solid var(--border); border-radius:5px; padding:3px 8px;
    background:var(--bg); color:var(--accent); cursor:pointer; font-size:12px; }
  #settings button:hover { background:var(--side); }
  .extitem, .iconitem { display:flex; align-items:center; gap:6px; margin:2px 0; }
  .extitem .x, .iconitem .x { cursor:pointer; color:#b35900; font-weight:700; }
</style></head>
<body><div id="wrap">
  <div id="side">
    <h1>📁 __ROOT__
      <span id="settingsBtn" title="Settings" style="float:right;cursor:pointer;margin-left:8px">⚙️</span>
      <span id="refresh" title="Rescan (pick up new files)" style="float:right;cursor:pointer">🔄</span>
    </h1>
    <input id="filter" placeholder="Filter by name or description...">
    <div id="count"></div>
    <div id="settings" class="hidden">
      <div class="setgrp">
        <div class="settitle">Viewable extensions</div>
        <div id="extList"></div>
        <div class="setrow">
          <input id="extAdd" placeholder=".rst" style="width:80px">
          <button id="extAddBtn">Add</button>
        </div>
        <div class="setnote">md / markdown / pdf / svg render inline; other listed types open with the OS app.</div>
      </div>
      <div class="setgrp">
        <div class="settitle">Project icons</div>
        <div id="iconList"></div>
        <div class="setrow">
          <input id="iconName" placeholder="project dir" style="width:110px">
          <input id="iconEmoji" placeholder="🧠" style="width:46px">
          <button id="iconAddBtn">Add</button>
        </div>
      </div>
      <div class="setgrp">
        <div class="setrow"><label><input type="checkbox" id="enableOpen"> Allow OS-association open</label></div>
        <div class="setnote" id="openNote">When off, the server returns 403 for open requests. Server may still force this off.</div>
      </div>
      <div class="setgrp">
        <div class="setrow">
          <label>Theme
            <select id="themeSel"><option value="light">light</option><option value="dark">dark</option></select>
          </label>
        </div>
      </div>
      <div class="setrow">
        <button id="saveCfg">Save</button>
        <span id="cfgMsg" class="when"></span>
      </div>
      <div class="setnote" id="cfgPath"></div>
    </div>
    <div id="tree">Loading...</div>
  </div>
  <div id="drag"></div>
  <div id="content"><div class="empty">Select a file from the tree on the left.</div></div>
</div>
<script>
// The CSRF token is embedded in the page (same-origin only). Every POST sends it
// back in the X-CSRF-Token header; a custom header forces a CORS pre-flight on
// cross-origin requests, so a malicious page cannot forge a state-changing POST.
const CSRF_TOKEN = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
function postJSON(url, body) {
  const headers = { 'X-CSRF-Token': CSRF_TOKEN };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  return fetch(url, { method: 'POST', headers,
    body: body === undefined ? undefined : JSON.stringify(body) });
}
const treeEl = document.getElementById('tree');
const contentEl = document.getElementById('content');
const countEl = document.getElementById('count');
const filterEl = document.getElementById('filter');
let activeEl = null, treeData = null, flatFiles = [], recentWrap = null, projWrap = null, resultsEl = null;
const RECENT_KEY = 'mdv_recent_v1', RECENT_MAX = 40;
const OPEN_KEY = 'mdv_open_dirs_v1', CR_KEY = 'mdv_collapsed_recent_v1';
function loadSet(k){ try { return new Set(JSON.parse(localStorage.getItem(k))||[]); } catch(e){ return new Set(); } }
function saveSet(k, s){ localStorage.setItem(k, JSON.stringify([...s])); }
let openDirs = loadSet(OPEN_KEY), collapsedRecent = loadSet(CR_KEY);  // dirs collapsed by default / recent open by default

function timeago(ts) {
  if (!ts) return '';
  const s = Date.now()/1000 - ts;
  if (s < 90) return 'just now';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  if (s < 86400*30) return Math.floor(s/86400) + 'd ago';
  return Math.floor(s/86400/30) + 'mo ago';
}

function repoHue(s) { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0; return h % 360; }

function makeFileSpan(node, when, showPath) {
  const span = document.createElement('span');
  span.className = 'file' + (node.ext === 'pdf' ? ' pdf' : '');
  span.dataset.path = node.path; span.dataset.ext = node.ext;
  span.dataset.hay = (node.name + ' ' + (node.title||'') + ' ' + (node.desc||'') + ' ' + node.path).toLowerCase();
  span.title = node.path + (node.title ? ('\n' + node.title) : '') + (node.desc ? ('\n' + node.desc) : '');
  const nm = document.createElement('span'); nm.className = 'fname';
  const repo = node.path.split('/')[0];                       // top-level dir = "repo"
  // Icon resolution: config/baked project_icons emoji first, else a colour dot.
  const icons = (treeData && treeData.project_icons) || {};
  if (icons[repo]) {
    const ic = document.createElement('span'); ic.className = 'repoicon';
    ic.textContent = icons[repo] + ' '; ic.title = repo; nm.appendChild(ic);
  } else {
    const dot = document.createElement('span'); dot.className = 'repodot';
    dot.style.background = 'hsl(' + repoHue(repo) + ' 60% 52%)'; dot.title = repo; nm.appendChild(dot);
  }
  nm.appendChild(document.createTextNode(node.name));
  span.appendChild(nm);
  if (when) { const w = document.createElement('span'); w.className = 'when'; w.textContent = ' · ' + when; nm.appendChild(w); }
  // In flat views (recent/search) also show the containing path so same-named files are distinguishable.
  if (showPath && node.path.indexOf('/') >= 0) {
    const fp = document.createElement('span'); fp.className = 'fpath';
    fp.textContent = node.path.slice(0, node.path.lastIndexOf('/'));
    span.appendChild(fp);
  }
  const label = (node.title && node.title.toLowerCase() !== node.name.toLowerCase().replace(/\.(md|markdown)$/,''))
                ? node.title : '';
  const d = [label, node.desc].filter(Boolean).join(' — ');
  if (d) { const de = document.createElement('span'); de.className = 'fdesc'; de.textContent = d; span.appendChild(de); }
  span.onclick = () => openFile(node, span);
  return span;
}

function makeChild(node, parentPath) {
  if (node.type === 'file') {
    const li = document.createElement('li');
    li.appendChild(makeFileSpan(node)); return li;
  }
  return makeDir(node, parentPath ? parentPath + '/' + node.name : node.name);
}

// Lazy render: a directory's children are created the first time it is opened.
// Keeps the DOM small so collapse/expand reflow stays cheap.
function makeDir(node, dirPath) {
  const li = document.createElement('li');
  li.className = 'dir'; li.dataset.path = dirPath;
  const label = document.createElement('span');
  label.className = 'label'; label.textContent = node.name;
  // Highlight by the max mtime of the contents (24h = fresher / 7d = fresh).
  if (node.mtime) {
    const age = Date.now()/1000 - node.mtime;
    if (age < 3600*6) li.classList.add('fresher');      // within 6h = actively editing now
    else if (age < 86400*3) li.classList.add('fresh');  // within 3d = recent work
    if (age < 86400*3) {
      const w = document.createElement('span'); w.className = 'when';
      w.textContent = ' · ' + timeago(node.mtime); label.appendChild(w);
    }
  }
  li.appendChild(label);
  const ul = document.createElement('ul'); li.appendChild(ul);
  let built = false;
  function build() {
    if (built) return;
    const frag = document.createDocumentFragment();
    for (const c of node.children) frag.appendChild(makeChild(c, dirPath));
    ul.appendChild(frag); built = true;
  }
  function setOpen(open) {
    if (open) { build(); li.classList.add('open'); li.classList.remove('collapsed'); openDirs.add(dirPath); }
    else { li.classList.remove('open'); li.classList.add('collapsed'); openDirs.delete(dirPath); }
    saveSet(OPEN_KEY, openDirs);
  }
  label.onclick = () => setOpen(!li.classList.contains('open'));
  if (openDirs.has(dirPath)) { build(); li.classList.add('open'); } else { li.classList.add('collapsed'); }
  return li;
}

function getRecent() { try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; } catch(e){ return []; } }
function recordRecent(node) {
  let r = getRecent().filter(x => x.path !== node.path);
  r.unshift({ path:node.path, name:node.name, ext:node.ext, title:node.title||'', desc:node.desc||'', ts: Date.now()/1000 });
  localStorage.setItem(RECENT_KEY, JSON.stringify(r.slice(0, RECENT_MAX)));
}
function makeSpecialSection(key, title, items) {
  // Recent section = a collapsible pseudo-folder at the top of the tree. Open by default.
  const li = document.createElement('li');
  li.className = 'dir special';
  li.dataset.path = key;
  const label = document.createElement('span');
  label.className = 'label'; label.textContent = title;
  li.appendChild(label);
  const ul = document.createElement('ul');
  for (const it of items) {
    const fl = document.createElement('li'); fl.appendChild(makeFileSpan(it.node, it.when, true)); ul.appendChild(fl);
  }
  li.appendChild(ul);
  li.classList.add(collapsedRecent.has(key) ? 'collapsed' : 'open');
  label.onclick = () => {
    const open = li.classList.contains('collapsed');
    li.classList.toggle('open', open); li.classList.toggle('collapsed', !open);
    if (open) collapsedRecent.delete(key); else collapsedRecent.add(key);
    saveSet(CR_KEY, collapsedRecent);
  };
  return li;
}

function renderRecent() {
  if (!recentWrap) return;
  recentWrap.innerHTML = '';
  const rec = getRecent();
  if (rec.length) {
    recentWrap.appendChild(makeSpecialSection('::recent_opened', '🕘 Recently opened',
      rec.slice(0, 30).map(n => ({ node: n, when: timeago(n.ts) }))));
  }
  const mod = flatFiles.filter(f => f.mtime).slice().sort((a, b) => b.mtime - a.mtime).slice(0, 8);
  if (mod.length) {
    recentWrap.appendChild(makeSpecialSection('::recent_modified', '✨ Recently modified',
      mod.map(n => ({ node: n, when: timeago(n.mtime) }))));
  }
}

function collectFlat(node) {
  if (node.type === 'file') { flatFiles.push(node); return; }
  for (const c of node.children) collectFlat(c);
}

async function loadTree(fresh) {
  const r = await fetch('/api/tree' + (fresh ? '?fresh=1' : '')); treeData = await r.json();
  flatFiles = []; collectFlat(treeData);
  openDirs = loadSet(OPEN_KEY); collapsedRecent = loadSet(CR_KEY);
  treeEl.innerHTML = '';
  if (!flatFiles.length) { treeEl.innerHTML = '<div class="empty">No files to show.</div>'; return; }
  recentWrap = document.createElement('ul'); recentWrap.id = 'recentWrap'; treeEl.appendChild(recentWrap);
  projWrap = document.createElement('ul'); treeEl.appendChild(projWrap);
  resultsEl = document.createElement('div'); resultsEl.id = 'results'; resultsEl.style.display = 'none';
  treeEl.appendChild(resultsEl);
  const frag = document.createDocumentFragment();
  for (const c of treeData.children) frag.appendChild(makeChild(c, ''));  // collapsed by default = only top level rendered
  projWrap.appendChild(frag);
  renderRecent();
  countEl.textContent = flatFiles.length + ' files';
  lastLoad = performance.now();
}

// When the tab regains focus after 10s+ away, auto-refresh (pick up new files without disturbing reading).
let lastLoad = 0;
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && performance.now() - lastLoad > 10000) loadTree(true);
});

let currentDocPath = '';
function resolveRel(base, href) {  // base = current doc path (file), href = relative link → normalized path
  const parts = base.split('/'); parts.pop();
  for (const seg of href.split('/')) {
    if (seg === '' || seg === '.') continue;
    if (seg === '..') parts.pop(); else parts.push(seg);
  }
  return parts.join('/');
}

function pathFromHash() { return decodeURIComponent(location.hash.replace(/^#/, '')); }
function openByPath(path) {
  const node = flatFiles.find(f => f.path === path);
  if (node) { openFile(node, null); }
}

// path (e.g. sub/x.md) → GitHub blob URL. null if the repo has no remote.
function ghUrlForPath(path) {
  const map = (treeData && treeData.gh_repos) || {};
  const base = map[path.split('/')[0]];
  return base ? base + '/' + path.split('/').slice(1).map(encodeURIComponent).join('/') : null;
}
function ghUrlForRef(text) { return ghUrlForPath(resolveRel(currentDocPath, text)); }

// Resolve whether an md/pdf referenced from the current doc exists locally.
function resolveDocRef(text) {
  text = text.trim();
  if (!/\.(md|markdown|pdf)$/i.test(text)) return null;
  let node = flatFiles.find(f => f.path === resolveRel(currentDocPath, text));
  if (node) return node;
  node = flatFiles.find(f => f.path === text);
  if (node) return node;
  const base = text.split('/').pop().toLowerCase();
  const hits = flatFiles.filter(f => f.name.toLowerCase() === base);
  return hits.length === 1 ? hits[0] : null;
}

// Make file paths inside inline `code` clickable: md/pdf that exist locally open
// in the viewer; anything else goes to GitHub blob (if the repo has a remote).
function linkifyCodeRefs(container) {
  container.querySelectorAll('code').forEach(c => {
    if (c.closest('pre')) return;                              // inline code only
    const txt = c.textContent.trim();
    if (!/^[\w./+\-]+\.[A-Za-z0-9]{1,8}$/.test(txt)) return;   // file-path-like only
    const local = resolveDocRef(txt);
    if (local) {
      const a = document.createElement('a');
      a.className = 'coderef'; a.href = '#' + encodeURI(local.path); a.textContent = c.textContent;
      a.title = local.path; a.onclick = (e) => { e.preventDefault(); openFile(local, null); };
      c.replaceWith(a); return;
    }
    const gh = ghUrlForRef(txt);
    if (gh) {
      const a = document.createElement('a');
      a.className = 'coderef gh'; a.href = gh; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = c.textContent; a.title = 'GitHub: ' + gh;
      c.replaceWith(a);
    }
  });
}

function pathHeader(node) {
  const gh = ghUrlForPath(node.path);
  return '<div id="path">' + node.path
    + (gh ? ' · <a href="' + gh + '" target="_blank" rel="noopener">Open on GitHub ↗</a>' : '') + '</div>';
}

async function openFile(node, el) {
  recordRecent(node); renderRecent();
  currentDocPath = node.path;
  if (location.hash.slice(1) !== encodeURI(node.path)) history.replaceState(null, '', '#' + encodeURI(node.path));
  if (activeEl) activeEl.classList.remove('active');
  if (el) { el.classList.add('active'); activeEl = el; }
  // Non-renderable types (listed via config but not viewable inline): offer to
  // launch with the OS association via POST /api/open (server-gated: 403 when off).
  if (node.renderable === false) {
    contentEl.innerHTML = pathHeader(node)
      + '<div class="doc"><p>This file type is not rendered inline.</p>'
      + '<p><button id="osopen">Open with the default app ↗</button> '
      + '<span id="openmsg" class="when"></span></p>'
      + '<p class="when">Opens the file on the machine running the server, using its '
      + 'OS file association. Disabled unless the server was started with '
      + '<code>--enable-open</code>.</p></div>';
    const btn = document.getElementById('osopen'), msg = document.getElementById('openmsg');
    btn.onclick = async () => {
      msg.textContent = 'opening…';
      try {
        const r = await postJSON('/api/open?path=' + encodeURIComponent(node.path));
        const j = await r.json().catch(() => ({}));
        msg.textContent = r.ok ? 'launched.' : (j.error || ('failed (' + r.status + ')'));
      } catch (e) { msg.textContent = 'request failed'; }
    };
    contentEl.scrollTop = 0;
    return;
  }
  if (node.ext === 'svg') {
    contentEl.innerHTML = pathHeader(node) + '<div class="doc"><img src="/api/raw?path=' + encodeURIComponent(node.path) + '" alt="' + node.name + '" style="max-width:100%"></div>';
    return;
  }
  if (node.ext === 'pdf') {
    contentEl.innerHTML = pathHeader(node)
      + '<iframe src="/api/raw?path=' + encodeURIComponent(node.path)
      + '" style="width:100%;height:92vh;border:1px solid #d0d7de;border-radius:6px"></iframe>';
    return;
  }
  const r = await fetch('/api/file?path=' + encodeURIComponent(node.path));
  if (!r.ok) { contentEl.innerHTML = '<div class="empty">Could not load: ' + node.path + '</div>'; return; }
  const md = await r.text();
  marked.setOptions({ gfm: true, breaks: false });
  contentEl.innerHTML = pathHeader(node) + '<div class="doc">' + marked.parse(md) + '</div>';
  // Resolve relative <img src> against the doc's directory and rewrite to /api/raw.
  (function(){
    var docDir = node.path.indexOf('/')>=0 ? node.path.slice(0, node.path.lastIndexOf('/')) : '';
    contentEl.querySelectorAll('.doc img').forEach(function(img){
      var src = img.getAttribute('src') || '';
      if (/^(https?:|data:|\/api\/)/i.test(src)) return;
      var parts = docDir ? docDir.split('/') : [];
      src.split('/').forEach(function(seg){ if (seg==='..') parts.pop(); else if (seg!=='.' && seg!=='') parts.push(seg); });
      img.setAttribute('src', '/api/raw?path=' + encodeURIComponent(parts.join('/')));
    });
  })();
  const blocks = contentEl.querySelectorAll('code.language-mermaid'); let i = 0;
  for (const code of blocks) {
    const div = document.createElement('div'); div.className = 'mermaid';
    div.id = 'mmd' + (i++); div.textContent = code.textContent;
    code.parentElement.replaceWith(div);
  }
  if (window.__mermaid && blocks.length) { try { await window.__mermaid.run({ querySelector: '.mermaid' }); } catch(e){} }
  linkifyCodeRefs(contentEl);
  contentEl.scrollTop = 0;
}

// Filtering works on the flatFiles array (not DOM walk) and renders only the results (debounced).
let fTimer;
filterEl.addEventListener('input', () => { clearTimeout(fTimer); fTimer = setTimeout(applyFilter, 130); });
function applyFilter() {
  const q = filterEl.value.toLowerCase().trim();
  if (!q) {
    recentWrap.style.display = ''; projWrap.style.display = ''; resultsEl.style.display = 'none';
    countEl.textContent = flatFiles.length + ' files';
    return;
  }
  const hit = flatFiles.filter(f =>
    (f.name + ' ' + (f.title || '') + ' ' + (f.desc || '') + ' ' + f.path).toLowerCase().includes(q));
  const LIM = 400;
  resultsEl.innerHTML = '';
  const ul = document.createElement('ul');
  const frag = document.createDocumentFragment();
  hit.slice(0, LIM).forEach(n => { const li = document.createElement('li'); li.appendChild(makeFileSpan(n, null, true)); frag.appendChild(li); });
  ul.appendChild(frag); resultsEl.appendChild(ul);
  if (hit.length > LIM) {
    const m = document.createElement('div'); m.className = 'empty'; m.style.padding = '10px';
    m.textContent = '... ' + (hit.length - LIM) + ' more. Narrow the filter.'; resultsEl.appendChild(m);
  }
  recentWrap.style.display = 'none'; projWrap.style.display = 'none'; resultsEl.style.display = '';
  countEl.textContent = hit.length + ' / ' + flatFiles.length + ' files';
}

(function(){
  const drag = document.getElementById('drag'), side = document.getElementById('side');
  let down=false;
  drag.addEventListener('mousedown', ()=>{down=true; document.body.style.userSelect='none';});
  window.addEventListener('mousemove', e=>{ if(down){ side.style.width=Math.max(180,e.clientX)+'px'; }});
  window.addEventListener('mouseup', ()=>{down=false; document.body.style.userSelect='';});
})();

// Relative links in the body open the target doc inside the viewer (avoid 404). External URLs/anchors keep default behavior.
contentEl.addEventListener('click', (e) => {
  const a = e.target.closest('a'); if (!a) return;
  const href = a.getAttribute('href'); if (!href) return;
  if (/^([a-z]+:|#|\/\/)/i.test(href)) return;
  e.preventDefault();
  const target = resolveRel(currentDocPath, decodeURIComponent(href.split('#')[0]));
  const node = flatFiles.find(f => f.path === target);
  if (node) { openFile(node, null); return; }
  const gh = ghUrlForPath(target);
  if (gh) { window.open(gh, '_blank', 'noopener'); return; }
  const note = document.createElement('div'); note.className = 'empty';
  note.style.cssText = 'padding:10px;color:#b35900';
  note.textContent = 'Cannot open link (not a local target and no GitHub remote): ' + target;
  contentEl.prepend(note); setTimeout(() => note.remove(), 4000);
});

document.getElementById('refresh').onclick = () => { treeEl.innerHTML='Rescanning...'; loadTree(true); };

// ---- Settings panel (config GET/POST + theme) ---------------------------------
const THEME_KEY = 'mdv_theme_v1';
let serverCfg = null;          // last config_payload() from the server
let draftExt = [], draftIcons = {};   // editable working copy

function applyTheme(theme) {
  document.body.classList.toggle('dark', theme === 'dark');
  if (window.__mermaid) { try { window.__mermaid.initialize({ startOnLoad:false, theme: theme==='dark'?'dark':'neutral' }); } catch(e){} }
}
// Apply a locally remembered theme immediately (before the config fetch) so there is no flash.
try { applyTheme(localStorage.getItem(THEME_KEY) || 'light'); } catch(e){}

function renderExtList() {
  const box = document.getElementById('extList'); box.innerHTML = '';
  const renderable = (serverCfg && serverCfg.renderable_ext) || [];
  draftExt.forEach((e, idx) => {
    const row = document.createElement('div'); row.className = 'extitem';
    const lbl = document.createElement('span'); lbl.textContent = e + (renderable.includes(e) ? '' : ' (OS-open)');
    const x = document.createElement('span'); x.className = 'x'; x.textContent = '×'; x.title = 'remove';
    x.onclick = () => { draftExt.splice(idx, 1); renderExtList(); };
    row.appendChild(lbl); row.appendChild(x); box.appendChild(row);
  });
}
function renderIconList() {
  const box = document.getElementById('iconList'); box.innerHTML = '';
  Object.keys(draftIcons).forEach(name => {
    const row = document.createElement('div'); row.className = 'iconitem';
    const em = document.createElement('span'); em.textContent = draftIcons[name];
    const nm = document.createElement('span'); nm.textContent = name; nm.style.flex = '1';
    const x = document.createElement('span'); x.className = 'x'; x.textContent = '×'; x.title = 'remove';
    x.onclick = () => { delete draftIcons[name]; renderIconList(); };
    row.appendChild(em); row.appendChild(nm); row.appendChild(x); box.appendChild(row);
  });
}
function fillSettings(cfg) {
  serverCfg = cfg;
  draftExt = (cfg.view_ext || []).slice();
  draftIcons = Object.assign({}, cfg.project_icons || {});
  renderExtList(); renderIconList();
  document.getElementById('enableOpen').checked = !!cfg.enable_open;
  document.getElementById('themeSel').value = cfg.theme || 'light';
  document.getElementById('cfgPath').textContent = cfg.config_path ? ('config: ' + cfg.config_path) : '';
}
async function loadConfig() {
  try {
    const r = await fetch('/api/config'); const cfg = await r.json();
    fillSettings(cfg);
    // Server config wins for theme; mirror to localStorage for flash-free reloads.
    const theme = cfg.theme || (localStorage.getItem(THEME_KEY) || 'light');
    try { localStorage.setItem(THEME_KEY, theme); } catch(e){}
    applyTheme(theme);
  } catch(e){}
}
document.getElementById('settingsBtn').onclick = () => {
  document.getElementById('settings').classList.toggle('hidden');
};
document.getElementById('extAddBtn').onclick = () => {
  let v = (document.getElementById('extAdd').value || '').trim().toLowerCase();
  if (!v) return;
  if (v[0] !== '.') v = '.' + v;
  if (!draftExt.includes(v)) draftExt.push(v);
  document.getElementById('extAdd').value = ''; renderExtList();
};
document.getElementById('iconAddBtn').onclick = () => {
  const name = (document.getElementById('iconName').value || '').trim();
  const em = (document.getElementById('iconEmoji').value || '').trim();
  if (!name || !em) return;
  draftIcons[name] = em;
  document.getElementById('iconName').value = ''; document.getElementById('iconEmoji').value = '';
  renderIconList();
};
document.getElementById('themeSel').onchange = (e) => {
  const t = e.target.value; try { localStorage.setItem(THEME_KEY, t); } catch(_){}
  applyTheme(t);
};
document.getElementById('saveCfg').onclick = async () => {
  const msg = document.getElementById('cfgMsg'); msg.textContent = 'saving…';
  const body = {
    view_ext: draftExt,
    project_icons: draftIcons,
    enable_open: document.getElementById('enableOpen').checked,
    theme: document.getElementById('themeSel').value,
  };
  try {
    const r = await postJSON('/api/config', body);
    const j = await r.json().catch(() => ({}));
    if (r.ok && j.ok) {
      fillSettings(j.config);
      const t = j.config.theme || 'light'; try { localStorage.setItem(THEME_KEY, t); } catch(_){}
      applyTheme(t);
      msg.textContent = 'saved.';
      loadTree(true);   // view_ext / icons may have changed → rescan + redraw
    } else {
      msg.textContent = (j.error || ('failed (' + r.status + ')'));
    }
  } catch(e) { msg.textContent = 'request failed'; }
};

loadConfig();
loadTree().then(() => { if (location.hash.length > 1) openByPath(pathFromHash()); });
window.addEventListener('hashchange', () => {
  const p = pathFromHash();
  if (p && p !== currentDocPath) openByPath(p);
});
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json_body(self) -> dict | None:
        """Read and JSON-parse the request body. Returns None on a missing/oversized
        body or invalid JSON (caller maps that to a 400). 256 KiB cap is generous
        for a config file and bounds memory."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return None
        if length <= 0 or length > 256 * 1024:
            return None
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # --- request-origin / CSRF guards for state-changing requests --------------
    def _bound_port(self) -> int | None:
        """The port this server is actually bound to (authoritative, taken from the
        live socket). None if unavailable."""
        try:
            return int(self.server.server_address[1])
        except (AttributeError, IndexError, TypeError, ValueError):
            return None

    def _host_ok(self) -> bool:
        """Validate the Host header so a DNS-rebinding name that resolves to
        127.0.0.1 cannot reach the local server. Only loopback literals (with the
        bound port, or no port) are accepted."""
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return False
        # Split a trailing :port, tolerating bracketed IPv6 ("[::1]:8765").
        if host.startswith("["):
            hostname, _, port = host[1:].partition("]")
            port = port.lstrip(":")
        elif ":" in host:
            hostname, _, port = host.rpartition(":")
        else:
            hostname, port = host, ""
        hostname = hostname.strip("[]").lower()
        bound = self._bound_port()
        if port and bound is not None and port != str(bound):
            return False
        if hostname == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

    def _origin_ok(self) -> bool:
        """Validate Origin (or, failing that, Referer): cross-origin pages are
        rejected. A missing Origin/Referer is allowed because the CSRF-token
        header is the primary defence and same-origin fetches do not always set
        Origin; the token check still gates those."""
        bound = self._bound_port()
        for header in ("Origin", "Referer"):
            val = (self.headers.get(header) or "").strip()
            if not val:
                continue
            try:
                u = urlparse(val)
            except ValueError:
                return False
            if u.scheme not in ("http", "https"):
                return False
            hostname = (u.hostname or "").lower()
            if bound is not None and u.port not in (None, bound):
                return False
            if hostname == "localhost":
                return True
            try:
                if ipaddress.ip_address(hostname).is_loopback:
                    return True
            except ValueError:
                pass
            return False           # header present but not same-origin loopback → reject
        return True                # no Origin/Referer → defer to the token check

    def _csrf_ok(self) -> bool:
        """The X-CSRF-Token header must match the per-process token. Constant-time
        compare. A custom header cannot be set by a cross-origin 'simple request',
        so this is the core CSRF defence."""
        sent = self.headers.get("X-CSRF-Token") or ""
        return bool(sent) and secrets.compare_digest(sent, CSRF_TOKEN)

    def _reject_cross_site(self) -> bool:
        """Run all guards for a state-changing POST. On failure, send 403 and
        return True (caller must stop). Fail-closed."""
        if not self._host_ok():
            self._send_json(403, {"ok": False, "error": "bad Host header"})
            return True
        if not self._origin_ok():
            self._send_json(403, {"ok": False, "error": "cross-origin request rejected"})
            return True
        if not self._csrf_ok():
            self._send_json(403, {"ok": False, "error": "missing or invalid CSRF token"})
            return True
        return False

    def do_POST(self):
        p = urlparse(self.path)
        if p.path not in ("/api/config", "/api/open"):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        if self._reject_cross_site():
            return
        if p.path == "/api/config":
            self._handle_post_config()
        else:
            self._handle_post_open(p)

    def _handle_post_config(self):
        """The ONLY write endpoint. Sanitises the body to known keys, persists it
        to the single config file (CONFIG_PATH) and applies it in memory. No other
        path is ever written."""
        body = self._read_json_body()
        if body is None:
            self._send_json(400, {"ok": False, "error": "invalid JSON body"})
            return
        cfg = _coerce_config(body)
        try:
            _write_config_file(cfg)
        except OSError as e:
            self._send_json(500, {"ok": False, "error": f"could not write config: {e}"})
            return
        _apply_config(cfg)
        _tree_cache["json"] = None   # view_ext / icons may have changed → rescan
        self._send_json(200, {"ok": True, "config": config_payload()})

    def _handle_post_open(self, p):
        """OS-association launch of a root-confined file. Disabled (403) unless
        ENABLE_OPEN is on. Never executes a shell string; only a validated path."""
        if not ENABLE_OPEN:
            self._send_json(403, {"ok": False, "error": "open is disabled (start with --enable-open)"})
            return
        rel = (parse_qs(p.query).get("path") or [""])[0]
        target = _safe_open_resolve(rel)
        if target is None:
            self._send_json(404, {"ok": False, "error": "not found or not allowed"})
            return
        try:
            _os_open(target)
        except OSError as e:
            self._send_json(500, {"ok": False, "error": f"could not open: {e}"})
            return
        self._send_json(200, {"ok": True, "path": rel})

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/":
            html = INDEX_HTML.replace("__ROOT__", str(ROOT)).replace(
                "__CSRF_TOKEN__", CSRF_TOKEN)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif p.path == "/api/config":
            self._send_json(200, config_payload())
        elif p.path == "/api/tree":
            force = bool(parse_qs(p.query).get("fresh"))
            self._send(200, _tree_json(force).encode("utf-8"), "application/json; charset=utf-8")
        elif p.path in ("/api/file", "/api/raw"):
            rel = (parse_qs(p.query).get("path") or [""])[0]
            target = _safe_resolve(rel)
            if target is None:
                self._send(404, b"not found or not allowed", "text/plain; charset=utf-8")
                return
            if p.path == "/api/raw":
                suf = target.suffix.lower()
                if suf == ".pdf":
                    ctype = "application/pdf"
                elif suf == ".svg":
                    ctype = "image/svg+xml"
                elif suf in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                    ctype = "image/" + ("jpeg" if suf in (".jpg", ".jpeg") else suf.lstrip("."))
                else:
                    ctype = "text/plain; charset=utf-8"
                self._send(200, target.read_bytes(), ctype)
            else:
                self._send(200, target.read_text(encoding="utf-8", errors="replace").encode("utf-8"),
                           "text/plain; charset=utf-8")
        else:
            # Direct path access (e.g. /sub/README.md) → 302 to the viewer deep-link.
            rel = p.path.lstrip("/")
            if rel and not rel.startswith("api/"):
                self.send_response(302)
                self.send_header("Location", "/#" + rel)
                self.end_headers()
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")


def main(argv: list[str] | None = None) -> int:
    global ROOT, VIEW_EXT, ENABLE_OPEN
    ap = argparse.ArgumentParser(
        prog="mdtree",
        description="Local web viewer for Markdown / PDF / SVG files under a directory tree.",
    )
    ap.add_argument("root", nargs="?", default=str(Path.cwd()),
                    help="root directory to scan (default: current directory)")
    ap.add_argument("--port", type=int, default=8765, help="local server port (default: 8765)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser automatically")
    ap.add_argument("--ext", default=None, metavar='".a,.b"',
                    help="comma/space-separated viewable extensions, overriding the config "
                         '(default: ".md,.markdown,.pdf,.svg")')
    ap.add_argument("--enable-open", action="store_true",
                    help="allow POST /api/open to launch non-viewable files with their OS "
                         "association (default off; root-confined either way)")
    args = ap.parse_args(argv)

    ROOT = Path(args.root).resolve()
    if not ROOT.is_dir():
        print(f"[ERROR] root not found: {ROOT}", file=sys.stderr)
        return 1

    # Load the on-disk config (sets VIEW_EXT / ENABLE_OPEN / project_icons), then
    # let CLI flags override it for this run (they do not rewrite the file).
    load_config(ROOT)
    if args.ext is not None:
        ext = _normalise_ext_list(args.ext)
        VIEW_EXT = tuple(ext) if ext else DEFAULT_VIEW_EXT
    if args.enable_open:
        ENABLE_OPEN = True
    _tree_cache["json"] = None

    url = f"http://127.0.0.1:{args.port}/"

    # Singleton: if already running, just open the browser and exit (taskbar double-click guard).
    import socket
    with socket.socket() as _s:
        _s.settimeout(0.3)
        if _s.connect_ex(("127.0.0.1", args.port)) == 0:
            if not args.no_browser:
                webbrowser.open(url)
            print(f"Already running: {url} (opened browser)")
            return 0

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Markdown Tree Viewer started: {url}\n  root = {ROOT}\n  stop: Ctrl+C")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
