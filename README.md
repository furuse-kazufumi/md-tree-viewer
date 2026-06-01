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
- **Fast & quiet** — dependency dirs, virtualenvs, caches and `.git` are skipped
  while scanning, so a tree with thousands of files stays responsive.
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
mdtree                 # scan the current directory, open the browser
mdtree path/to/docs    # scan a specific directory
mdtree --port 9000     # use a different port (default 8765)
mdtree --no-browser    # do not open a browser automatically
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

### Security

- The server is **read-only** (HTTP `GET` only) and binds to `127.0.0.1`.
- Only `.md` / `.markdown` / `.pdf` / `.svg` files **under the root** are served.
  Requests are resolved against the root and **path traversal is rejected**.

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
