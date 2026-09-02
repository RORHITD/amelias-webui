# Tracking upstream

This repo is a fork of [`nesquena/hermes-webui`](https://github.com/nesquena/hermes-webui)
(MIT), and it runs on top of [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent)
(MIT), which we install rather than fork. Both move fast — the WebUI shipped 49
commits in the fortnight this document was written, and the engine ships on the
order of 5,000 commits per minor release.

Staying close to them is the whole strategy. Almost everything in the harness
arrives for free if we can keep merging; it becomes an expensive rewrite the
moment we cannot.

## The parent that was cut, and why it must never be cut again

This fork began as commit `9e5b7f8e`, *"Snapshot mirror of nesquena/hermes-webui
@ 63a562f6 (MIT)"* — a squashed copy of upstream's tree with **no parent
commit**.

That single missing pointer cost more than anything else in the repo. With no
common ancestor, `git merge-base ours origin/master` exits 1, and `git merge`
and `git rebase` against upstream are simply not possible. Forty-nine upstream
commits — nineteen of them bug fixes, four performance — were unreachable
except by reading each diff and retyping it by hand. Predictably, nobody did,
and the fork drifted.

It was repaired on 2026-09-01 by reattaching the real parent:

```bash
git replace --graft 9e5b7f8e 63a562f6
```

This is not a guess. The snapshot commit's tree hash is `2e758dfb…`, and
upstream's tree at `63a562f6` is *the same object*. The graft asserts a
relationship that was already true and had merely been discarded.

**You do not need that graft today.** It unlocked the first merge; the merge
commit that followed records both parents, so the ancestry is now written into
ordinary history:

```
$ git --no-replace-objects merge-base HEAD origin/master
e168b67e4278df618d1cab61fdb3a8dc55b29a81
```

Note `--no-replace-objects`: the answer holds with the graft disabled. It
travels with a normal `git push`, needs no special refspec, and needs no setup
in a fresh clone.

The rule this leaves behind: **never import upstream as a parentless snapshot
again.** If a second upstream ever needs vendoring, merge it with
`--allow-unrelated-histories`, or graft it immediately, so the first merge is a
three-way merge rather than an archaeology project. `amelias-agent-ios` has
always done this correctly against `hermex` and sits zero commits behind as a
result — that is the standard to match.

## Doing a catch-up

```bash
git fetch origin                       # upstream: nesquena/hermes-webui
git switch -c chore/catch-up-upstream
git merge origin/master
```

Conflicts concentrate in the ~20 files both sides touch — mostly `static/*`,
`api/routes.py`, `bootstrap.py`, and the README, because that is where the
rebrand lives. Everything else has historically merged clean.

Then run the suite. It is expected to be **green**; if it is not, that is a
signal, not background noise. Keeping it green is what makes the next catch-up
cheap:

```bash
./scripts/test.sh
```

## The engine is pinned, deliberately

`bootstrap.py` used to install the engine with `curl -fsSL <installer> | bash`,
which installs whatever shipped that morning. Two people onboarding a week
apart got materially different engines, and nothing recorded which.

[`UPSTREAM_TESTED_ENGINE`](../UPSTREAM_TESTED_ENGINE) names the versions this
WebUI has actually been run against. `bootstrap.py` refuses anything outside
that range unless `HERMES_WEBUI_ALLOW_UNTESTED_ENGINE=1` is set — a refusal
rather than a warning, because a warning printed during a noisy first-run
bootstrap is a warning nobody reads.

Upstream's tags are date-stamped rather than semver (0.20.6 ships as
`v2026.8.27`), so the tag is recorded next to the version; it cannot be derived
from it.

### Evaluating a new engine without breaking your own machine

Clone the tag somewhere scratch and point the checker at it. Do **not** install
a candidate over the engine you use daily:

```bash
git clone --depth 1 --branch <tag> --filter=blob:none --sparse \
  https://github.com/NousResearch/hermes-agent.git /tmp/engine-candidate
git -C /tmp/engine-candidate sparse-checkout set --no-cone \
  '/agent/' '/cron/' '/hermes_cli/' '/tools/' '/*.py'
python3 scripts/check_engine_contract.py --mode symbols --engine-dir /tmp/engine-candidate
```

`--no-cone` matters: cone mode accepts directories only, so passing a file path
aborts the entire `sparse-checkout set` — and then every engine module reads as
deleted and the checker reports a tree full of failures that are really an
empty checkout.

A green result means the WebUI will not die on import. It does **not** mean the
engine behaves the same. Widening `max` in `UPSTREAM_TESTED_ENGINE` is a claim
about behaviour, so drive a real session through the candidate first.

## What the contract checker is for

The WebUI reaches deep into the engine's Python — `scripts/audit_agent_source_dependencies.py`
counts 245 dependency findings across 7 classes — including leading-underscore
names like `_get_auxiliary_task_config` and `_estimate_msg_budget_tokens` that
upstream is free to rename in any commit without it being a breaking change on
their side.

That is a defensible trade. It is only defensible if something checks, and that
audit script only *reports*; it cannot fail, so it cannot tell you an upgrade
just removed something. `scripts/check_engine_contract.py` is the part that
fails.

```bash
python3 scripts/check_engine_contract.py             # against the installed engine
python3 scripts/check_engine_contract.py --self-test # prove the guard still bites
```

It reads both sides with `ast` rather than importing anything — importing a
module to find out whether a symbol exists runs that module's side effects — and
it derives the symbol list from the source rather than hardcoding it, because a
hardcoded list rots the first time someone adds an import and then cheerfully
reports all-clear about a set that no longer matches reality.

It also distinguishes a symbol we **depend** on from one whose import site
already catches `ImportError`. Upstream uses that pattern deliberately; treating
those as hard failures would make the guard cry wolf, and a guard that cries
wolf gets skipped.

On its first run against a real engine it found
`agent.anthropic_adapter.normalize_anthropic_response` — imported unguarded by
two title-generation paths, and present in **no** version of the engine. Every
Anthropic-Messages-mode provider raised `ImportError` there instead of
generating a title, silently, because both call sites sit under a broad
`except Exception`.
