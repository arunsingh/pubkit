# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Pre-flight checks.

These exist because of a specific, humbling experience: a scripted pass over 32
numeric claims in a finished article found 5 that were wrong, including two
ratios that had been transposed during an edit. Every one of the 5 was real.
Prose does not have a type checker; this is the closest thing.

Checks are pluggable (`pubkit.checks` entry point). Ship your own house-style
rule and it runs in the same pipeline.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from .ir import Callout, Document, Heading, ListBlock, Paragraph, Quote, Table


class Severity(str, Enum):
    ERROR = "error"
    WARN = "warn"
    INFO = "info"


@dataclass
class Finding:
    check: str
    severity: Severity
    message: str
    where: str = ""

    def __str__(self) -> str:
        loc = f" [{self.where}]" if self.where else ""
        return f"{self.severity.value.upper():5} {self.check}{loc}: {self.message}"


@dataclass
class CheckResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: CheckResult) -> None:
        self.findings.extend(other.findings)


class Check(Protocol):
    name: str

    def __call__(self, doc: Document, ctx: dict) -> CheckResult: ...


def _text_blocks(doc: Document) -> Iterable[tuple[str, str]]:
    for i, b in enumerate(doc.blocks):
        if isinstance(b, (Paragraph, Quote, Callout)):
            yield f"block[{i}]", b.text
        elif isinstance(b, Heading):
            yield f"heading[{i}]", b.text
        elif isinstance(b, ListBlock):
            for j, item in enumerate(b.items):
                yield f"block[{i}].item[{j}]", item
        elif isinstance(b, Table):
            for r, row in enumerate(b.rows):
                yield f"block[{i}].row[{r}]", " | ".join(row)


# --------------------------------------------------------------------------
# placeholders (failure C3)
# --------------------------------------------------------------------------
PLACEHOLDER_RE = re.compile(r"(\$\{[^}]+\}|URL-PART-\d+|TODO|TK|FIXME|XXX|\[\[\s*IMAGE)", re.I)
SERIES_TOKEN_RE = re.compile(r"\$\{series\.part(\d+)\.url\}")


def check_placeholders(doc: Document, ctx: dict) -> CheckResult:
    """No placeholder token may reach a published document.

    `URL-PART-2` links once existed in a draft because part 2 had no URL yet —
    the chicken-and-egg every multi-document publish hits. The fix is the
    two-phase publish; this check is the guard rail that proves it ran.
    """
    res = CheckResult()
    allow: set[str] = set(ctx.get("allow_placeholders", ()))
    # `${series.partN.url}` is *expected* to be unresolved before phase 1 of a
    # two-phase publish — the sibling has no URL yet. It is only an error if it
    # names a part that does not exist, or if it is still there at publish time
    # (which is what `post_resolution` mode checks).
    post = bool(ctx.get("post_resolution"))
    for where, text in _text_blocks(doc):
        for m in PLACEHOLDER_RE.finditer(text):
            tok = m.group(0)
            if tok in allow:
                continue
            sm = SERIES_TOKEN_RE.fullmatch(tok)
            if sm and doc.series:
                idx = int(sm.group(1))
                if idx > doc.series.of:
                    res.findings.append(
                        Finding(
                            "placeholders",
                            Severity.ERROR,
                            f"{tok!r} names part {idx} but the series has {doc.series.of}",
                            where,
                        )
                    )
                elif post:
                    res.findings.append(
                        Finding(
                            "placeholders",
                            Severity.ERROR,
                            f"{tok!r} survived link resolution — phase 1 did not record that URL",
                            where,
                        )
                    )
                else:
                    res.findings.append(
                        Finding("placeholders", Severity.INFO, f"{tok} resolves during publish", where)
                    )
                continue
            res.findings.append(
                Finding("placeholders", Severity.ERROR, f"unresolved placeholder {tok!r}", where)
            )
    return res


