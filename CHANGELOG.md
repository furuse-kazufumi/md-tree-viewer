# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/) and this project adheres to
[Semantic Versioning](https://semver.org/).

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
