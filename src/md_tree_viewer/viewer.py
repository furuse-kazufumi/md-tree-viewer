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
- Mostly read-only. GET serves only viewable files under the root (path traversal
  is prevented). The ONLY write endpoint, POST /api/config, writes that one config
  file and nothing else. POST /api/open launches a root-confined file with its OS
  association, and is disabled by default (opt-in via --enable-open / config).
- Dependencies: Python standard library only (rendering uses marked.js +
  mermaid.js loaded from a CDN).
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

# Per-project (top-level dir) emoji icons baked into a distribution. The OSS
# package ships an EMPTY map so it never hard-codes anyone's project names; a
# private/local build may seed this with its own defaults. config.project_icons
# always takes precedence, and an unset project falls back to a colour dot.
REPO_ICON: dict[str, str] = {}

# Whether POST /api/open (OS-association launch of non-viewable files) is allowed.
# Default OFF for the OSS package; enabled only via --enable-open or config.
ENABLE_OPEN = False

# Active config (in memory). Mirrors the on-disk config file 1:1.
CONFIG: dict = {}
# Resolved path of the single config file this process reads/writes (set by
# load_config). The ONLY path POST /api/config is ever allowed to write.
CONFIG_PATH: Path | None = None

# The complete set of keys the config file is allowed to carry. POST bodies are
# filtered to these keys so an attacker cannot stash arbitrary data in the file.
CONFIG_KEYS = ("view_ext", "project_icons", "enable_open", "theme")


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
    return cfg


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
    target). Raises OSError on failure; callers map that to a 500."""
    if CONFIG_PATH is None:
        raise OSError("config path not initialised")
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _apply_config(cfg: dict) -> None:
    """Apply a sanitised config to the in-memory globals (VIEW_EXT, REPO_ICON
    precedence is resolved at lookup time, ENABLE_OPEN)."""
    global VIEW_EXT, ENABLE_OPEN, CONFIG
    CONFIG = cfg
    ext = cfg.get("view_ext")
    VIEW_EXT = tuple(ext) if ext else DEFAULT_VIEW_EXT
    if "enable_open" in cfg:
        ENABLE_OPEN = bool(cfg["enable_open"])


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
        "default_view_ext": list(DEFAULT_VIEW_EXT),
        "renderable_ext": list(RENDERABLE_EXT),
        "config_path": str(CONFIG_PATH) if CONFIG_PATH else "",
    }


def _skip_dir(name: str) -> bool:
    n = name.lower()
    if n in NOISE_DIRS or n.startswith("."):
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


def _build_tree(root: Path) -> dict:
    """Walk the root with pruning and return a tree of only the directories that
    contain a viewable file (per the active VIEW_EXT)."""
    nodes: dict[str, dict] = {}  # rel-dir -> node

    def node_for(rel: str) -> dict:
        if rel in nodes:
            return nodes[rel]
        name = root.name if rel == "" else Path(rel).name
        n = {"name": name, "type": "dir", "children": []}
        nodes[rel] = n
        return n

    root_node = node_for("")
    file_dirs: dict[str, list] = {}
    md_jobs: list[tuple[dict, Path]] = []  # (entry, full) — meta extracted in parallel later
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not _skip_dir(d)]
        rel = "" if Path(dp) == root else str(Path(dp).relative_to(root)).replace("\\", "/")
        for f in sorted(fns, key=str.lower):
            ext = Path(f).suffix.lower()
            if ext not in VIEW_EXT:
                continue
            full = Path(dp) / f
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
            elif kind == "md":
                md_jobs.append((entry, full))
            else:
                entry["title"], entry["desc"] = f, f"({ext.lstrip('.').upper() or 'file'})"
            file_dirs.setdefault(rel, []).append(entry)

    # Extract title/desc for .md files in parallel (fast for 1000+ files).
    from concurrent.futures import ThreadPoolExecutor

    def _fill(job: tuple[dict, Path]) -> None:
        entry, full = job
        entry["title"], entry["desc"] = _extract_meta(full)

    if md_jobs:
        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(_fill, md_jobs))

    # Connect only the directories that hold files, walking up to the root.
    def ensure_chain(rel: str) -> dict:
        if rel == "":
            return root_node
        if rel in nodes and nodes[rel].get("_linked"):
            return nodes[rel]
        node = node_for(rel)
        parent_rel = "" if "/" not in rel else rel.rsplit("/", 1)[0]
        parent = ensure_chain(parent_rel)
        if not node.get("_linked"):
            parent["children"].append(node)
            node["_linked"] = True
        return node

    for rel, entries in file_dirs.items():
        d = ensure_chain(rel)
        for e in entries:
            d["children"].append(e)

    def sort_node(n: dict):
        n.pop("_linked", None)
        if n["type"] == "dir":
            n["children"].sort(key=lambda c: (c["type"] == "file", c["name"].lower()))
            # Aggregate the max mtime of the contents (files + subdirs) onto the dir
            # so the client can highlight folders that contain recently modified
            # files; the max propagates to the parent so updates are reachable from
            # the top level.
            mt = 0.0
            for c in n["children"]:
                sort_node(c)
                cm = c.get("mtime", 0.0) or 0.0
                if cm > mt:
                    mt = cm
            n["mtime"] = mt

    sort_node(root_node)
    root_node["gh_repos"] = _gh_repos_map(root)   # repo name -> GitHub blob base URL
    # Per-project icon map (config.project_icons over baked-in REPO_ICON) for every
    # top-level dir, so the client can show emoji icons with a colour-dot fallback.
    icons: dict[str, str] = {}
    for child in root_node["children"]:
        if child.get("type") == "dir":
            ic = project_icon(child["name"])
            if ic:
                icons[child["name"]] = ic
    root_node["project_icons"] = icons
    return root_node


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


_tree_cache = {"json": None, "ts": 0.0}
_TREE_TTL = 5.0  # rescan after this many seconds → new/updated files show up on reload


def _tree_json(force: bool = False) -> str:
    import time
    now = time.monotonic()
    if force or _tree_cache["json"] is None or (now - _tree_cache["ts"]) > _TREE_TTL:
        _tree_cache["json"] = json.dumps(_build_tree(ROOT), ensure_ascii=False)
        _tree_cache["ts"] = now
    return _tree_cache["json"]


def _safe_resolve(rel: str) -> Path | None:
    """Resolve a request path to an existing file under ROOT whose extension is in
    the active VIEW_EXT. Returns None on traversal, missing file or bad extension.
    Used by the read endpoints (/api/file, /api/raw)."""
    try:
        target = (ROOT / rel).resolve()
        target.relative_to(ROOT.resolve())
    except (ValueError, OSError):
        return None
    if not target.is_file() or target.suffix.lower() not in VIEW_EXT:
        return None
    return target


def _safe_open_resolve(rel: str) -> Path | None:
    """Resolve a request path for OS-association launch: confined to ROOT and must
    be an existing regular file, but NOT limited to VIEW_EXT (the point is to open
    non-viewable types). No extension filter widens the attack surface beyond
    'any existing file under root'; combined with ENABLE_OPEN being opt-in and the
    launcher passing a single path argument (never a shell string), this avoids an
    arbitrary-command-execution surface."""
    try:
        target = (ROOT / rel).resolve()
        target.relative_to(ROOT.resolve())
    except (ValueError, OSError):
        return None
    if not target.is_file():
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
<title>Markdown Tree Viewer — __ROOT__</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  window.__mermaid = mermaid;
  mermaid.initialize({ startOnLoad: false, theme: 'neutral' });
</script>
<style>
  :root { --bg:#fff; --fg:#24292f; --side:#f6f8fa; --border:#d0d7de; --accent:#0969da; --muted:#8b949e; }
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
</style></head>
<body><div id="wrap">
  <div id="side">
    <h1>📁 __ROOT__ <span id="refresh" title="Rescan (pick up new files)" style="float:right;cursor:pointer">🔄</span></h1>
    <input id="filter" placeholder="Filter by name or description...">
    <div id="count"></div>
    <div id="tree">Loading...</div>
  </div>
  <div id="drag"></div>
  <div id="content"><div class="empty">Select a file from the tree on the left.</div></div>
</div>
<script>
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
  const dot = document.createElement('span'); dot.className = 'repodot';
  dot.style.background = 'hsl(' + repoHue(repo) + ' 60% 52%)'; dot.title = repo; nm.appendChild(dot);
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

    def do_POST(self):
        p = urlparse(self.path)
        if p.path == "/api/config":
            self._handle_post_config()
        elif p.path == "/api/open":
            self._handle_post_open(p)
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

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
            self._send(200, INDEX_HTML.replace("__ROOT__", str(ROOT)).encode("utf-8"),
                       "text/html; charset=utf-8")
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
    global ROOT
    ap = argparse.ArgumentParser(
        prog="mdtree",
        description="Local read-only web viewer for Markdown / PDF / SVG files under a directory tree.",
    )
    ap.add_argument("root", nargs="?", default=str(Path.cwd()),
                    help="root directory to scan (default: current directory)")
    ap.add_argument("--port", type=int, default=8765, help="local server port (default: 8765)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser automatically")
    args = ap.parse_args(argv)

    ROOT = Path(args.root).resolve()
    if not ROOT.is_dir():
        print(f"[ERROR] root not found: {ROOT}", file=sys.stderr)
        return 1
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
