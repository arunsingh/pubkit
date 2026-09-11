# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""The runner: idempotent, resumable, gated, fan-out-safe."""
from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .adapter import Adapter, Context, PublishedRef, VerificationFailed, with_retry
from .assets import materialise
from .auth import CredentialError
from .capabilities import PublishPlan
from .capabilities import plan as make_plan
from .checks import CheckResult, run_checks
from .ir import Document, Series
from .state import StateStore, Status, Step

log = logging.getLogger(__name__)

HOOKS = ("pre_check", "post_plan", "pre_publish", "post_publish", "on_error")


@dataclass
class LegResult:
    document_id: str
    platform: str
    status: str
    url: str | None = None
    error: str | None = None
    resumed_from: str | None = None


@dataclass
class RunReport:
    run_id: str
    legs: list[LegResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(leg.status in ("published", "skipped", "drafted") for leg in self.legs)

    def human(self) -> str:
        rows = []
        for leg in self.legs:
            mark = {"published": "✓", "drafted": "◔", "skipped": "–", "failed": "✗"}.get(leg.status, "?")
            tail = leg.url or leg.error or ""
            resumed = f" (resumed at {leg.resumed_from})" if leg.resumed_from else ""
            rows.append(f"  {mark} {leg.platform:<10} {leg.document_id:<28} {tail}{resumed}")
        return "\n".join(rows)


class Pipeline:
    """Load → check → plan → publish, with every step resumable.

    The shape is dictated by two things that happen constantly in practice: the
    channel drops mid-run, and you re-run the same command afterwards. If a
    re-run is not safe, nobody will re-run it, and they will finish the job by
    hand at 1am.
    """

    def __init__(
        self,
        state: StateStore | None = None,
        hooks: dict[str, list[Callable]] | None = None,
    ) -> None:
        self.state = state or StateStore()
        self.hooks: dict[str, list[Callable]] = {h: [] for h in HOOKS}
        for name, fns in (hooks or {}).items():
            self.hooks.setdefault(name, []).extend(fns)

    def on(self, event: str, fn: Callable) -> None:
        self.hooks.setdefault(event, []).append(fn)

    def _fire(self, event: str, **kw) -> None:
        for fn in self.hooks.get(event, []):
            try:
                result = fn(**kw)
                if event == "pre_publish" and result is False:
                    raise PermissionError("a pre_publish hook vetoed this publish")
            except PermissionError:
                raise
            except Exception:  # noqa: BLE001 - a bad hook must not break a run
                log.exception("hook %s failed", event)

    # ------------------------------------------------------------------ plan
    def check(self, doc: Document, ctx: dict | None = None) -> CheckResult:
        self._fire("pre_check", doc=doc)
        return run_checks(doc, ctx)

    def plan(self, docs: Sequence[Document], adapters: Sequence[Adapter]) -> list[PublishPlan]:
        plans = [make_plan(d, a.name, a.capabilities, a) for d in docs for a in adapters]
        self._fire("post_plan", plans=plans)
        return plans

    @staticmethod
    def plan_hash(plans: Sequence[PublishPlan]) -> str:
        """Pin the plan to its content.

        `--confirm` approves a *specific* plan. If the content changes between
        planning and applying, the hash changes and the run stops — which is the
        difference between reviewing what ships and reviewing something else
        (failure D3).
        """
        payload = "|".join(f"{p.platform}:{p.document_id}:{p.content_id}" for p in sorted(
            plans, key=lambda x: (x.platform, x.document_id)
        ))
        return hashlib.blake2b(payload.encode(), digest_size=12).hexdigest()

    # --------------------------------------------------------------- publish
    async def run_leg(
        self,
        doc: Document,
        adapter: Adapter,
        plan_: PublishPlan,
        ctx: Context,
        run_id: str,
        *,
        publish: bool,
    ) -> LegResult:
        pf, did = adapter.name, doc.id
        resume = self.state.resume_point(run_id, did, pf)

        if not self.state.needs_update(did, pf, doc.content_id) and not publish:
            log.info("%s/%s: content unchanged, nothing to do", pf, did)
            rec = self.state.remote(did, pf)
            return LegResult(did, pf, "skipped", url=rec.url if rec else None)

        rec = self.state.remote(did, pf)
        if rec:
            ctx.options["remote_ref"] = rec.remote_ref

        # Adapt the document to this platform: on Medium a table becomes a
        # rendered figure, on Dev.to it stays a table. The source IR is never
        # mutated — the same document has to serve every leg of this run.
        doc = materialise(doc, adapter.capabilities, Path(ctx.workdir))

        try:
            order = Step.order()
            start = order.index(resume)

            for step in order[start:]:
                if step is Step.PUBLISH and not publish:
                    break
                self.state.record_step(run_id, did, pf, step, Status.RUNNING)

                if step is Step.AUTH:
                    await with_retry(lambda: adapter.authenticate(ctx))

                elif step is Step.DRAFT:
                    ref = await with_retry(lambda: adapter.ensure_draft(doc, ctx))
                    ctx.options["remote_ref"] = ref.id
                    self._ref = ref
                    self.state.remember_remote(did, pf, doc.content_id, ref.id, ref.url)

                elif step is Step.CONTENT:
                    await with_retry(lambda: adapter.push_content(doc, plan_, self._ref, ctx))

                elif step is Step.MEDIA:
                    await with_retry(lambda: adapter.push_media(doc, plan_, self._ref, ctx))

                elif step is Step.VERIFY:
                    fp = await adapter.verify(doc, plan_, self._ref, ctx)
                    self.state.record_step(
                        run_id, did, pf, step, Status.DONE, content_id=doc.content_id, fingerprint=fp.__dict__
                    )
                    continue

                elif step is Step.PUBLISH:
                    self._fire("pre_publish", doc=doc, platform=pf, plan=plan_)
                    pub: PublishedRef = await adapter.publish(doc, self._ref, ctx)
                    self.state.remember_remote(did, pf, doc.content_id, pub.id, pub.url, published=True)
                    self.state.record_step(run_id, did, pf, step, Status.DONE, remote_ref=pub.id)
                    self._fire("post_publish", doc=doc, platform=pf, url=pub.url)
                    return LegResult(did, pf, "published", url=pub.url,
                                     resumed_from=resume.value if start else None)

                self.state.record_step(run_id, did, pf, step, Status.DONE, content_id=doc.content_id)

            rec = self.state.remote(did, pf)
            return LegResult(did, pf, "drafted", url=rec.url if rec else None,
                             resumed_from=resume.value if start else None)

        except VerificationFailed as exc:
            # The most important failure to surface loudly: the remote does not
            # contain what we think it does. Never proceed to publish.
            self.state.record_step(run_id, did, pf, Step.VERIFY, Status.FAILED, error=str(exc))
            self._fire("on_error", doc=doc, platform=pf, error=exc)
            log.error("%s/%s: verification failed — NOT publishing: %s", pf, did, exc)
            return LegResult(did, pf, "failed", error=f"verification: {exc}")

        except (CredentialError, PermissionError) as exc:
            # Expected, actionable, and not worth a stack trace: the message
            # already tells the user exactly what to run.
            self.state.record_step(run_id, did, pf, resume, Status.FAILED, error=str(exc))
            self._fire("on_error", doc=doc, platform=pf, error=exc)
            log.error("%s/%s: %s", pf, did, exc)
            return LegResult(did, pf, "failed", error=str(exc))

        except Exception as exc:  # noqa: BLE001
            self.state.record_step(run_id, did, pf, resume, Status.FAILED, error=str(exc))
            self._fire("on_error", doc=doc, platform=pf, error=exc)
            log.exception("%s/%s failed", pf, did)
            return LegResult(did, pf, "failed", error=str(exc))

    async def run(
        self,
        docs: Sequence[Document],
        adapters: Sequence[Adapter],
        ctx_for: Callable[[str], Context],
        *,
        publish: bool = False,
        run_id: str | None = None,
    ) -> RunReport:
        plans = self.plan(docs, adapters)
        blocking = [p for p in plans if not p.ok]
        if blocking:
            raise RuntimeError(
                "plan is blocked:\n" + "\n".join(p.human() for p in blocking)
            )

        run_id = run_id or f"run-{int(time.time())}"
        self.state.start_run(run_id, self.plan_hash(plans))
        report = RunReport(run_id=run_id)

        by_key = {(p.document_id, p.platform): p for p in plans}
        # Sequential on purpose. Browser adapters share one browser, and
        # hammering five platforms at once turns one rate-limit into five.
        for doc in docs:
            for adapter in adapters:
                ctx = ctx_for(adapter.name)
                leg = await self.run_leg(
                    doc, adapter, by_key[(doc.id, adapter.name)], ctx, run_id, publish=publish
                )
                report.legs.append(leg)

        self.state.finish_run(run_id, Status.DONE if report.ok else Status.FAILED)
        return report

    async def run_series(
        self,
        series: Series,
        adapters: Sequence[Adapter],
        ctx_for: Callable[[str], Context],
        *,
        publish: bool = False,
    ) -> RunReport:
        """Two-phase publish, the only correct way to ship mutually-linked docs.

        Phase 1 creates every draft and collects permalinks; phase 2 substitutes
        them into the cross-links and pushes final content. Doing it in one pass
        means the first document links to a URL that does not exist yet.
        """
        from .loader import resolve_series_links

        log.info("phase 1/2: creating drafts to collect permalinks")
        phase1 = await self.run(series.documents, adapters, ctx_for, publish=False)

        for leg in phase1.legs:
            if leg.url:
                doc = next(d for d in series.documents if d.id == leg.document_id)
                if doc.series:
                    for other in series.documents:
                        if other.series:
                            other.series.sibling_urls[doc.series.index] = leg.url

        resolve_series_links(series)

        log.info("phase 2/2: pushing final content with resolved cross-links")
        phase2 = await self.run(series.documents, adapters, ctx_for, publish=publish)
        phase2.legs = phase2.legs or phase1.legs
        return phase2
