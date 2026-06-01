# md-tree-viewer

A local, **mostly read-only web viewer** for the Markdown / PDF / SVG files in a
directory tree. Point it at a folder full of docs and browse them by
**title + opening description** instead of by filename alone.

- **Left pane** — a collapsible tree of just the viewable files under the root
  (`.md` / `.markdown` / `.pdf` / `.svg` by default), with a live search filter
  and a **settings panel** (⚙️). Each Markdown file shows its title and a
  one-line description so you can tell files apart at a glance.
- **Right pane** — the selected file rendered:
  - Markdown with GitHub-flavored tables, code, **Mermaid** diagrams, and CJK text
  - PDF embedded in the page
  - SVG shown as an image
- **Fast startup, even on huge trees** — the tree loads **lazily** (only the top
  couple of levels up front; deeper folders are fetched when you expand them) and
  a **persistent scan cache** re-scans only the directories that changed since
  last time, so startup cost is bounded by the breadth of the top levels rather
  than the total file count. Dependency dirs, virtualenvs, caches and `.git` are
  skipped while scanning, and you can add your own folder names to skip.
- **Recently opened / Recently modified** quick sections, and folders that
  contain recently changed files are highlighted so you can find your active work.
- **Configurable** — viewable extensions, per-project emoji icons, light/dark
  theme and the OS-open toggle are editable in the settings panel and persist to
  a single config file (see *Settings* below).
- **No build, no dependencies** — Python standard library only. (Rendering loads
  `marked.js` and `mermaid.js` from a CDN, so Markdown styling needs a network
  connection; the tree, PDF and SVG work offline.)

## Install

```bash
pip install md-tree-viewer
```

Requires Python 3.10+.

## Usage

```bash
mdtree                       # scan the current directory, open the browser
mdtree path/to/docs          # scan a specific directory
mdtree --port 9000           # use a different port (default 8765)
mdtree --no-browser          # do not open a browser automatically
mdtree --ext ".md,.rst"      # override the viewable extensions for this run
mdtree --enable-open         # allow OS-association launch of non-viewable files
```

You can also run it as a module:

```bash
python -m md_tree_viewer path/to/docs
```

Then open <http://127.0.0.1:8765/> (opened automatically unless `--no-browser`).

### Options

| Argument        | Default             | Description                              |
|-----------------|---------------------|------------------------------------------|
| `root`          | current directory   | the directory to scan                    |
| `--port`        | `8765`              | local server port                        |
| `--no-browser`  | (browser opens)     | do not open a browser automatically      |
| `--ext`         | `.md,.markdown,.pdf,.svg` | viewable extensions, overriding the config for this run |
| `--enable-open` | off                 | allow `POST /api/open` to launch non-viewable files with their OS association |

## Features in detail

### Titles and descriptions

For each `.md` file the viewer reads only the head of the file and extracts:

- **Title** — the first heading (`#`) or the first non-empty line.
- **Description** — the first real paragraph, or the YAML frontmatter
  `description:` field when present (frontmatter wins).

### Cross-file links

- Relative links inside a rendered document open the target document **inside
  the viewer** (no 404s when browsing a doc set).
- Inline-code file paths (e.g. `` `docs/guide.md` ``) become clickable: local
  `.md`/`.pdf` open in the viewer; anything else links to the file on GitHub
  **if** the containing repository has a GitHub `origin` remote.

### Settings

Open the **⚙️ settings panel** in the top-left to change, and persist:

- **Viewable extensions** — add or remove the file types shown in the tree.
  `md` / `markdown` / `pdf` / `svg` render inline; any other listed type (e.g.
  `.rst`, `.txt`) appears in the tree but opens via the OS association (see
  below). Also settable per run with `--ext`.
- **Project icons** — assign an emoji to a top-level directory
  (`{"<dir>": "<emoji>"}`). The tree shows that emoji; unset projects fall back
  to a deterministic colour dot, so you can always tell projects apart.
- **OS-association open** — toggle whether non-viewable files can be launched.
- **Theme** — light or dark.

Settings are saved by the server to a **single config file**, searched/written in
this order: `<root>/.mdtree.json`, then `~/.md_tree_viewer.json`. The schema:

