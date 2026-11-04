"""Module discovery: scan ``modules/<id>/``, validate the manifest, import ``module.py``
(docs/MODULES.md §3, §14, §15).

A module folder is not an importable package; ``module.py`` is loaded from its path under
the synthetic name ``beepboop_modules.<id>.module`` so relative imports inside the folder
work and two modules never collide.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from beepboop.module.base import Module
from beepboop.registry.manifest import Manifest, ManifestError, load_manifest

MODULE_FILE = "module.py"
REQUIRED_FILES: tuple[str, ...] = (
    "manifest.yaml",
    MODULE_FILE,
    "AGENT.md",
    "SETUP.md",
    "evals.yaml",
)
PACKAGE_PREFIX = "beepboop_modules"


class ModuleLoadError(RuntimeError):
    """``module.py`` could not be imported or has no ``Module`` class."""


@dataclass(frozen=True, slots=True)
class DiscoveredModule:
    """One folder that passed manifest validation; ``error`` set if import failed later."""

    module_dir: Path
    manifest: Manifest
    module_class: type[Module] | None = None
    error: str | None = None

    @property
    def id(self) -> str:
        return self.manifest.id


def missing_files(module_dir: Path) -> list[str]:
    return [name for name in REQUIRED_FILES if not (module_dir / name).exists()]


def import_module_file(module_dir: Path, *, name: str | None = None) -> type[Module]:
    """Import ``<module_dir>/module.py`` and return its ``Module`` subclass."""
    path = module_dir / MODULE_FILE
    if not path.exists():
        raise ModuleLoadError(f"{path} does not exist")
    module_id = name or module_dir.name.replace("-", "_")
    pkg_name = f"{PACKAGE_PREFIX}.{module_id}"
    _ensure_namespace_package(PACKAGE_PREFIX, None)
    _ensure_namespace_package(pkg_name, module_dir)
    full_name = f"{pkg_name}.module"
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ModuleLoadError(f"cannot build an import spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        sys.modules.pop(full_name, None)
        raise ModuleLoadError(f"{path}: {type(exc).__name__}: {exc}") from exc
    klass = getattr(mod, "Module", None)
    if not (isinstance(klass, type) and issubclass(klass, Module)):
        raise ModuleLoadError(f"{path} must define `class Module(beepboop.module.Module)`")
    return klass


def _ensure_namespace_package(name: str, location: Path | None) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    pkg = ModuleType(name)
    pkg.__path__ = [str(location)] if location is not None else []
    sys.modules[name] = pkg
    return pkg


def discover(
    modules_root: Path, *, enabled: frozenset[str] | None = None
) -> list[DiscoveredModule]:
    """Scan ``modules_root`` (one folder per module). Folders that fail validation are
    returned with ``error`` set so the UI can show why; the rest keep running (§3)."""
    found: list[DiscoveredModule] = []
    if not modules_root.exists():
        return found
    for module_dir in sorted(p for p in modules_root.iterdir() if p.is_dir()):
        if module_dir.name.startswith((".", "_")):
            continue
        if enabled is not None and module_dir.name not in enabled:
            continue
        try:
            manifest = load_manifest(module_dir)
        except ManifestError as exc:
            # No manifest means no module; report it only when it looks like one.
            if (module_dir / MODULE_FILE).exists() or (module_dir / "manifest.yaml").exists():
                found.append(_broken(module_dir, str(exc)))
            continue
        missing = missing_files(module_dir)
        if missing:
            found.append(DiscoveredModule(module_dir, manifest, error=f"missing {missing}"))
            continue
        try:
            klass = import_module_file(module_dir, name=manifest.id)
        except ModuleLoadError as exc:
            found.append(DiscoveredModule(module_dir, manifest, error=str(exc)))
            continue
        found.append(DiscoveredModule(module_dir, manifest, klass))
    return found


def _broken(module_dir: Path, error: str) -> DiscoveredModule:
    placeholder = (
        Manifest(
            id=module_dir.name if _looks_like_id(module_dir.name) else "broken_module",
            api=1,
            version="0.0.0",
            summary="Failed to load.",
            scope="household",
            category="system",
            prompt_block=None,
            tools=[],
        )
        if False
        else _placeholder(module_dir)
    )  # pragma: no cover - see _placeholder
    return DiscoveredModule(module_dir, placeholder, error=error)


def _looks_like_id(name: str) -> bool:
    from beepboop.registry.manifest import MODULE_ID

    return MODULE_ID.match(name) is not None


def _placeholder(module_dir: Path) -> Manifest:
    """A minimal valid manifest standing in for one that failed to load."""
    return Manifest.model_validate(
        {
            "id": module_dir.name if _looks_like_id(module_dir.name) else "broken_module",
            "api": 1,
            "version": "0.0.0",
            "summary": "Failed to load.",
            "scope": "household",
            "category": "system",
            "tools": [
                {
                    "name": "broken.noop",
                    "description": "Placeholder for a module that failed to load.",
                    "risk": "read",
                    "untrusted_output": False,
                    "args": {"type": "object", "properties": {}, "additionalProperties": False},
                    "output": {"type": "object", "properties": {"speak": {"type": "string"}}},
                }
            ],
        }
    )


__all__ = [
    "MODULE_FILE",
    "PACKAGE_PREFIX",
    "REQUIRED_FILES",
    "DiscoveredModule",
    "ModuleLoadError",
    "discover",
    "import_module_file",
    "missing_files",
]
