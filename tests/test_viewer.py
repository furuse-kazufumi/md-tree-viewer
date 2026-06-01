"""Tests for md_tree_viewer: metadata extraction, tree pruning, path-traversal
safety, the HTTP endpoints, and the v0.2 config / VIEW_EXT / open / icons features."""
import json
import threading
import urllib.error
import urllib.parse
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
    # Secrets hidden inside pruned dirs (.git / node_modules) — they must stay
    # unreachable even if VIEW_EXT is widened to their extension via config.
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / "credentials.txt").write_text("https://user:token@github.com", encoding="utf-8")
    (noise / "npm.txt").write_text("NPM_TOKEN=abc123", encoding="utf-8")
    # A non-viewable file that must never appear (top-level, not in a pruned dir).
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    # A would-be executable that OS-association open must refuse to launch.
    (tmp_path / "payload.bat").write_text("echo pwned", encoding="utf-8")

    monkeypatch.setattr(viewer, "ROOT", tmp_path)
    monkeypatch.setattr(viewer, "_GH_REPOS", None)
    # Reset the v0.2 config-driven globals to defaults so tests are isolated.
    monkeypatch.setattr(viewer, "VIEW_EXT", viewer.DEFAULT_VIEW_EXT)
    monkeypatch.setattr(viewer, "ENABLE_OPEN", False)
    monkeypatch.setattr(viewer, "CONFIG", {})
    monkeypatch.setattr(viewer, "CONFIG_PATH", tmp_path / ".mdtree.json")
    # v0.3: reset ignore set and isolate the persistent scan cache to a tmp dir so
    # tests never touch ~/.md_tree_viewer and never see each other's cache. The
    # cache dir lives OUTSIDE the scanned root (a sibling) so writing it does not
    # perturb the root's mtime or appear in the tree.
    monkeypatch.setattr(viewer, "IGNORE_DIRS", frozenset())
    cache_dir = tmp_path.parent / (tmp_path.name + "_cache")
    monkeypatch.setattr(viewer, "_cache_dir", lambda: cache_dir)
    viewer._reset_tree_cache()
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

