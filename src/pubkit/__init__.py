# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""pubkit — publish one source to many platforms, safely."""
from .core.ir import Asset, Code, Document, Figure, Heading, Paragraph, Series, Table  # noqa: F401
from .core.loader import load_document, load_series  # noqa: F401
from .core.runner import Pipeline, RunReport  # noqa: F401
from .registry import build_adapter, list_adapters, register  # noqa: F401

__version__ = "0.2.0"
__all__ = [
    "Document", "Series", "Asset", "Figure", "Table", "Heading", "Paragraph", "Code",
    "Pipeline", "RunReport", "load_document", "load_series",
    "build_adapter", "register", "list_adapters", "__version__",
]
