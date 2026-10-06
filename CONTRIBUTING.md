# Contributing to PuppetHub

Thanks for looking. This file is meant to answer one question: **what could I plausibly
contribute?** If the answer isn't here, that's a gap worth an issue.

The project premise: **an app is an agent.** An app perceives state, decides, acts, remembers,
and calls tools on its own. The interface is just one outlet for that. Most design decisions
follow from taking that seriously, and contributions are judged against it.

Two repos, both MIT:

| Repo | What it is |
|---|---|
| [`None-Ptr/puppet-language`](https://github.com/None-Ptr/puppet-language) | The language spec + a reference implementation |
| [`None-Ptr/PuppetHub`](https://github.com/None-Ptr/PuppetHub) | This repo — the software that builds and runs apps |

## Getting set up

Python 3.11 or newer. Both packages are on PyPI, so a clean install is one command:

```bash
pip install puppethub
puppethub verify-ci        # should print a pass/fail summary; this is your baseline
```

To work on the code itself, install both repos editable:

```bash
git clone https://github.com/None-Ptr/puppet-language
git clone https://github.com/None-Ptr/PuppetHub
pip install -e ./puppet-language -e ./PuppetHub
```

Then prove the checkout is sound before you change anything:

```bash
cd PuppetHub
puppethub verify-ci                        # conformance self-check, exits non-zero on failure
python conformance/runner.py --check       # only validate the case files (from puppet-language)
python docs/smoke-render.py                # one mechanism smoke test; all 21 live in docs/
```

Everything above runs offline. If any of it fails on a fresh checkout, that's a bug worth
filing before you start work.

## A ladder of first contributions

You do not have to start at the top. Each rung is a complete, mergeable contribution on its
own.

### 1. Add a conformance case

**The best first contribution, by a wide margin.** The spec is the source of truth, and every
case records which section of the spec it asserts. Adding one is self-contained: no engine
changes, no design discussion.

Cases are JSON in `puppet-language/conformance/cases/*.json`. Copy the shape of a nearby one:

```json
{
  "id": "g-minimal",
  "title": "Minimal program: root is a builtin node, structure is clean",
  "spec": "01-grammar.md §5, §7.1",
  "program": ["add #root window #win", "add #win col #main"],
  "loadExpect": { "noDiagnostics": true, "nodes": ["#root", "#win", "#main"] },
  "steps": [{ "expect": { "nodes": ["#main"] } }]
}
```

Validate it without needing a full implementation:

```bash
cd puppet-language
python conformance/runner.py --check
puppet conformance --filter g-minimal
```

Two rules, both load-bearing:

- **Cases are written from the spec, not from the implementation.** If you port an old test
  you are asserting what the code once did, not what the spec says. Those differ, and that
  difference is the whole point of the suite.
- The `spec` field must point at a real section. A case that can't be traced back to the
  spec is an opinion, not a test.

### 2. Fix a diagnostic that misfires

Diagnostics have codes in `puppet-language/spec/06-diagnostics.md`. If a case is wrong
because the diagnostic is wrong — wrong code, wrong level, wrong line number, or missing
entirely — that's a self-contained fix in `puppet-language/puppet/`. Attach the case that
exposed it.

### 3. Extend a mechanism smoke test

The 21 scripts in `docs/smoke-*.py` are the project's regression net, and they assert
*mechanics*, not features. Each has numbered sections; adding one means asserting a
boundary nobody checked:

```bash
cd PuppetHub
python docs/smoke-agentic.py      # 21 scripts: render, window, llm, protocol, controlplane,
                                  # memory, v2, hub, fusion, edit, build, society, skills,
                                  # gui, keys, home, aesthetics, perception, watch, agentic, plan
```

CI globs `docs/smoke-*.py`, so a new script joins the pipeline automatically. A missing
assertion is a genuine gap, not busywork.

### 4. Improve a built-in skill

Skills are markdown in `puppethub/puppethub/builtin_skills/` — domain knowledge injected
into the LLM's context on demand. A skill is a self-contained file, so fixing a wrong claim
or filling a gap is a clean PR.

Two constraints: explicit trigger words (no vector search, by design — results stay
explainable), and trigger words are group-disciplined. A word that reads too broadly drags in
unrelated skills; a single letter or a bare noun is a known trap. Check what currently matches
before you add one.

Two of these are **generated** from the spec (`诊断速查` / `控件语义`) — edit the spec and
re-run `python docs/sync-spec-skills.py`, never the copies.

### 5. Propose a language change

Anything touching the spec starts as a proposal in `puppet-language/spec/proposals/`, never
as a direct edit. `touch-semantics.md` is the worked example of the format.

Write what problem exists, what the language does today, and at least one option for changing
it. Proposals that start from "this is how it should be" tend to stall; ones that start from a
concrete gap tend to converge.

### 6. Report a bug well

The most useful bug reports include the **smallest program that reproduces it**. A
`repro.txt` that fails beats a paragraph. Diagnostics reference a spec section — quoting it
helps.

## How the code is organized

**PuppetHub.** `puppethub/` — `session.py` holds the writer state machine and per-turn
assembly; `context.py` builds what the LLM actually sees and degrades in a fixed, declared
order; `engine.py` executes and observes; `chat.py` parses instruction blocks; `plan.py`,
`watch.py` and `autonomous.py` hold the agency layer. `docs/` carries the design docs and the
`smoke-*.py` net.

**puppet-language.** `puppet/lexer.py` → `parser` → `ir.py` (the tree) → `engine.py`
(evaluation) → `serialize.py` (the printed form that is the source of truth). `spec/` is the
normative document; the code follows it.

## Things that will get a PR declined

Not because they're bad ideas — because they break a mechanism the project depends on.

**Bypassing the writer discipline.** Editing `app.puppet` by hand, from a script, or by
patching the file directly. There is exactly one path that changes a program: a command
batch, validated, with an atomic whole-file write-back and a snapshot before the write. Bypassing
the conversation is fine; bypassing the discipline is not.

**Making failure quiet.** Any change that degrades, drops, caps, or ignores something without
emitting a diagnostic, log line, or event. The test is one sentence: *when this path fails,
can the agent see it?* If not, it's a bug. "Unimplemented" is a valid state — silence about
it isn't.

**Self-reporting evaluation.** An LLM asked whether it did the right thing will say yes. After
the fact, the project takes evidence from mechanisms instead: did anything change, was
anything rejected, did anything error, did anything stall. Please don't add a code path that
asks the model to grade itself.

**A second writer.** Writer roles (`llm` / `autonomous` / `none`) are mutually exclusive, per
instance. Collaboration means sending messages and calling capabilities that another app
lends you — never writing another app's program.

**Pulling programs apart.** `puppetOS` is design docs only. Don't add the implementation.

## Pull requests

- One change per PR. A rename and a behavior change in one diff is two reviews.
- Say what problem you were solving. If it fixes an issue, link it.
- If you touch the spec, update or add the conformance cases with it, and run
  `puppet conformance` before pushing.
- CI runs conformance, the smoke suite, a flet field probe, a spec→skill sync check, and a
  compile pass. All of it runs offline; you can reproduce it locally.

## Code of conduct

Be decent. Assume the other person is trying to help. Critique the code, not the person.
Harassment of any kind isn't welcome in issues, PRs, or discussion.

Report security issues privately to the maintainer rather than opening a public issue.

## License

Contributions are MIT, matching both repos. See [LICENSE](LICENSE).
