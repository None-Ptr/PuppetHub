# PuppetHub

**Software for building apps.** Not an app runtime framework, not a UI library.

You describe what you want in natural language; a built-in LLM turns it into a **command
batch**; the engine validates the batch, writes the source back, and renders. The apps you
build **contain no chat box** — chat only exists inside PuppetHub's own window.

> **An app is an agent.**
> The app itself perceives, decides, acts, remembers, and calls tools. Natural language is
> just one way of *driving* it — not its output. It keeps working on its own judgement when
> you stop talking to it.

## The three layers

| Layer | What it is |
|---|---|
| [`puppet`](https://github.com/None-Ptr/puppet-language) (PyPI: `openpuppet-language`) | Language spec + semantics engine + conformance suite. Zero third-party dependencies |
| **`puppethub` (this repo)** | Build / run / customize at runtime / merge apps |
| `puppetOS` | A Linux-kernel-based distribution. Design docs only, not implemented |

This repo depends on the language repo above. If you only want to read the spec or write
another implementation, `puppet` is enough.

## Install and run

```bash
pip install puppethub          # pulls openpuppet-language from PyPI automatically
puppethub verify-ci            # prove the install works — prints a pass/fail summary

puppethub new myapp
puppethub run myapp              # window: app on the left, cockpit on the right
puppethub run myapp --handoff    # start autonomous: snapshot an anchor, hand over the pen
puppethub run myapp --no-llm     # no LLM: stdio control plane, you are the writer
puppethub repl myapp             # hand-write batches as the driver
puppethub edit myapp             # replace the whole program (dry-run before commit)
puppethub remote myapp           # TCP headless binding
puppethub hub <dir> up|down|status|list   # orchestrate several apps
puppethub fuse <A> <B> --yes     # merge B into A
```

Python 3.11 or newer. The reference Tk renderer needs Tcl/Tk
(`apt install python3-tk` on Debian/Ubuntu).

## Four invariants

**No silent failure.** Every degradation, drop, cap, or unimplemented path must produce a
diagnostic, a log line, or an event. One test: when this path fails, can the agent see it?
If not, it's a bug.

**Only the LLM writes.** A human is a pure operator — no editing the program by hand. The
only way to change `app.puppet` is a command batch, which carries validation, line-numbered
diagnostics, and an atomic whole-file write-back at the end of the batch.

**Two ledgers, physically separate.** The program (`app.puppet` + `capabilities.py`) and the
state (`.puppet/`) never mix. The engine writes state and **never** touches the program — so
memory can never contaminate the program.

**The source of truth is a printed form of the program IR.** Comments and formatting get
reflowed on every write-back, so the only carrier of intent is `DESIGN.md`, not the comments
in the code.

These are mechanisms, not conventions. `docs/` holds the decision log, including the
decisions that were overturned and why.

## Configuration and plugins

`puppethub.toml` is optional — the zero-config path works. Three single-choice slots plus a
stackable prompt list:

```toml
llm_provider = "openai-compat"     # single choice
storage = "file"                   # single choice
style = "plain"                    # interface style recipe (plain / phosphor / plugin)
prompt = ["default"]               # multi-choice, applied in order

[plugins.openai-compat]
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"
key_env = "OPENAI_API_KEY"         # stores the *name* only: this file is tracked by git
context_limit = 128000             # provider's own window limit
```

Drop a plugin into `~/.puppethub/plugins/*.py` and it works — no install step:

```python
NAME = "my-provider"
PROVIDES = ["llm_provider"]        # llm_provider / storage / prompt / style

def create_llm_provider(api):      # one factory per slot
    return MyProvider(api)
```

Three boundaries, all enforced: plugins only produce values (`PluginAPI` hands out no engine
handle) · policy lives in the host, mechanism lives in the plugin (a storage plugin can only
write inside `.puppet/` and `.puppethub/`) · failure is always reported — a bad load, a wrong
slot name, a duplicate name, a path outside the allowed roots. A broken plugin never stops
startup.

## What exists today

**The path that runs.** Load → command batch → whole-file write-back to the source →
automatic snapshot before the write. The render layer maps program IR plus an observation
surface onto a flet widget tree (widget / attribute / animation / icon). The cockpit handles
chat, the observation stream, a read-only program panel, named snapshots, rollback, reload,
self-check, and shows you the exact prompt that was sent this turn. It blocks closing the
window with unsaved state.

**A writer state machine.** `llm` / `autonomous` / `none` switch exclusively, and every
switch is recorded. While autonomous is on duty, the co-author degrades to read-only
questioning. Its boundaries are harder: dangerous capabilities are denied by default (the
allowlist can only be written *in advance* by a human via `[autonomous] allow_calls`),
batches are capped in lines and frequency, and repeated failures trip a breaker back to
no-writer (with optional auto-rollback). Every step lands in
`.puppethub/autonomous.jsonl`; the cockpit shows rejected attempts too.

**Knowledge on demand.** One `.md` file is one skill; drop it in and it works. Three
directories: built-in `builtin_skills/` (ships with the package), app-level
`.puppethub/skills/` (**travels with the app** through merges and copies), and user-level
`~/.puppethub/skills/`. A skill is injected only when an explicit trigger word hits, so
unrelated work adds zero noise.

Skill groups are worth a note, because they decide how big each prompt gets. The old rule
was "hit any member of a group, inject the whole group" — measured on a real app, skills were
**77.8%** of a request while the program itself was 1.5%. The single word "button" dragged in
~10k characters. Now the shared discipline is always present and the specifics are on demand:
each group's lead carries a few hundred characters of group-wide rules in a `synopsis`
field (always in context, but only that summary — never the full body), matched specifics
come first, and everything left out is named on screen with a ` ```skill name``` ` escape
hatch. Same request: **43.8% smaller** overall, 55% smaller in the skill layer. See
`docs/design-skills.md` §1.1.

**Four pieces of work on making an app actually an agent** (`docs/review-agent-gap.md` §6):

- *Identity lives in the context, not in SYSTEM.* The co-author and the autonomous loop
  share one prompt chain; identity is injected per turn by `context.build(role=…)`. The
  system prompt used to hardcode "you are the co-author", which handed the autonomous loop
  two contradictory identities at once.
- *Handover.* `--handoff`, or the cockpit's "hand over to autonomous (set anchor)". It takes
  a named snapshot **before** switching writers, and aborts if the anchor fails. The anchor
  is needed because "drifting counts as evolution" requires a hard rollback point, and
  `DESIGN.md` is on the writable list — the app edits it, so it can't be the anchor.
  Handing over doesn't weaken any boundary; a human takes the pen back with
  `set_writer("llm")` at any time.
- *After-the-fact evaluation, gathered by mechanism.* Did the last step change anything, get
  rejected, error, or stall? The LLM is not asked to self-report "I did the right thing" —
  it would say "yes", and that's self-congratulation, not evaluation. "Decided not to change"
  and "was blocked" are counted as different outcomes.
- *Experience consolidation.* Skills may include `.puppethub/skills/*.md`, and they reload
  immediately after being written. Without the reload, the app would believe the lesson
  landed and make the same mistake next turn.

**Planning layer.** A `plan` block expands a `goal` into steps, each optionally carrying
`done: #node.attribute <op> literal`. Completion is evaluated by the host, not self-reported —
"the LLM writes the steps and ticks them off itself" is a self-reporting device that will
always tick "done". A step with no `done:` is **never** counted as complete (neither faked
success nor faked failure), and when a criterion can't be evaluated the host emits a visible
`PLAN_EVAL` warning rather than silently defaulting to "done". At most 12 steps; a plan
never increases the budget.

**Also implemented**: capability docs on demand (signatures always in context, docstrings
only when called) · runtime memory (readable and editable JSONL; reset preserves, wiping is
explicit) · snapshots and engine state through the storage slot · `fuse` merging (mechanism
does the health check, renames happen at the IR layer, B is archived and never deleted) ·
`hub` orchestration (never creates a second writer) · sandboxing (storage plugins run in a
subprocess) · remote and multi-client · credential resolution in three layers (env var →
system keychain → an error naming both layers it tried; only `secrets.resolve()` ever
materializes plaintext, and output carries the name, source, and length — not even a mask) ·
an agent society (apps expose services, a collaboration bus; messages are stimuli, never
writes) · `build --target android|web` producing standalone flet projects (credential
manifest, build-time REQUIRES resolution) · GUI coverage of the CLI (every action is a method
directly callable from a smoke test; the floating layer is a thin shell, semantics live in
the service layer).

## Not done

`flet build apk` builds successfully (a real 134 MB APK with the Python 3.14 runtime bundled;
step-by-step notes in `docs/design-v3-mobile.md` §4.1), but **installation and runtime on a
real device are unverified** — no device on hand.

Deliberately not done (rationale in `docs/design-v1-draft.md` §15): merge `drops` · a third
injection round for `needs_detail` · batch merges of N>2 · cross-machine bus · text-patch
write-back · multi-writer semantics · cross-process home discovery.

PyInstaller and Dockerfile live in `packaging/`. The container runs a headless binding; a
desktop window needs a display server, and we don't claim what we can't do.

## Self-check

Everything here runs offline and is repeatable. `verify-ci` currently reports
**130/130 passing (0 skipped)** with the product renderer, and **126/126** on a plain
`pip install` from PyPI.

```bash
python docs/probe-flet-fields.py     # the flet field surface the render layer depends on
python docs/smoke-render.py          # no GUI: load → render → batch → write-back → interact
python docs/smoke-window.py          # real (hidden) window: UI glue + background LLM thread
python docs/smoke-llm.py             # conversation loop mechanics (scripted provider)
python docs/smoke-protocol.py --full # protocol shell + full self-check (minutes)
python docs/smoke-controlplane.py    # run --no-llm: the driver is the writer
python docs/smoke-memory.py          # runtime memory: traced writes / editable / trimmed
python docs/smoke-v2.py              # writer state machine / autonomous / hot reload / sandbox
python docs/smoke-hub.py             # multi-app orchestration
python docs/smoke-fusion.py          # merge: audit halts / IR renames / dry-run / archive
python docs/smoke-edit.py            # hand-written programs: dry-run guard / rejected draft
python docs/smoke-build.py           # device packaging: manifest / REQUIRES / standalone
python docs/smoke-society.py         # services / collaboration bus / deep autonomy
python docs/smoke-skills.py          # skills: triggers / app-level override / groups
python docs/smoke-aesthetics.py      # vision self-description / screenshot feedback / budget
python docs/smoke-gui.py             # GUI over CLI, asserted mechanically
python docs/smoke-home.py            # home and app discovery
python docs/smoke-keys.py            # credentials never fail silently
python docs/smoke-perception.py      # L2 perception: blind spots / state changes / read-only
python docs/smoke-watch.py           # watch blocks: timers, thresholds, bounds
python docs/smoke-agentic.py         # agency: identity / anchored handover / evaluation
python docs/smoke-plan.py            # planning layer: plan blocks / mechanism-checked criteria
```

CI (`.github/workflows/ci.yml`) runs the conformance self-check, globs `docs/smoke-*.py`
(new scripts join automatically), probes the flet fields, verifies the spec→skill copies are
in sync, and compiles everything. Headless flet uses xvfb.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: conformance cases are written
*from the spec*, so adding one is a complete, self-contained first contribution.

## License

MIT. See [LICENSE](LICENSE).
