"""Phase plugins: how live coaching, sales intelligence and agentic execution
attach to the core without editing it.

Each module in this package may define any of:
  NAV: list[tuple[str, str]]         extra top-nav entries (href, label)
  router: fastapi.APIRouter          web routes
  register(workflow)                 pipeline steps and event handlers, via
                                     workflow.register_step / register_handler
  register_cli(subparsers)           CLI subcommands (each sets fn=...)
  start_background(db_path, stop)    duties for `salescoach serve`; start threads,
                                     return quickly, honour the stop Event
Schema lives next to the module as <module>.sql (CREATE ... IF NOT EXISTS only);
stores.sales() applies every plugins/*.sql on connect, without importing Python.

Modules load in name order. One that fails to import is skipped and recorded in
`errors`; a broken plugin never takes the core down.
"""
import importlib
import logging
import os
import pkgutil

log = logging.getLogger("salescoach.plugins")
errors: dict[str, str] = {}
_loaded = None


def disabled() -> bool:
    """SALESCOACH_NO_PLUGINS=1 runs the bare core (backfills while plugins are being built)."""
    return os.environ.get("SALESCOACH_NO_PLUGINS") == "1"


def modules() -> list:
    global _loaded
    if _loaded is None:
        _loaded = []
        if disabled():
            return _loaded
        for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
            if info.name.startswith("_"):
                continue
            try:
                _loaded.append(importlib.import_module(f"{__name__}.{info.name}"))
            except Exception as exc:                       # pragma: no cover - reported, not fatal
                errors[info.name] = f"{type(exc).__name__}: {exc}"
                log.exception("plugin %s failed to load", info.name)
    return _loaded


def reset(fake_modules=None):
    """Tests: replace the loaded set."""
    global _loaded
    _loaded = fake_modules
    errors.clear()


def nav() -> list:
    return [tuple(entry) for m in modules() for entry in getattr(m, "NAV", [])]


def routers() -> list:
    return [m.router for m in modules() if getattr(m, "router", None) is not None]


def apply_workflow(workflow) -> None:
    for m in modules():
        if hasattr(m, "register"):
            try:
                m.register(workflow)
            except Exception as exc:
                errors[m.__name__.rsplit(".", 1)[-1]] = f"register failed: {type(exc).__name__}: {exc}"
                log.exception("plugin %s failed to register", m.__name__)


def register_cli(subparsers) -> None:
    for m in modules():
        if hasattr(m, "register_cli"):
            m.register_cli(subparsers)


def start_background(db_path, stop) -> None:
    for m in modules():
        if hasattr(m, "start_background"):
            try:
                m.start_background(db_path, stop)
            except Exception:
                log.exception("plugin %s background start failed", m.__name__)
