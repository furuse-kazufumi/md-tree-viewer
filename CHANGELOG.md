# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/) and this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.5.0] - 2026-06-02

### Added — MS-Office (and OpenDocument) files in the listing, opened via OS association

Word / Excel / PowerPoint / Visio / OneNote / OpenDocument extensions (`.doc`
`.docx` `.docm` `.dot` `.dotx` `.rtf` `.xls` `.xlsx` `.xlsm` `.xlsb` `.xlt`
`.xltx` `.ppt` `.pptx` `.pptm` `.pps` `.ppsx` `.pot` `.potx` `.vsd` `.vsdx`
`.one` `.odt` `.ods` `.odp`) now appear in the tree. They are **not rendered
inline** (registry kind `other`); selecting one shows an *"Open with the default
app ↗"* button that POSTs `/api/open`, launching the file with its OS file
association (Word/Excel/PowerPoint), exactly like Explorer. They are deliberately
**not** on the `EXECUTABLE_EXT` deny-list, so the OS hands them to their
application and never ShellExecutes them as code. Opening still requires the
server be started with `--enable-open` (the bundled `open_md_tree.cmd` launcher
now passes it).

### Changed

- **Recent lists show up to 100 entries** (was 30 "Recently opened" / 8 "Recently
  modified"). The client-side MRU cap for "Recently opened" was raised 40 → 100 so
  the larger display is backed by stored history. "Recently modified" reflects the
  full tree (loaded at startup), so it now lists up to 100 most-recently-modified
  files across all scanned directories.

### Security

- The new Office/ODF types are `kind: "other"` (OS-open only), never inline-rendered
  and never added to the inline/scriptable render paths. They remain gated by the
  existing `--enable-open` flag and the unchanged `EXECUTABLE_EXT` deny-list, so no
  new inline-render or active-document surface is introduced.

## [0.4.0] - 2026-06-02

### Added — safe inline rendering of more content types

A **content-type registry** now maps each file extension to a renderer kind and
an exact HTTP `Content-Type`, so the viewer renders every *safe* type a browser
can display natively **without executing embedded script** — not just Markdown /
PDF / SVG. All additions preserve the read-only model and the v0.2.1 / v0.3
behaviour (CSRF/origin/Host guards, pruned-dir read containment, executable-open
deny-list, symlinked-config/cache refusal, lazy loading, persistent scan cache).

- **Images** (`.png` `.jpg` `.jpeg` `.gif` `.webp` `.avif` `.bmp` `.ico`) render
  in an `<img>` — an image (including an `<img>`-loaded SVG) cannot run script.
- **Video** (`.mp4` `.webm` `.ogv`) renders in `<video controls>`; **audio**
  (`.mp3` `.wav` `.ogg` `.m4a` `.flac`) in `<audio controls>`.
- **Text / code** (`.txt` `.json` `.csv` `.xml` `.yaml`/`.yml` `.toml` `.ini`
  `.log` `.rst` `.py` `.js` `.ts`/`.tsx` `.c`/`.cpp`/`.h` `.rs` `.go` `.java`
  `.rb` `.sh` `.bat` `.ps1` and many more) renders in a **client-side
  HTML-escaped `<pre>`**, so a `<script>` inside a `.txt`/`.json` is shown as
  text and **never executed**.
- **`.md` / `.markdown` / `.pdf` / `.svg` are unchanged** — Markdown still uses
  the GFM/Mermaid renderer, PDF the `<iframe>`, and SVG the existing `<img>`
  path. **SVG and HTML are intentionally not widened in this increment**: SVG
  stays on the inert `<img>` path and HTML is *absent from the registry* (a
  later increment will sandbox HTML/SVG in an `<iframe>`). A `.html` added to
  `view_ext` is flagged non-renderable (OS-open only), so it can never be
  injected into the viewer's own origin here.

### Security
- **`/api/raw` Content-Type hardening.** Every raw response now carries the
  exact `Content-Type` from the registry **plus `X-Content-Type-Options:
  nosniff`** (the browser cannot MIME-sniff a text body into something
  executable) **plus `Content-Disposition: inline`** (display, not auto-download).
  Text/code and any unknown extension are served as `text/plain` (**never
  `text/html`, never a sniffable type**), so even a hypothetical sniff bypass
  cannot make the browser treat them as markup.
- **Client-side escape for text/code.** The text viewer HTML-escapes the body
  before inserting it into `<pre>`, so an XSS payload inside a viewed file is
  inert. This is the primary defence; `nosniff` + `text/plain` is
  defence-in-depth.
- **Uniform boundary.** The new types go through the same `_safe_resolve` /
  pruned-dir / traversal checks, so a media-typed secret inside `.git` /
  `node_modules`, or reached via traversal, is still **never** served (404).
- **SVG raw-response script neutralisation.** The viewer renders SVG via an inert
  `<img>`, but `/api/raw?path=*.svg` is a directly reachable GET, and an
  `image/svg+xml` body run as a *top-level* document executes its embedded
  `<script>`. The SVG raw response now carries
  `Content-Security-Policy: default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; sandbox`,
  so a direct top-level navigation to a planted `.svg` under the root can no
  longer run script in the viewer's loopback origin (which would otherwise leak
  the per-process CSRF token and forge `POST /api/config`). The CSP is attached
  only to scriptable-document kinds (currently SVG); image/video/audio/text/PDF
  responses are untouched, so the `<img>`/`<video>`/`<audio>`/`<iframe>` render
  paths and the browser's built-in PDF viewer are unaffected. Additive,
  read-only, no new write surface.

