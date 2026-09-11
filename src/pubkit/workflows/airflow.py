# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Airflow integration.

One task per (document, platform) rather than one task for the whole fan-out.
That is the entire trick: retries, alerting, SLA misses and partial reruns
become Airflow's problem, which it is much better at than a bespoke loop.

    with DAG("blog", schedule="0 9 * * TUE") as dag:
        validate = PubkitValidateOperator(task_id="validate", path="content/series/")
        fanout = PubkitPublishOperator.expand_fanout(
            path="content/series/",
            platforms=["medium", "devto", "x"],
            confirm=True,
        )
        validate >> fanout
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

try:  # pragma: no cover - optional dependency
    from airflow.exceptions import AirflowFailException
    from airflow.models import BaseOperator
except Exception:  # pragma: no cover
    BaseOperator = object  # type: ignore[assignment,misc]

    class AirflowFailException(RuntimeError):  # type: ignore[no-redef]
        pass


class PubkitValidateOperator(BaseOperator):  # type: ignore[misc]
    """Run the check pipeline. Fails the DAG before anything reaches a platform."""

    template_fields = ("path",)

    def __init__(self, path: str, strict: bool = False, **kw: Any) -> None:
        super().__init__(**kw)
        self.path, self.strict = path, strict

    def execute(self, context) -> dict:
        from ..core.checks import run_checks
        from ..core.loader import load_document, load_series

        p = Path(self.path)
        docs = load_series(sorted(p.glob("*.md"))).documents if p.is_dir() else [load_document(p)]
        problems = []
        for doc in docs:
            res = run_checks(doc)
            for f in res.findings:
                self.log.info("%s", f)
            if res.errors or (self.strict and res.findings):
                problems.append(doc.id)
        if problems:
            raise AirflowFailException(f"checks failed for: {', '.join(problems)}")
        return {"documents": [d.id for d in docs]}


class PubkitPublishOperator(BaseOperator):  # type: ignore[misc]
    """Publish one document to one platform.

    Idempotent by construction: the state store means an Airflow retry resumes
    at the failed step rather than starting over, and never creates a second
    draft.
    """

    template_fields = ("path", "platform")

    def __init__(
        self,
        path: str,
        platform: str,
        document_id: str | None = None,
        confirm: bool = False,
        state_path: str = ".pubkit/state.sqlite",
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.path, self.platform = path, platform
        self.document_id, self.confirm, self.state_path = document_id, confirm, state_path

    def execute(self, context) -> dict:
        from ..core.adapter import Context
        from ..core.auth import SessionStore, TokenStore
        from ..core.loader import load_document, load_series
        from ..core.runner import Pipeline
        from ..core.state import StateStore
        from ..registry import build_adapter

        p = Path(self.path)
        docs = load_series(sorted(p.glob("*.md"))).documents if p.is_dir() else [load_document(p)]
        if self.document_id:
            docs = [d for d in docs if d.id == self.document_id]

        tokens, sessions = TokenStore(), SessionStore()
        adapter = build_adapter(self.platform)
        pipe = Pipeline(StateStore(self.state_path))

        def ctx_for(_name: str) -> Context:
            return Context(platform=self.platform, tokens=tokens, sessions=sessions, confirm=self.confirm)

        # A stable run_id per DAG run is what makes an Airflow retry resume
        # instead of restart.
        run_id = f"airflow-{context['run_id']}"
        report = asyncio.run(pipe.run(docs, [adapter], ctx_for, publish=self.confirm, run_id=run_id))
        if not report.ok:
            raise AirflowFailException(report.human())
        return {"legs": [leg.__dict__ for leg in report.legs]}

    @classmethod
    def expand_fanout(cls, path: str, platforms: Sequence[str], **kw: Any) -> list:
        return [cls(task_id=f"publish_{p}", path=path, platform=p, **kw) for p in platforms]
