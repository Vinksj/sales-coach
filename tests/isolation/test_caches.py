"""Process-level caches cannot serve one user another user's data, on either backend.

Every functools.lru_cache / functools.cache in salescoach/ is on the allow-list below with what its
key is made of; a new cache fails here until it is listed. Each listed cache is keyed on content, on
file paths plus their stamps, or on a version (never on mtimes alone, never on the acting user),
and none of them reads the acting user, the store or the request, so there is nothing per-user to
leak. intel/methodology._library is additionally keyed on methodology.settings_version(), the hook
for the day the org's settings live in the database (plan, Phase 6) and an mtime would go stale
across processes.
"""
import ast
from pathlib import Path

from salescoach.intel import methodology

ROOT = Path(__file__).resolve().parent.parent.parent / "salescoach"

# (module, function): the parameters the key is made of. "()" = no parameters, a constant.
ALLOWED = {
    ("config", "_load_cached"): ("tracked", "tracked_stamp", "user", "user_stamp"),     # path + stamp, both files
    ("config", "_user_problem"): ("path", "stamp"),
    ("intel.methodology", "_library"): ("stamps",),          # ((path, stamp)..., ("settings", version))
    ("intel.schemas", "strategy_model"): ("keys",),          # the methodology's element keys: content
    ("schemas.analysis", "analysis_model"): ("lenses", "elements"),
    ("setupui.forms", "timezones"): (),                      # the zoneinfo database
    ("store.db", "translate"): ("sql",),                     # the statement text
    ("store.catalog", "_scratch"): (),                       # the tracked schema files
    ("store.catalog", "tables"): (),
    ("store.catalog", "indexes"): (),
    ("store.catalog", "identity_tables"): (),
    ("store.catalog", "always_not_null_columns"): (),
    ("store.catalog", "check_counts"): (),
    ("store.catalog", "foreign_keys"): (),
}
USER_WORDS = ("user_id", "actor", "owner", "session", "request", "conn")   # "user" alone is the user settings FILE


def _caches():
    found = {}
    for path in sorted(ROOT.rglob("*.py")):
        module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        if module.endswith(".__init__"):
            module = module[:-9]
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                if name in ("lru_cache", "cache"):
                    params = tuple(a.arg for a in node.args.args + node.args.kwonlyargs)
                    found[(module, node.name)] = params
    return found


def test_every_cache_is_on_the_allow_list_with_its_key():
    found = _caches()
    unknown = set(found) - set(ALLOWED)
    assert not unknown, f"new caches need a line in tests/isolation/test_caches.py: {sorted(unknown)}"
    gone = set(ALLOWED) - set(found)
    assert not gone, f"allow-listed caches that no longer exist: {sorted(gone)}"
    for key, params in found.items():
        assert params == ALLOWED[key], (key, params)


def test_no_cache_is_keyed_on_the_acting_user():
    for (module, fn), params in _caches().items():
        for p in params:
            assert not any(w in p.lower() for w in USER_WORDS), f"{module}.{fn}({p}) looks per-user"


def test_stamp_keyed_caches_carry_the_path_not_only_the_mtime():
    for (module, fn), params in ALLOWED.items():
        if any("stamp" in p for p in params):
            assert any(p in ("path", "tracked", "user", "stamps") for p in params), (module, fn)


def test_methodology_library_is_keyed_on_paths_stamps_and_a_settings_version(monkeypatch):
    stamps = methodology._stamps()
    assert stamps[-1] == ("settings", methodology.settings_version())
    for path, stamp in stamps[:-1]:
        assert path.endswith(".yaml") and isinstance(stamp, int)
    before = methodology._all()
    monkeypatch.setattr(methodology, "settings_version", lambda: 1)
    after = methodology._all()
    assert methodology._stamps()[-1] == ("settings", 1)
    assert after is not before or methodology._library.cache_info().misses >= 2


def test_methodology_library_does_not_depend_on_the_acting_user(db):
    from salescoach import identity
    keys = []
    for actor in (identity.Actor("u-a"), identity.Actor("u-b")):
        with identity.as_actor(db, actor):
            keys.append(methodology._stamps())
    assert keys[0] == keys[1]