```json
{
  "view_ext": [".md", ".markdown", ".pdf", ".svg"],
  "project_icons": { "docs": "📘" },
  "enable_open": false,
  "theme": "light"
}
```

### Opening non-viewable files (OS association)

A file type listed in `view_ext` but not rendered inline (e.g. `.xlsx`) can be
launched with the machine's default application via `POST /api/open?path=…`
(`os.startfile` on Windows, `open` on macOS, `xdg-open` on Linux).

- **Disabled by default in this package.** It is only enabled when the server is
  started with `--enable-open` or `enable_open: true` in the config; otherwise
  the endpoint returns **403**. This is deliberate: launching files on the host
  is unsafe to expose unconditionally, so an OSS deployment stays off unless the
  operator opts in. (A private/local build of the same code may instead default
  it on for personal convenience — that on/off default is the only behavioural
  difference between the two builds.)
- The path is **confined to the root**, must be an existing file, and must **not**
  live in a pruned/hidden directory (`.git`, `node_modules`, …). The server passes
  a single validated path to the launcher (never a shell string), so it does not
  create a shell-injection surface.
- **Executable types are refused.** Even when enabled, the server rejects file
  types that the OS association would *run* rather than open (`.exe`, `.bat`,
  `.cmd`, `.ps1`, `.vbs`, `.js`, `.hta`, `.lnk`, `.msi`, …). This keeps the
  feature for viewing documents (e.g. `.xlsx`, `.png`) and prevents it from
  becoming a code-execution primitive if an executable happens to sit under the
  root.
- Like every state-changing request, `POST /api/open` is **CSRF-protected** (see
  *Security* below), so another web page open in your browser cannot trigger it.

### Security

This release adds two write paths to what was previously a GET-only server. The
read-only model is preserved everywhere except those two, narrowly-scoped,
endpoints:

- The server binds to `127.0.0.1` (local only).
- **Reads** (`GET /api/file`, `/api/raw`): only files **under the root** whose
  extension is in the active `view_ext` are served. Requests are resolved against
  the root and **path traversal is rejected** (symlinks are resolved and
  re-checked). Files inside pruned/hidden directories (`.git`, `node_modules`,
  dotdirs, virtualenvs, …) are **never** served, even if you widen `view_ext` to
  their extension — the read boundary matches what the tree hides, so a config
  change cannot leak secrets such as `.git/credentials` or `node_modules/**`
  tokens.
- **The only write endpoint is `POST /api/config`,** and it writes **exactly one
  file** — the config file described above — and nothing else. The request body
  is sanitised to a fixed set of known keys (`view_ext`, `project_icons`,
  `enable_open`, `theme`); unknown keys and malformed values are dropped, so a
  request cannot stash arbitrary data or influence any other path. There is no
  endpoint that writes any file you choose. The write is also refused if the
  config path is a symlink (it cannot be redirected to an outside file).
- **`POST /api/open`** does not write files; it launches a root-confined existing
  file with its OS association, refuses executable types, and is **off by
  default** (see above).
- **CSRF / origin protection.** Both POST endpoints require a per-process
  `X-CSRF-Token` header matching a random token embedded in the page. Because a
  custom header forces a CORS pre-flight, a malicious page open in your browser
  cannot forge a state-changing request against the loopback server. In addition,
  the `Host` header must be a loopback literal (DNS-rebinding mitigation) and any
  `Origin`/`Referer` must be same-origin. All checks fail closed (403).

**Honest caveat on `view_ext`:** widening the viewable extensions means **every
file with that extension under the root (outside pruned dirs) becomes readable
over HTTP**. Point the viewer at a directory whose contents you are comfortable
serving to a local browser, and avoid adding extensions like `.txt`/`.env`/`.pem`
to a root that mixes documents with secrets.

## Why

When a folder accumulates hundreds or thousands of Markdown notes, design docs,
diagrams and reports, the filenames stop being enough to find anything. This
viewer gives every file a human-readable title and summary in one scrollable,
searchable tree — and renders Mermaid and tables the way you expect — without
installing a static-site generator or any third-party packages.

## Development

```bash
git clone https://github.com/furuse-kazufumi/md-tree-viewer
cd md-tree-viewer
pip install -e ".[test]"   # or: pip install pytest && pip install -e .
pytest
```

## License

[MIT](LICENSE)
