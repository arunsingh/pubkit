# Contributing

## The rule that matters

**Every bug fix in an adapter adds a row to `docs/FAILURE-MODES.md` and a test
named after it.** This project's value is not the code — it is the catalogue of
ways publishing automation quietly goes wrong. A fix without a documented
failure mode is a fix the next person will have to rediscover.

## Setup

```bash
git clone https://github.com/arunsingh/pubkit && cd pubkit
pip install -e ".[all,dev]"
playwright install chromium
pytest && ruff check src tests
```

## Adding an adapter

1. Subclass `BrowserAdapter` or `ApiAdapter`.
2. Fill in `capabilities` **honestly**. An overstated capability becomes a
   silent degradation at publish time, which is the failure mode this whole
   design exists to eliminate.
3. Implement the six methods. `verify()` is not optional: it must read state
   back from the remote after a reload, not report what you just sent.
4. Every step must be safe to re-enter. Assume the connection drops mid-run,
   because it will.
5. Register via the `pubkit.adapters` entry point.

## Adapter checklist

- [ ] `content_roots` selects **all** content roots, not just the first
- [ ] no `execCommand('delete')` across the whole document
- [ ] images inserted via the platform's own upload path, never embedded in HTML
- [ ] anchors re-resolved after every mutation
- [ ] `verify()` reloads before comparing
- [ ] `publish()` calls `self.guard_publish(ctx)` first
- [ ] rate limit and retry behaviour matches the platform's documented limits
- [ ] a test for each failure mode you hit while writing it

## Style

Ruff for lint and imports. Comments explain *why*, especially where the code
looks odd — odd code here is usually load-bearing, and the comment is the only
thing stopping someone "simplifying" it back into a bug.
