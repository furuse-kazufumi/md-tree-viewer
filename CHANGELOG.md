# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/) and this project adheres to
[Semantic Versioning](https://semver.org/).

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