# --------------------------------------------------------------------------
# numeric consistency (failure C1)
# --------------------------------------------------------------------------
_NUM = r"(?:\d[\d,]*(?:\.\d+)?)"
RATIO_RE = re.compile(rf"({_NUM})\s*(?:×|x|times)\b", re.I)
DEFINE_RE = re.compile(rf"<!--\s*pubkit:define\s+(\w+)\s*=\s*({_NUM})\s*-->")
ASSERT_RE = re.compile(
    rf"<!--\s*pubkit:assert\s+({_NUM})\s*(?:×|x)?\s*=\s*(\w+)\s*/\s*(\w+)\s*(?:±\s*({_NUM})%)?\s*-->"
)


def check_numeric(doc: Document, ctx: dict) -> CheckResult:
    """Re-derive every ratio the author asserted from its declared inputs.

    Authors annotate source values once:

        <!-- pubkit:define nvlink = 900 -->
        <!-- pubkit:define pcie5  = 128 -->

    and then assert derived claims where they appear:

        NVLink is 7× wider than PCIe Gen5. <!-- pubkit:assert 7x = nvlink/pcie5 ±10% -->

    This is what catches a transposition. Two ratios were once swapped in an
    edit — 18× and 7× traded places — and no amount of re-reading found it.
    Arithmetic did, in under a second.
    """
    res = CheckResult()
    defines: dict[str, float] = {}
    raw = "\n".join(t for _, t in _text_blocks(doc))
    for name, val in DEFINE_RE.findall(raw):
        defines[name] = float(val.replace(",", ""))

    for where, text in _text_blocks(doc):
        for claimed, num, den, tol in ASSERT_RE.findall(text):
            if num not in defines or den not in defines:
                res.findings.append(
                    Finding("numeric", Severity.ERROR, f"assert references undefined {num!r}/{den!r}", where)
                )
                continue
            if defines[den] == 0:
                res.findings.append(Finding("numeric", Severity.ERROR, f"{den!r} is zero", where))
                continue
            actual = defines[num] / defines[den]
            want = float(claimed.replace(",", ""))
            tolerance = float(tol) / 100 if tol else 0.10
            if want == 0 or abs(actual - want) / max(abs(want), 1e-9) > tolerance:
                res.findings.append(
                    Finding(
                        "numeric",
                        Severity.ERROR,
                        f"claimed {want:g}× but {num}/{den} = {actual:.3g}× "
                        f"({defines[num]:g}/{defines[den]:g}), outside ±{tolerance:.0%}",
                        where,
                    )
                )
    return res


def check_number_drift(doc: Document, ctx: dict) -> CheckResult:
    """Flag the same quantity stated with two different values.

    One section said 5,000 tok/s and another derived 5,150 from the same
    assumptions; three cost figures downstream inherited the discrepancy.
    """
    res = CheckResult()
    pattern = re.compile(rf"({_NUM})\s*(tok/s|tokens/s|GB/s|TB/s|GB|ms|µs|%|\$/M)", re.I)
    seen: dict[str, set[str]] = {}
    for _where, text in _text_blocks(doc):
        for val, unit in pattern.findall(text):
            key = unit.lower()
            seen.setdefault(key, set()).add(val.replace(",", ""))
    for unit, values in seen.items():
        if len(values) > 6:  # a table of varied figures, not a drift signal
            continue
        floats = sorted(float(v) for v in values)
        for a, b in zip(floats, floats[1:], strict=False):
            if a and abs(b - a) / a < 0.08 and a != b:
                res.findings.append(
                    Finding(
                        "number-drift",
                        Severity.WARN,
                        f"{a:g} and {b:g} {unit} differ by <8% — same quantity stated twice?",
                    )
                )
    return res


# --------------------------------------------------------------------------
# cross-references (failure C2)
# --------------------------------------------------------------------------
XREF_RE = re.compile(r"\b[Pp]art\s+(\d+)\b")


