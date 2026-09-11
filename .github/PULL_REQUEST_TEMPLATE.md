## What and why

<!-- One paragraph. If this fixes a publishing failure, describe the failure, not the diff. -->

## If this touches an adapter

- [ ] `content_roots` selects **all** content roots, not just the first
- [ ] No `execCommand('delete')` across the whole document
- [ ] Images go through the platform's own upload path, never embedded in HTML
- [ ] Anchors are re-resolved after every mutation
- [ ] `verify()` reloads before comparing
- [ ] `publish()` calls `self.guard_publish(ctx)` first
- [ ] Every step is safe to re-enter

## If this fixes a bug

- [ ] Added a row to `docs/FAILURE-MODES.md`
- [ ] Added a test **named after the failure**

That second pair is the project's one hard rule. The code is replaceable; the
catalogue of ways publishing quietly goes wrong is the actual asset.

## Checks

- [ ] `pytest` passes
- [ ] `ruff check src tests` is clean
