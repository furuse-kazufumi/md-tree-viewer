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
        try:
            urlopen(base + "/api/file?path=secret.txt")
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# v0.2: config helpers, VIEW_EXT, /api/config, /api/open, project icons.
# --------------------------------------------------------------------------- #

def _post(url, payload=None):
    """POST helper returning (status, json-or-None)."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urlopen(req) as r:
            body = r.read().decode("utf-8")
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        return e.code, (json.loads(body) if body else None)


def test_normalise_ext_list_string_and_list():
    assert viewer._normalise_ext_list(".md,.rst") == [".md", ".rst"]
    assert viewer._normalise_ext_list("md .rst  txt") == [".md", ".rst", ".txt"]
    assert viewer._normalise_ext_list([".MD", "rst", ".rst"]) == [".md", ".rst"]  # lower + dedup
    assert viewer._normalise_ext_list(None) == []


def test_coerce_config_drops_unknown_and_bad():
    cfg = viewer._coerce_config({
        "view_ext": ".md, .rst",
        "project_icons": {"llive": "🧠", "": "x", "bad": "  "},
        "enable_open": True,
        "theme": "dark",
        "evil": "rm -rf /",          # unknown key dropped
        "extra_path": "/etc/passwd",  # unknown key dropped
    })
    assert cfg["view_ext"] == [".md", ".rst"]
    assert cfg["project_icons"] == {"llive": "🧠"}   # blank name/value dropped
    assert cfg["enable_open"] is True
    assert cfg["theme"] == "dark"
    assert "evil" not in cfg and "extra_path" not in cfg
    # Bad theme value is dropped, not coerced.
    assert "theme" not in viewer._coerce_config({"theme": "neon"})


def test_config_get_post_round_trip(sample_tree, monkeypatch):
    server, port = _serve(sample_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        # GET reports the effective defaults.
        with urlopen(base + "/api/config") as r:
            cfg = json.loads(r.read())
        assert cfg["view_ext"] == list(viewer.DEFAULT_VIEW_EXT)
        assert cfg["enable_open"] is False
        # POST persists and applies.
        status, j = _post(base + "/api/config", {
            "view_ext": [".md", ".markdown", ".pdf", ".svg", ".rst"],
            "project_icons": {"docs": "📘"},
            "enable_open": False,
            "theme": "dark",
        })
        assert status == 200 and j["ok"] is True
        # The single config file was written, and only it.
        cfg_file = sample_tree / ".mdtree.json"
        assert cfg_file.is_file()
        on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert on_disk["view_ext"][-1] == ".rst"
        assert on_disk["project_icons"] == {"docs": "📘"}
        # GET now reflects the saved values.
        with urlopen(base + "/api/config") as r:
            cfg2 = json.loads(r.read())
        assert ".rst" in cfg2["view_ext"]
        assert cfg2["theme"] == "dark"
    finally:
        server.shutdown()
        server.server_close()


def test_config_post_only_writes_config_file(sample_tree):
    """A malicious POST cannot create files outside the single config path, and
    unknown keys are stripped (no arbitrary data persisted)."""
    server, port = _serve(sample_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        before = {p.name for p in sample_tree.iterdir()}
        status, j = _post(base + "/api/config", {
            "view_ext": [".md"],
            "../escape.json": "x",        # not a known key
            "outfile": "/tmp/pwned",      # not a known key
            "evil": "data",
        })
        assert status == 200 and j["ok"] is True
        after = {p.name for p in sample_tree.iterdir()}
        # Only .mdtree.json appeared; no stray files anywhere in the root.
        assert after - before == {".mdtree.json"}
        # No traversal artifact above root.
        assert not (sample_tree.parent / "escape.json").exists()
        on_disk = json.loads((sample_tree / ".mdtree.json").read_text(encoding="utf-8"))
        assert set(on_disk) <= set(viewer.CONFIG_KEYS)   # only sanctioned keys
    finally:
        server.shutdown()
        server.server_close()


def test_config_post_invalid_json_400(sample_tree):
    server, port = _serve(sample_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(base + "/api/config", data=b"not json{",
                                     method="POST", headers={"Content-Type": "application/json"})
        try:
            urlopen(req)
            assert False, "expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        server.shutdown()
        server.server_close()


def test_view_ext_reflected_in_tree_and_resolve(sample_tree, monkeypatch):
    """Adding .txt to VIEW_EXT makes secret.txt both listed and resolvable; the
    default set keeps it hidden."""
    # Default: secret.txt is not viewable.
    assert viewer._safe_resolve("secret.txt") is None
    assert "secret.txt" not in json.dumps(viewer._build_tree(sample_tree))
    # Override the active VIEW_EXT (as config / --ext would).
    monkeypatch.setattr(viewer, "VIEW_EXT", (".md", ".markdown", ".pdf", ".svg", ".txt"))
    viewer._tree_cache["json"] = None
    assert viewer._safe_resolve("secret.txt") is not None
    blob = json.dumps(viewer._build_tree(sample_tree))
    assert "secret.txt" in blob
    # The .txt entry is flagged non-renderable (it opens via OS association, not inline).
    tree = viewer._build_tree(sample_tree)
    txt = [n for n in _iter_files(tree) if n["name"] == "secret.txt"][0]
    assert txt["renderable"] is False


def _iter_files(node):
    if node.get("type") == "file":
        yield node
        return
    for c in node.get("children", []):
        yield from _iter_files(c)


def test_open_disabled_by_default_403(sample_tree):
    server, port = _serve(sample_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        status, j = _post(base + "/api/open?path=README.md")
        assert status == 403
        assert j and j["ok"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_open_enabled_resolves_and_blocks_traversal(sample_tree, monkeypatch):
    """When enabled, /api/open resolves root-confined paths and rejects traversal.
    The actual launcher is stubbed so no real app is spawned in tests."""
    monkeypatch.setattr(viewer, "ENABLE_OPEN", True)
    launched = []
    monkeypatch.setattr(viewer, "_os_open", lambda p: launched.append(p))
    server, port = _serve(sample_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        # Traversal is rejected (404) even with open enabled.
        status, _ = _post(base + "/api/open?path=../../etc/passwd")
        assert status == 404
        assert launched == []
        # A real root-confined file launches.
        status, j = _post(base + "/api/open?path=README.md")
        assert status == 200 and j["ok"] is True
        assert len(launched) == 1
        assert launched[0].name == "README.md"
    finally:
        server.shutdown()
        server.server_close()


def test_safe_open_resolve_not_limited_to_view_ext(sample_tree):
    """_safe_open_resolve confines to root and requires an existing file, but
    (unlike _safe_resolve) is not limited to VIEW_EXT — that is the point of open."""
    assert viewer._safe_open_resolve("secret.txt") is not None   # non-viewable but real
    assert viewer._safe_open_resolve("../../etc/passwd") is None  # traversal
    assert viewer._safe_open_resolve("missing.xyz") is None       # absent


def test_project_icon_resolution_order(monkeypatch):
    """config.project_icons wins over baked REPO_ICON; unknown projects get ''."""
    monkeypatch.setattr(viewer, "REPO_ICON", {"llive": "BAKED", "llmesh": "🕸️"})
    monkeypatch.setattr(viewer, "CONFIG", {"project_icons": {"llive": "🧠"}})
    assert viewer.project_icon("llive") == "🧠"      # config over baked
    assert viewer.project_icon("llmesh") == "🕸️"     # baked when no config entry
    assert viewer.project_icon("unknown") == ""       # neither → color-dot fallback


def test_build_tree_attaches_project_icons(sample_tree, monkeypatch):
    monkeypatch.setattr(viewer, "CONFIG", {"project_icons": {"docs": "📘"}})
    tree = viewer._build_tree(sample_tree)
    assert tree.get("project_icons", {}).get("docs") == "📘"


def test_load_config_applies_file(tmp_path, monkeypatch):
    monkeypatch.setattr(viewer, "ROOT", tmp_path)
    monkeypatch.setattr(viewer, "VIEW_EXT", viewer.DEFAULT_VIEW_EXT)
    monkeypatch.setattr(viewer, "ENABLE_OPEN", False)
    monkeypatch.setattr(viewer, "CONFIG", {})
    monkeypatch.setattr(viewer, "CONFIG_PATH", None)
    (tmp_path / ".mdtree.json").write_text(
        json.dumps({"view_ext": [".md", ".org"], "enable_open": True,
                    "project_icons": {"x": "🦖"}}),
        encoding="utf-8",
    )
    cfg = viewer.load_config(tmp_path)
    assert cfg["view_ext"] == [".md", ".org"]
    assert viewer.VIEW_EXT == (".md", ".org")
    assert viewer.ENABLE_OPEN is True
    assert viewer.CONFIG_PATH == tmp_path / ".mdtree.json"