def check_xrefs(doc: Document, ctx: dict) -> CheckResult:
    """"Part N" must resolve, and must not collide with section numbering.

    Splitting one article into three turned seven internal references into
    lies, and made the phrase "Part 1" ambiguous between a series part and a
    section. Sections are numbered by generator now; this check enforces that
    "Part N" only ever means the series.
    """
    res = CheckResult()
    if doc.series is None:
        for where, text in _text_blocks(doc):
            if XREF_RE.search(text):
                res.findings.append(
                    Finding("xref", Severity.WARN, "'Part N' used but document declares no series", where)
                )
        return res

    for where, text in _text_blocks(doc):
        for n in XREF_RE.findall(text):
            idx = int(n)
            if idx < 1 or idx > doc.series.of:
                res.findings.append(
                    Finding(
                        "xref",
                        Severity.ERROR,
                        f"refers to Part {idx} but the series has {doc.series.of} parts",
                        where,
                    )
                )
            elif idx == doc.series.index and "this" not in text.lower():
                res.findings.append(
                    Finding(
                        "xref",
                        Severity.WARN,
                        f"Part {idx} refers to itself — probably a leftover from a split",
                        where,
                    )
                )
    return res


# --------------------------------------------------------------------------
# assets, budget, structure
# --------------------------------------------------------------------------
def check_assets(doc: Document, ctx: dict) -> CheckResult:
    res = CheckResult()
    for i, fig in enumerate(doc.figures):
        asset = doc.assets.get(fig.asset_id)
        if asset is None:
            res.findings.append(
                Finding("assets", Severity.ERROR, f"figure references unknown asset {fig.asset_id!r}", f"figure[{i}]")
            )
            continue
        if not asset.path.exists() and asset.generated_from is None:
            res.findings.append(
                Finding("assets", Severity.ERROR, f"asset file missing: {asset.path}", f"figure[{i}]")
            )
        if not (fig.alt or asset.alt):
            res.findings.append(
                Finding("assets", Severity.WARN, f"no alt text for {fig.asset_id!r}", f"figure[{i}]")
            )
    unused = set(doc.assets) - {f.asset_id for f in doc.figures}
    for a in sorted(unused):
        res.findings.append(Finding("assets", Severity.INFO, f"asset {a!r} declared but never used"))
    return res


def check_budget(doc: Document, ctx: dict) -> CheckResult:
    res = CheckResult()
    if not doc.budget or not doc.budget.words:
        return res
    target, tol = doc.budget.words, doc.budget.tolerance
    actual = doc.word_count
    if abs(actual - target) / target > tol:
        res.findings.append(
            Finding(
                "budget",
                Severity.WARN,
                f"{actual:,} words against a target of {target:,} (±{tol:.0%})",
            )
        )
    return res


def check_structure(doc: Document, ctx: dict) -> CheckResult:
    res = CheckResult()
    if not doc.title.strip():
        res.findings.append(Finding("structure", Severity.ERROR, "empty title"))
    levels = [b.level for b in doc.blocks if isinstance(b, Heading)]
    for a, b in zip(levels, levels[1:], strict=False):
        if b > a + 1:
            res.findings.append(
                Finding("structure", Severity.WARN, f"heading level jumps h{a} → h{b}")
            )
            break
    return res


DEFAULT_CHECKS: list[tuple[str, Callable[[Document, dict], CheckResult]]] = [
    ("structure", check_structure),
    ("placeholders", check_placeholders),
    ("numeric", check_numeric),
    ("number-drift", check_number_drift),
    ("xref", check_xrefs),
    ("assets", check_assets),
    ("budget", check_budget),
]


def run_checks(doc: Document, ctx: dict | None = None, extra: list | None = None) -> CheckResult:
    ctx = ctx or {}
    out = CheckResult()
    for _name, fn in DEFAULT_CHECKS + list(extra or []):
        out.extend(fn(doc, ctx))
    return out