def _post(url, payload=None, headers=None, csrf=True):
    """POST helper returning (status, json-or-None).

    By default it sends a valid X-CSRF-Token (the server's per-process token) so
    happy-path tests pass the CSRF guard. Pass csrf=False (or override via
    `headers`) to exercise the rejection paths."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if csrf:
        hdrs["X-CSRF-Token"] = viewer.CSRF_TOKEN
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method="POST", headers=hdrs)
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
        req = urllib.request.Request(
            base + "/api/config", data=b"not json{", method="POST",
            headers={"Content-Type": "application/json", "X-CSRF-Token": viewer.CSRF_TOKEN})
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


# --------------------------------------------------------------------------- #
# v0.2.1 security hardening: pruned-dir read containment, executable-open
# deny-list, config-write symlink refusal, and CSRF/Origin/Host guards.
# --------------------------------------------------------------------------- #

def test_widening_view_ext_cannot_reach_pruned_dirs(sample_tree, monkeypatch):
    """Widening VIEW_EXT (e.g. to .txt via config) must NOT expose files inside
    pruned/hidden dirs (.git, node_modules). Tree-pruning is matched by the read
    boundary so a config change cannot leak secrets the tree hides."""
    monkeypatch.setattr(viewer, "VIEW_EXT",
                        (".md", ".markdown", ".pdf", ".svg", ".txt"))
    viewer._tree_cache["json"] = None
    # The top-level .txt becomes reachable (intended) ...
    assert viewer._safe_resolve("secret.txt") is not None
    # ... but secrets behind pruned dirs stay unreachable.
    assert viewer._safe_resolve(".git/credentials.txt") is None
    assert viewer._safe_resolve("node_modules/pkg/npm.txt") is None
    # And they never appear in the tree either.
    blob = json.dumps(viewer._build_tree(sample_tree))
    assert "credentials.txt" not in blob
    assert "npm.txt" not in blob


def test_pruned_dir_read_blocked_over_http(sample_tree, monkeypatch):
    """End-to-end: even with .txt enabled, GET cannot fetch .git/node_modules
    secrets (404), while the legit top-level .txt is served."""
    monkeypatch.setattr(viewer, "VIEW_EXT",
                        (".md", ".markdown", ".pdf", ".svg", ".txt"))
    viewer._tree_cache["json"] = None
    server, port = _serve(sample_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        with urlopen(base + "/api/file?path=secret.txt") as r:
            assert r.status == 200            # top-level .txt is now viewable
        for hidden in (".git/credentials.txt", "node_modules/pkg/npm.txt"):
            try:
                urlopen(base + "/api/file?path=" + urllib.parse.quote(hidden))
                assert False, f"expected 404 for {hidden}"
            except urllib.error.HTTPError as e:
                assert e.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_open_refuses_executable_extensions(sample_tree, monkeypatch):
    """Even with open enabled and the file under root, ShellExecute-able types
    (.bat etc.) are refused so /api/open is not a code-execution primitive."""
    monkeypatch.setattr(viewer, "ENABLE_OPEN", True)
    launched = []
    monkeypatch.setattr(viewer, "_os_open", lambda p: launched.append(p))
    # Resolver level: executable extension is rejected.
    assert viewer._safe_open_resolve("payload.bat") is None
    assert ".bat" in viewer.EXECUTABLE_EXT
    server, port = _serve(sample_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        status, _ = _post(base + "/api/open?path=payload.bat")
        assert status == 404
        assert launched == []                 # never launched
        # A safe, non-executable file still opens.
        status, j = _post(base + "/api/open?path=secret.txt")
        assert status == 200 and j["ok"] is True
        assert len(launched) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_open_resolver_blocks_pruned_dirs(sample_tree):
    """_safe_open_resolve also honours the pruned-dir boundary."""
    assert viewer._safe_open_resolve(".git/credentials.txt") is None
    assert viewer._safe_open_resolve("node_modules/pkg/npm.txt") is None


def test_write_config_refuses_symlink(tmp_path, monkeypatch):
    """If CONFIG_PATH is a symlink (e.g. pre-planted to point outside root), the
    write is refused so the only write target cannot be redirected."""
    victim = tmp_path / "victim_outside.json"
    victim.write_text("ORIGINAL", encoding="utf-8")
    link = tmp_path / ".mdtree.json"
    try:
        link.symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    monkeypatch.setattr(viewer, "CONFIG_PATH", link)
    with pytest.raises(OSError):
        viewer._write_config_file({"view_ext": [".md"]})
    # The victim file was not overwritten.
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"


def test_post_config_requires_csrf_token(sample_tree):
    """POST /api/config without (or with a wrong) X-CSRF-Token is rejected 403
    and does not write the config file."""
    server, port = _serve(sample_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        cfg_file = sample_tree / ".mdtree.json"
        # No token.
        status, j = _post(base + "/api/config", {"view_ext": [".md"]}, csrf=False)
        assert status == 403 and j and j["ok"] is False
        assert not cfg_file.exists()
        # Wrong token.
        status, _ = _post(base + "/api/config", {"view_ext": [".md"]},
                          headers={"X-CSRF-Token": "wrong-token"}, csrf=False)
        assert status == 403
        assert not cfg_file.exists()
    finally:
        server.shutdown()
        server.server_close()


def test_post_open_requires_csrf_token(sample_tree, monkeypatch):
    """POST /api/open without a valid token is rejected before ENABLE_OPEN is
    even consulted, and never launches."""
    monkeypatch.setattr(viewer, "ENABLE_OPEN", True)
    launched = []
    monkeypatch.setattr(viewer, "_os_open", lambda p: launched.append(p))
    server, port = _serve(sample_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        status, j = _post(base + "/api/open?path=README.md", csrf=False)
        assert status == 403
        assert launched == []
    finally:
        server.shutdown()
        server.server_close()


def test_post_rejects_cross_origin(sample_tree):
    """A cross-origin Origin header is rejected even if a token is supplied
    (defence in depth; the token alone already stops simple-request CSRF)."""
    server, port = _serve(sample_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        status, j = _post(base + "/api/config", {"view_ext": [".md"]},
                          headers={"Origin": "https://evil.example.com"})
        assert status == 403 and j and "cross-origin" in (j.get("error") or "")
        assert not (sample_tree / ".mdtree.json").exists()
        # Same-origin Origin is accepted.
        status, j = _post(base + "/api/config", {"view_ext": [".md"]},
                          headers={"Origin": f"http://127.0.0.1:{port}"})
        assert status == 200 and j["ok"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_post_rejects_bad_host_header(sample_tree):
    """A non-loopback Host header (DNS-rebinding attempt) is rejected."""
    server, port = _serve(sample_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        status, j = _post(base + "/api/config", {"view_ext": [".md"]},
                          headers={"Host": "attacker.example.com"})
        assert status == 403 and j and "Host" in (j.get("error") or "")
        assert not (sample_tree / ".mdtree.json").exists()
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# v0.3 startup speed: lazy subtree endpoint, persistent scan cache (incremental
# rescan), config `ignore`, and shallow startup walk.
# --------------------------------------------------------------------------- #

@pytest.fixture
def deep_tree(sample_tree):
    """Extend the sample tree with a deeper directory chain so lazy/shallow
    behaviour is observable. Adds docs/sub/deep/deepfile.md (3 levels under root)
    and a top-level skipme/ dir with a markdown file (for the `ignore` test)."""
    deep = sample_tree / "docs" / "sub" / "deep"
    deep.mkdir(parents=True)
    (deep / "deepfile.md").write_text("# Deep\n\nA deeply nested document.\n", encoding="utf-8")
    skip = sample_tree / "skipme"
    skip.mkdir()
    (skip / "noted.md").write_text("# In skipme\n\nShould be excluded when ignored.\n", encoding="utf-8")
    return sample_tree


def test_normalise_ignore_list_names_only():
    """`ignore` accepts bare directory names; path-bearing / traversal tokens are
    dropped so it can never become a path primitive."""
    assert viewer._normalise_ignore_list("Build, Tmp") == ["build", "tmp"]
    assert viewer._normalise_ignore_list(["A", "a", "b"]) == ["a", "b"]   # lower + dedup
    # Anything with a separator or traversal is rejected outright.
    assert viewer._normalise_ignore_list(["../etc", "a/b", "c\\d", "..", "."]) == []
    assert viewer._normalise_ignore_list(None) == []


def test_coerce_config_accepts_ignore():
    cfg = viewer._coerce_config({"ignore": ["dist", "Build", "a/b"]})
    assert cfg["ignore"] == ["dist", "build"]      # a/b dropped, lower-cased
    # ignore is a sanctioned key.
    assert "ignore" in viewer.CONFIG_KEYS


def test_ignore_excludes_dir_from_tree(deep_tree, monkeypatch):
    """A directory named in the active IGNORE_DIRS is skipped while scanning, just
    like the built-in NOISE_DIRS."""
    # Present by default.
    blob = json.dumps(viewer._build_tree(deep_tree, use_cache=False))
    assert "noted.md" in blob
    # With skipme ignored, it disappears from the tree and is unresolvable.
    monkeypatch.setattr(viewer, "IGNORE_DIRS", frozenset({"skipme"}))
    viewer._reset_tree_cache()
    assert viewer._skip_dir("skipme")
    blob = json.dumps(viewer._build_tree(deep_tree, use_cache=False))
    assert "noted.md" not in blob
    assert viewer._safe_resolve("skipme/noted.md") is None
    # The lazy directory resolver also refuses an ignored dir.
    assert viewer._safe_resolve_dir("skipme") is None


def test_ignore_round_trips_through_config_post(sample_tree):
    """POST /api/config persists `ignore`, and GET reflects it."""
    server, port = _serve(sample_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        status, j = _post(base + "/api/config", {"ignore": ["Build", "tmp", "bad/x"]})
        assert status == 200 and j["ok"] is True
        assert sorted(j["config"]["ignore"]) == ["build", "tmp"]   # path token dropped
        on_disk = json.loads((sample_tree / ".mdtree.json").read_text(encoding="utf-8"))
        assert on_disk["ignore"] == ["build", "tmp"]
        with urlopen(base + "/api/config") as r:
            cfg = json.loads(r.read())
        assert sorted(cfg["ignore"]) == ["build", "tmp"]
    finally:
        server.shutdown()
        server.server_close()


def test_shallow_build_emits_lazy_stub_for_deep_dirs(deep_tree):
    """With max_depth=SHALLOW_DEPTH (=2), the top 2 directory levels are walked and
    anything deeper arrives as a lazy stub (empty children, lazy=True), so startup
    does not walk everything. Here docs(1) → sub(2) are shown, deep(3) is lazy."""
    shallow = viewer._build_tree(deep_tree, max_depth=viewer.SHALLOW_DEPTH, use_cache=False)
    docs = [c for c in shallow["children"] if c.get("name") == "docs"][0]
    sub = [c for c in docs["children"] if c.get("name") == "sub"][0]
    assert sub.get("lazy") is not True          # within the depth limit → fully built
    deep = [c for c in sub["children"] if c.get("name") == "deep"][0]
    # `deep` is at level 3 (> limit) → lazy stub with no children.
    assert deep.get("lazy") is True
    assert deep["children"] == []
    # The deep file is NOT present in the shallow payload.
    assert "deepfile.md" not in json.dumps(shallow)
    # A depth-1 build truncates even sooner: docs' subdirs become lazy stubs.
    d1 = viewer._build_tree(deep_tree, max_depth=1, use_cache=False)
    docs1 = [c for c in d1["children"] if c.get("name") == "docs"][0]
    sub1 = [c for c in docs1["children"] if c.get("name") == "sub"][0]
    assert sub1.get("lazy") is True and sub1["children"] == []
    # But the FULL build does contain the deep file (depth unlimited).
    full = viewer._build_tree(deep_tree, use_cache=False)
    assert "deepfile.md" in json.dumps(full)


def test_shallow_build_does_not_walk_deeper_than_limit(deep_tree, monkeypatch):
    """A shallow build must not _extract_meta files below the depth limit — proof
    the deep directories are not scanned for content (only existence-probed). With
    max_depth=1, root + its immediate child dirs (docs) are scanned, but the deep
    chain docs/sub/deep is not."""
    seen = []
    real = viewer._extract_meta
    monkeypatch.setattr(viewer, "_extract_meta", lambda p: (seen.append(str(p)), real(p))[1])
    viewer._build_tree(deep_tree, max_depth=1, use_cache=False)
    # docs/sub/deep/deepfile.md is 3 levels down → never read for metadata.
    assert not any("deepfile.md" in s for s in seen)
    # docs/guide.md lives in a depth-1 directory → it IS scanned (expected).
    assert any("guide.md" in s for s in seen)


def test_lazy_subtree_endpoint_returns_children(deep_tree):
    """GET /api/tree?path=<dir> returns that directory's immediate children."""
    server, port = _serve(deep_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        with urlopen(base + "/api/tree?path=" + urllib.parse.quote("docs/sub")) as r:
            sub = json.loads(r.read())
        assert sub["type"] == "dir"
        names = {c["name"] for c in sub["children"]}
        assert "deep" in names              # the deep dir appears (as a stub)
        # Its grandchild file is NOT inlined here (one level only).
        deep = [c for c in sub["children"] if c["name"] == "deep"][0]
        assert deep.get("lazy") is True and deep["children"] == []
        # Fetching the deep dir itself yields its file.
        with urlopen(base + "/api/tree?path=" + urllib.parse.quote("docs/sub/deep")) as r:
            d = json.loads(r.read())
        assert "deepfile.md" in json.dumps(d)
    finally:
        server.shutdown()
        server.server_close()


def test_lazy_subtree_rejects_outside_root_and_pruned(deep_tree, monkeypatch):
    """The lazy endpoint 404s for paths outside the root or inside pruned/hidden
    or ignored directories — the same boundary as the read endpoints."""
    monkeypatch.setattr(viewer, "IGNORE_DIRS", frozenset({"skipme"}))
    viewer._reset_tree_cache()
    server, port = _serve(deep_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        for bad in ("../..", "node_modules", "node_modules/pkg", ".git", "skipme", "does/not/exist"):
            try:
                urlopen(base + "/api/tree?path=" + urllib.parse.quote(bad))
                assert False, f"expected 404 for {bad}"
            except urllib.error.HTTPError as e:
                assert e.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_safe_resolve_dir_confinement(deep_tree):
    """_safe_resolve_dir confines to root, requires a real dir, and rejects pruned
    components (including the leaf dir's own name)."""
    assert viewer._safe_resolve_dir("docs") is not None
    assert viewer._safe_resolve_dir("") is not None              # root itself
    assert viewer._safe_resolve_dir("README.md") is None         # a file, not a dir
    assert viewer._safe_resolve_dir("../..") is None             # traversal
    assert viewer._safe_resolve_dir(".git") is None              # hidden/pruned leaf
    assert viewer._safe_resolve_dir("node_modules/pkg") is None  # pruned component


def test_cache_round_trip_writes_and_reuses(deep_tree, monkeypatch):
    """A cached build writes a cache file; a second build reuses unchanged dirs
    (verified by counting how many directories are freshly scanned)."""
    cpath = viewer._cache_path(deep_tree)
    assert cpath is not None
    # First build (cold) → cache file created, every dir scanned.
    scanned = []
    real_scan = viewer._scan_one_dir
    monkeypatch.setattr(viewer, "_scan_one_dir",
                        lambda root, rel: (scanned.append(rel), real_scan(root, rel))[1])
    t1 = viewer._build_tree(deep_tree, use_cache=True)
    assert cpath.is_file()
    cold_scans = list(scanned)
    assert cold_scans, "cold build should scan directories"
    # Second build (warm) → no directory mtimes changed, so nothing is re-scanned.
    scanned.clear()
    t2 = viewer._build_tree(deep_tree, use_cache=True)
    assert scanned == [], "warm build must reuse the cache (no rescans)"
    # Same content both times.
    assert json.dumps(t1, sort_keys=True) == json.dumps(t2, sort_keys=True)


def test_cache_incremental_rescan_on_dir_mtime_change(deep_tree, monkeypatch):
    """Adding a file to ONE directory changes only that directory's mtime, so the
    incremental rescan touches that dir (and its now-stale ancestors via stat) but
    not the unrelated, unchanged directories."""
    import os as _os
    import time as _time
    # Warm the cache.
    viewer._build_tree(deep_tree, use_cache=True)
    # Mutate one directory: add a new md to docs/sub/deep and bump its mtime.
    deep = deep_tree / "docs" / "sub" / "deep"
    (deep / "added.md").write_text("# Added\n\nNew deep file.\n", encoding="utf-8")
    future = _time.time() + 10
    _os.utime(deep, (future, future))
    # Track which dirs get freshly scanned on the next build.
    scanned = []
    real_scan = viewer._scan_one_dir
    monkeypatch.setattr(viewer, "_scan_one_dir",
                        lambda root, rel: (scanned.append(rel), real_scan(root, rel))[1])
    tree = viewer._build_tree(deep_tree, use_cache=True)
    # The changed dir is re-scanned; an unrelated unchanged dir is NOT.
    assert "docs/sub/deep" in scanned
    assert "docs" not in scanned          # docs' own mtime did not change
    # The new file is now present in the rebuilt tree.
    assert "added.md" in json.dumps(tree)


def test_no_cache_does_not_write_cache_file(deep_tree):
    """use_cache=False must not create any cache file."""
    cpath = viewer._cache_path(deep_tree)
    assert cpath is not None and not cpath.exists()
    viewer._build_tree(deep_tree, use_cache=False)
    assert not cpath.exists()


def test_cache_signature_changes_with_view_ext(deep_tree, monkeypatch):
    """The cache file name embeds the scan signature (view_ext + ignore) so a
    config change does not serve a tree built under a different config."""
    p_default = viewer._cache_path(deep_tree)
    monkeypatch.setattr(viewer, "VIEW_EXT", (".md", ".markdown", ".pdf", ".svg", ".txt"))
    p_widened = viewer._cache_path(deep_tree)
    assert p_default != p_widened
    monkeypatch.setattr(viewer, "VIEW_EXT", viewer.DEFAULT_VIEW_EXT)
    monkeypatch.setattr(viewer, "IGNORE_DIRS", frozenset({"foo"}))
    assert viewer._cache_path(deep_tree) != p_default


def test_tree_json_shallow_vs_full(deep_tree):
    """_tree_json() defaults to the shallow tree (deep file absent); full=True
    returns the complete tree (deep file present)."""
    viewer._reset_tree_cache()
    shallow = viewer._tree_json(force=True, full=False)
    assert "deepfile.md" not in shallow
    full = viewer._tree_json(force=True, full=True)
    assert "deepfile.md" in full


def test_api_tree_full_param_over_http(deep_tree):
    """GET /api/tree?full=1 returns the complete tree over HTTP; the plain
    endpoint stays shallow."""
    server, port = _serve(deep_tree)
    try:
        base = f"http://127.0.0.1:{port}"
        with urlopen(base + "/api/tree") as r:
            assert b"deepfile.md" not in r.read()
        with urlopen(base + "/api/tree?full=1") as r:
            assert b"deepfile.md" in r.read()
    finally:
        server.shutdown()
        server.server_close()