### Changed
- The default `view_ext` expands from `.md,.markdown,.pdf,.svg` to the full set
  of safe content types the registry can render inline. `RENDERABLE_EXT` is now
  derived from the registry so a viewable type is exactly an inline-renderable
  one. README updated.

  **Honest caveat (reiterated):** widening the default viewable set means every
  file with one of those extensions under the root (outside pruned dirs) is
  readable over HTTP. Point the viewer at a directory you are comfortable serving
  to a local browser.

## [0.3.0] - 2026-06-01

### Added — startup speed

For roots with many files, the previous full tree walk on every startup/reload
was slow. v0.3 makes startup cost bounded by the breadth of the top levels rather
than the total file count. All additions preserve the read-only model and the
v0.2.1 security guards (CSRF/origin/Host, pruned-dir read containment,
executable-open deny-list, symlinked-config refusal).

- **Lazy tree loading.** The initial `GET /api/tree` now returns only a *shallow*
  tree (the top ~2 directory levels); directories below that arrive as **lazy
  stubs** and are fetched on demand when you expand them via the new
  `GET /api/tree?path=<dir>` endpoint (a directory's immediate children only).
  The subtree endpoint is confined to the root and refuses pruned/hidden/ignored
  directories, exactly like the read endpoints (404 otherwise). Search and the
  full "recently modified" list lazily fetch the complete tree once
  (`GET /api/tree?full=1`) so deep files are still found.
- **Persistent scan cache.** A snapshot of every scanned directory (keyed by that
  directory's mtime) is stored at `~/.md_tree_viewer/cache/<root-hash>-<sig>.json`.
  Startup re-scans only the directories whose mtime changed (incremental rescan);
  unchanged directories are reused from the cache. The cache file name embeds a
  signature of `view_ext` + `ignore`, so a config change never serves a stale
  tree. **The cache directory is the only location written beyond the single
  config file**, the write refuses a symlinked target, and `--no-cache` disables
  it entirely (every scan walks fresh). A corrupt/old cache is ignored (fail-safe
  full rescan), never an error.
- **`ignore` config + exclusion.** The built-in `NOISE_DIRS` skip (`.git`,
  `node_modules`, `__pycache__`, `.venv`, `dist`, …) is honoured everywhere; you
  can now add your own directory **names** to skip via config `ignore: [...]`
  (editable in the settings panel). Names only — any token with a path separator
  or `..` is dropped, so `ignore` can only exclude, never become a path
  primitive. Ignored directories are excluded from the tree, from `_safe_resolve`,
  and from the lazy subtree endpoint.

### Changed
- New `--no-cache` CLI flag. The config schema gains an optional `ignore` array.
  `GET /api/config` reports the effective `ignore` list. README updated.

## [0.2.1] - 2026-06-01

### Security
Hardening of the two write paths added in 0.2.0, found by an internal security
review. None is remotely reachable (the server binds to `127.0.0.1`), but the
fixes close browser-driven CSRF and read-scope-widening chains:

- **CSRF / origin protection on every POST.** `POST /api/config` and
  `POST /api/open` now require a per-process `X-CSRF-Token` header that matches a
  random token embedded in the served page. A custom header forces a CORS
  pre-flight, so a malicious web page can no longer forge a state-changing
  "simple request" against the loopback server. The `Origin`/`Referer` (when
  present) must be same-origin loopback, and the `Host` header must be a loopback
  literal (DNS-rebinding mitigation). All checks fail closed (403).
- **Read scope no longer escapes pruned/hidden directories.** `GET /api/file` /
  `/api/raw` (and `POST /api/open`) now refuse any path inside a pruned dir
  (`.git`, `node_modules`, dotdirs, virtualenvs, …). Previously, widening
  `view_ext` via config could expose secrets the tree hides (e.g.
  `.git/credentials`, `node_modules/**` tokens). Tree-pruning and the read
  boundary now match.
- **`POST /api/open` refuses executable file types.** A server-side deny-list
  (`.exe`, `.bat`, `.cmd`, `.ps1`, `.vbs`, `.js`, `.hta`, `.lnk`, `.msi`, …)
  blocks types that the OS association would *execute* rather than open, so the
  feature cannot become a one-click code-execution primitive for a malicious file
  that happens to sit under the root.
- **Config write refuses a symlinked target.** `POST /api/config` will not write
  if `<root>/.mdtree.json` is (or resolves through) a symlink or non-regular
  file, matching the symlink resolution already done on the read side.

### Changed
- README and SPEC `§8` security sections updated to describe the CSRF/origin
  guards, the pruned-dir read boundary, the executable-open deny-list, and the
  honest caveat that widening `view_ext` exposes that extension's files under the
  root (so a secret-bearing root should not be served).

## [0.2.0] - 2026-06-01

### Added
- **Settings panel** (⚙️) in the sidebar for editing and persisting configuration.
- **Config persistence**: `GET /api/config` reads, `POST /api/config` writes a
  **single** config file (`<root>/.mdtree.json`, else `~/.md_tree_viewer.json`).
  The POST body is sanitised to known keys (`view_ext`, `project_icons`,
  `enable_open`, `theme`) and no other path is ever written.
- **Configurable viewable extensions** via config `view_ext` and the new `--ext`
  CLI flag (default unchanged: `.md,.markdown,.pdf,.svg`). `_safe_resolve` follows
  the active set. Non-renderable but listed types are shown in the tree and can be
  opened with the OS association.
- **`POST /api/open`**: launch a root-confined, non-viewable file with its OS
  association (`os.startfile` / `open` / `xdg-open`). **Disabled by default**
  (returns 403); enabled via `--enable-open` or `enable_open: true`. The path is
  root-confined and passed as a single argument (no shell string).
- **Per-project icons**: config `project_icons` maps a top-level directory name to
  an emoji, editable in the settings panel. Resolution order is config →
  baked-in default (empty in this OSS build) → deterministic colour-dot fallback.
- **Light / dark theme** toggle (persisted to config; mirrored to `localStorage`
  for flash-free reloads).

### Security
- The previously GET-only server now has two write paths, both narrowly scoped:
  `POST /api/config` writes only the one config file (body sanitised to known
  keys); `POST /api/open` writes nothing and is off by default. All other reads
  remain root-confined with path-traversal rejection.

### Notes
- A private/local build of the same code may default `enable_open` on for personal
  use; this packaged build defaults it off. That on/off default is the only
  behavioural difference between the two builds.

## [0.1.0] - 2026-05-31

### Added
- Initial release. Local, read-only web viewer for Markdown / PDF / SVG files
  under a directory tree.
- Left pane: collapsible file tree with a search filter; each `.md` shows its
  title + opening description (extracted from the heading / first paragraph /
  YAML frontmatter `description:`).
- Right pane: Markdown rendered with GFM tables/code/Mermaid (marked.js +
  mermaid.js via CDN); PDF embedded; SVG shown as an image.
- "Recently opened" / "Recently modified" quick sections; folders that contain
  recently modified files are highlighted.
- Inline-code file paths and relative links open the target doc in the viewer,
  or fall back to the GitHub blob URL when the repo has a GitHub remote.
- Read-only HTTP server (GET only) bound to `127.0.0.1`; only
  `.md/.markdown/.pdf/.svg` under the root are served, with path-traversal
  protection.
- `mdtree` console entry point. Standard library only (no pip dependencies).
