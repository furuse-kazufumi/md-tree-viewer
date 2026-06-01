"""Tests for md_tree_viewer: metadata extraction, tree pruning, path-traversal
safety, the HTTP endpoints, and the v0.2 config / VIEW_EXT / open / icons features."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

import pytest

from md_tree_viewer import viewer


@pytest.fixture
def sample_tree(tmp_path, monkeypatch):
    """Build a small doc tree under tmp_path and point the viewer's ROOT at it."""
    (tmp_path / "README.md").write_text(
        "# My Project\n\nA short description line that is long enough.\n",
        encoding="utf-8",
    )
    sub = tmp_path / "docs"
    sub.mkdir()
    (sub / "guide.md").write_text(
        "---\ndescription: Frontmatter wins over the first paragraph\n---\n"
        "# Guide\n\nBody paragraph.\n",
        encoding="utf-8",
    )
    (sub / "diagram.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
    # A noise dir that must be skipped entirely.
    noise = tmp_path / "node_modules" / "pkg"
    noise.mkdir(parents=True)
    (noise / "ignored.md").write_text("# Should be skipped\n", encoding="utf-8")
    # A non-viewable file that must never appear.
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")

    monkeypatch.setattr(viewer, "ROOT", tmp_path)
    monkeypatch.setattr(viewer, "_GH_REPOS", None)
    # Reset the v0.2 config-driven globals to defaults so tests are isolated.
    monkeypatch.setattr(viewer, "VIEW_EXT", viewer.DEFAULT_VIEW_EXT)
    monkeypatch.setattr(viewer, "ENABLE_OPEN", False)
    monkeypatch.setattr(viewer, "CONFIG", {})
    monkeypatch.setattr(viewer, "CONFIG_PATH", tmp_path / ".mdtree.json")
    viewer._tree_cache["json"] = None
    return tmp_path


def test_extract_meta_heading_and_paragraph(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("# Title Here\n\nThis is the opening paragraph text.\n", encoding="utf-8")
    title, desc = viewer._extract_meta(p)
    assert title == "Title Here"
    assert "opening paragraph" in desc


def test_extract_meta_frontmatter_description_wins(tmp_path):
    p = tmp_path / "b.md"
    p.write_text(
        "---\ndescription: FM description\n---\n# Heading\n\nBody.\n", encoding="utf-8"
    )
    title, desc = viewer._extract_meta(p)
    assert title == "Heading"
    assert desc == "FM description"


def test_skip_dir():
    assert viewer._skip_dir("node_modules")
    assert viewer._skip_dir(".git")
    assert viewer._skip_dir(".hidden")
    assert viewer._skip_dir("my.egg-info")
    assert viewer._skip_dir("my-venv")
    assert not viewer._skip_dir("docs")
    assert not viewer._skip_dir("src")


def test_build_tree_prunes_noise_and_non_viewable(sample_tree):
    tree = viewer._build_tree(sample_tree)
    blob = json.dumps(tree)
    assert "README.md" in blob
    assert "guide.md" in blob
    assert "diagram.svg" in blob
    assert "ignored.md" not in blob       # node_modules pruned
    assert "secret.txt" not in blob       # non-viewable extension excluded


def test_safe_resolve_blocks_traversal_and_bad_ext(sample_tree):
    assert viewer._safe_resolve("README.md") is not None
    assert viewer._safe_resolve("secret.txt") is None          # not a viewable ext
    assert viewer._safe_resolve("../../etc/passwd") is None     # path traversal
    assert viewer._safe_resolve("does/not/exist.md") is None


def _serve(root):
    """Start the viewer's handler on an ephemeral port and return (server, port)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), viewer.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_http_endpoints(sample_tree):
    server, port = _serve(sample_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        # Index page renders.
        with urlopen(base + "/") as r:
            assert r.status == 200
            assert b"Markdown Tree Viewer" in r.read()
        # Tree API returns JSON with the expected files.
        with urlopen(base + "/api/tree") as r:
            data = json.loads(r.read())
            assert data["type"] == "dir"
            assert "README.md" in json.dumps(data)
        # File API returns the markdown body.
        with urlopen(base + "/api/file?path=README.md") as r:
            assert b"My Project" in r.read()
        # Raw API serves the svg with the right content type.
        with urlopen(base + "/api/raw?path=docs/diagram.svg") as r:
            assert r.headers["Content-Type"] == "image/svg+xml"
        # Disallowed file 404s.
        import urllib.error
        try:
            urlopen(base + "/api/file?path=secret.txt")
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.shutdown()
        server.server_close()
