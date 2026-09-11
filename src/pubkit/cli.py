# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""pubkit CLI."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table as RichTable

from . import __version__
from .core.adapter import Context
from .core.auth import SessionStore, TokenStore
from .core.capabilities import plan as make_plan
from .core.checks import run_checks
from .core.loader import load_document, load_series
from .core.runner import Pipeline
from .core.state import StateStore
from .registry import build_adapter, list_adapters

app = typer.Typer(add_completion=False, help="Publish one source to many platforms, safely.")
auth_app = typer.Typer(help="Credentials. pubkit never accepts a password.")
app.add_typer(auth_app, name="auth")
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        Console().print(f"pubkit {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        None, "--version", "-V", callback=_version_callback, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    """Publish one source to many platforms, safely."""


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _docs(path: Path):
    if path.is_dir():
        files = sorted(path.glob("*.md"))
        if not files:
            raise typer.BadParameter(f"no .md files in {path}")
        return load_series(files)
    return load_document(path)


# ---------------------------------------------------------------- validate
@app.command()
def validate(
    path: Path = typer.Argument(..., help="A .md file, or a directory for a series"),
    strict: bool = typer.Option(False, help="Treat warnings as errors"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Run the check pipeline. No network, no browser, no side effects."""
    _setup_logging(verbose)
    target = _docs(path)
    documents = target.documents if hasattr(target, "documents") else [target]

    failed = False
    for doc in documents:
        res = run_checks(doc)
        console.print(f"\n[bold]{doc.id}[/] — {doc.word_count:,} words, "
                      f"{len(doc.figures)} figures, {len(doc.tables)} tables")
        if not res.findings:
            console.print("  [green]all checks passed[/]")
        for f in res.findings:
            colour = {"error": "red", "warn": "yellow", "info": "dim"}[f.severity.value]
            console.print(f"  [{colour}]{f}[/]")
        if res.errors or (strict and res.findings):
            failed = True

    raise typer.Exit(1 if failed else 0)


# -------------------------------------------------------------------- plan
@app.command()
def plan(
    path: Path = typer.Argument(...),
    platforms: str = typer.Option(..., "--to", help="Comma-separated: medium,x,substack,devto"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Show exactly what each platform will get, including every degradation."""
    _setup_logging(verbose)
    target = _docs(path)
    documents = target.documents if hasattr(target, "documents") else [target]
    names = [p.strip() for p in platforms.split(",") if p.strip()]

    for doc in documents:
        for name in names:
            adapter = build_adapter(name)
            p = make_plan(doc, name, adapter.capabilities, adapter)
            console.print()
            console.print(p.human())

    console.print("\n[dim]Nothing has been sent. Use `pubkit publish` to apply.[/]")


# ----------------------------------------------------------------- publish
@app.command()
def publish(
    path: Path = typer.Argument(...),
    platforms: str = typer.Option(..., "--to"),
    confirm: bool = typer.Option(False, "--confirm", help="Actually publish (not just draft)"),
    draft_only: bool = typer.Option(False, "--draft-only", help="Create/update drafts and stop"),
    headless: bool = typer.Option(True, help="Browser adapters run headless"),
    email_subscribers: bool = typer.Option(False, help="Substack: send the email too"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Create or update drafts and, with --confirm, publish.

    Safe to re-run. A dropped connection costs you one step, not one run.
    """
    _setup_logging(verbose)
    confirm = confirm or os.environ.get("PUBKIT_CONFIRM") == "1"
    target = _docs(path)
    names = [p.strip() for p in platforms.split(",") if p.strip()]

    tokens, sessions = TokenStore(), SessionStore()
    adapters = [build_adapter(n) for n in names]

    def ctx_for(name: str) -> Context:
        return Context(
            platform=name,
            tokens=tokens,
            sessions=sessions,
            confirm=confirm and not draft_only,
            headless=headless,
            options={"email_subscribers": email_subscribers},
        )

    pipe = Pipeline(StateStore())
    live = confirm and not draft_only

    async def go():
        # Browser adapters get a live page here and nowhere else. API-only runs
        # never launch Chromium.
        from .browserctl import attached

        async with attached(adapters, sessions, headless=headless) as ready:
            if hasattr(target, "documents"):
                return await pipe.run_series(target, ready, ctx_for, publish=live)
            return await pipe.run([target], ready, ctx_for, publish=live)

    report = asyncio.run(go())
    console.print(f"\n[bold]{report.run_id}[/]")
    console.print(report.human())
    if not confirm and not draft_only:
        console.print("\n[yellow]Drafts only — nothing is public. Re-run with --confirm to publish.[/]")
    raise typer.Exit(0 if report.ok else 1)


# ------------------------------------------------------------------ status
@app.command()
def status(as_json: bool = typer.Option(False, "--json")):
    """What exists where, and whether it is published."""
    store = StateStore()
    rows = store._conn.execute(
        "SELECT document_id, platform, url, published, updated_at FROM remotes ORDER BY document_id"
    ).fetchall()
    if as_json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return
    t = RichTable("document", "platform", "state", "url")
    for r in rows:
        t.add_row(
            r["document_id"],
            r["platform"],
            "published" if r["published"] else "draft",
            r["url"] or "",
        )
    console.print(t)


@app.command()
def platforms():
    """List available adapters and what they can do."""
    t = RichTable("platform", "kind", "tables", "images", "tags", "threads")
    for name, adapter in list_adapters():
        c = adapter.capabilities
        t.add_row(
            name,
            "browser" if c.image_upload == "browser_paste" else "api",
            "yes" if c.tables else "no → images",
            c.image_upload,
            f"{c.tags.max_count}×{c.tags.max_len}" if c.tags else "—",
            "yes" if c.threads else "—",
        )
    console.print(t)


# -------------------------------------------------------------------- init
@app.command()
def init(
    path: Path = typer.Argument(Path("."), help="Where to scaffold"),
    series: bool = typer.Option(False, help="Include a two-part series example"),
    force: bool = typer.Option(False, help="Overwrite existing files"),
):
    """Scaffold a content repo that validates on the first try."""
    from .scaffold import init_repo

    written = init_repo(path, series=series, force=force)
    if not written:
        console.print("[yellow]nothing written — files already exist (use --force)[/]")
        raise typer.Exit(1)
    for p in written:
        console.print(f"  [green]+[/] {p}")
    console.print(
        f"\nNext:\n"
        f"  pubkit validate {path / 'content'}\n"
        f"  pubkit plan {path / 'content'} --to medium,devto\n"
        f"  pubkit auth login devto"
    )


@app.command()
def doctor():
    """Diagnose the environment. Run this before opening an issue."""
    from .scaffold import diagnose

    findings = diagnose()
    t = RichTable("", "check", "state")
    problems = []
    for f in findings:
        t.add_row("[green]✓[/]" if f.ok else "[red]✗[/]", f.label, f.detail)
        if not f.ok and f.fix:
            problems.append((f.label, f.fix))
    console.print(t)
    for label, fix in problems:
        console.print(f"[yellow]{label}[/]: " + fix.replace("[", "\\["))
    console.print(f"\npubkit {__version__}")


# -------------------------------------------------------------------- auth
@auth_app.command("login")
def auth_login(
    platform: str = typer.Argument(...),
    token: str = typer.Option(None, "--token", help="API platforms only. Prefer stdin."),
):
    """Authenticate.

    API platforms: paste a token (it goes to your OS keychain).
    Browser platforms: a real browser window opens and you sign in yourself —
    pubkit stores only the resulting session, never a password.
    """
    from .browserctl import BROWSER_PLATFORMS

    tokens = TokenStore()
    build_adapter(platform)  # fail fast on an unknown platform

    if platform not in BROWSER_PLATFORMS:
        if token is None:
            token = typer.prompt(f"{platform} API token", hide_input=True)
        tokens.set(platform, token.strip())
        console.print(f"[green]stored {platform} token in the system keychain[/]")
        return

    from .browserctl import interactive_login

    try:
        asyncio.run(interactive_login(platform, SessionStore()))
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    console.print(
        f"[green]saved {platform} session (encrypted)[/]\n"
        f"Verify any time with: pubkit auth verify {platform}"
    )


@auth_app.command("logout")
def auth_logout(platform: str = typer.Argument(...)):
    TokenStore().delete(platform)
    SessionStore().forget(platform)
    console.print(f"[green]forgot all {platform} credentials[/]")


@auth_app.command("verify")
def auth_verify(platform: str = typer.Argument(...)):
    """Check a saved session is still valid — cheaper now than mid-publish."""
    from .browserctl import BROWSER_PLATFORMS, verify_session

    if platform not in BROWSER_PLATFORMS:
        tok = TokenStore().get(platform) or TokenStore().get(platform, "bearer")
        console.print(f"[green]{platform}: token present[/]" if tok else f"[red]{platform}: no token[/]")
        raise typer.Exit(0 if tok else 1)

    ok = asyncio.run(verify_session(platform, SessionStore()))
    if ok:
        console.print(f"[green]{platform}: session valid[/]")
    else:
        console.print(f"[red]{platform}: session missing or expired[/] — run `pubkit auth login {platform}`")
    raise typer.Exit(0 if ok else 1)


@auth_app.command("list")
def auth_list():
    tokens, sessions = TokenStore(), SessionStore()
    t = RichTable("platform", "token", "session")
    for name, _ in list_adapters():
        t.add_row(
            name,
            "yes" if tokens.get(name) else "—",
            "yes" if sessions.exists(name) else "—",
        )
    console.print(t)


def main() -> None:  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted — re-run the same command to resume[/]")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
