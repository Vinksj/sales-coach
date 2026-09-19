## What and why

## How it was tested

## Checklist

- [ ] **Every fix has a regression test** that fails without the change.
- [ ] **No test reaches a real model or the network** (no Gmail, calendar, `claude`, recorder API or download). Fakes and `httpx.MockTransport` only.
- [ ] **Nothing sends email without an explicit user action.** No new path to Gmail; auto-send is not easier to switch on.
- [ ] **Numbers shown to users are computed in code**, never taken from a model.
- [ ] Prompts go through `seller.render` with `{{var}}` only, and no tracked prompt or config names a person or a company.
- [ ] No personal or customer data in fixtures, docs or screenshots (invented names, `example.com` / `.test` domains).
- [ ] `pytest -q` and `ruff check .` pass.
- [ ] Docs updated where behaviour changed (`README.md`, `docs/`).
