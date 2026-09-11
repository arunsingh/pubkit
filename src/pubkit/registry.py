# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Adapter registry and plugin discovery.

Built-ins are registered here; third-party adapters are discovered through the
`pubkit.adapters` entry point, so adding a platform never requires touching
this repository.

    # in your own package's pyproject.toml
    [project.entry-points."pubkit.adapters"]
    ghost = "my_pubkit_ghost:GhostAdapter"
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import entry_points

log = logging.getLogger(__name__)

_BUILTIN: dict[str, Callable[..., object]] = {}


def register(name: str, factory: Callable[..., object]) -> None:
    _BUILTIN[name] = factory


def _load_builtins() -> None:
    if _BUILTIN:
        return
    from .adapters.devto import DevToAdapter, HashnodeAdapter
    from .adapters.medium import MediumAdapter
    from .adapters.substack import SubstackAdapter
    from .adapters.x import XAdapter

    register("medium", MediumAdapter)
    register("substack", SubstackAdapter)
    register("x", XAdapter)
    register("twitter", XAdapter)
    register("devto", DevToAdapter)
    register("hashnode", HashnodeAdapter)


def _plugins() -> dict[str, Callable[..., object]]:
    found: dict[str, Callable[..., object]] = {}
    try:
        eps = entry_points(group="pubkit.adapters")
    except TypeError:  # pragma: no cover - older importlib
        eps = entry_points().get("pubkit.adapters", [])  # type: ignore[assignment]
    for ep in eps:
        try:
            found[ep.name] = ep.load()
        except Exception:  # noqa: BLE001
            log.exception("could not load adapter plugin %s", ep.name)
    return found


def build_adapter(name: str, **kwargs):
    _load_builtins()
    factories = {**_BUILTIN, **_plugins()}
    if name not in factories:
        raise KeyError(f"unknown platform {name!r}; available: {', '.join(sorted(factories))}")
    factory = factories[name]
    if name == "substack" and "publication" not in kwargs:
        import os

        kwargs["publication"] = os.environ.get("PUBKIT_SUBSTACK_URL", "https://example.substack.com")
    return factory(**kwargs)


def list_adapters() -> list[tuple[str, object]]:
    _load_builtins()
    out = []
    for name in sorted({**_BUILTIN, **_plugins()}):
        if name == "twitter":
            continue
        try:
            out.append((name, build_adapter(name)))
        except Exception:  # noqa: BLE001
            continue
    return out
