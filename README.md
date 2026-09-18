# Computer-Use Automation System

An LLM-driven agent that learns a UI flow once ("discovery"), records it as
a typed, reviewable **capability artifact**, then replays it **deterministically
— no LLM in the decision loop** — with explicit handling for business
outcomes, recoverable conditions, hard failures, and human escalation.

See `/REPORT.md` for the design write-up (architecture, schema, error
handling, multi-tenant story, safety model, and what was cut).

## Status of the two required runs — read this before assuming the repo is "done"

| Run | Status |
|---|---|
| **Deterministic replay** (safety, business outcomes, escalation, checkpoints) | ✅ Real, automated, in `tests/` and pre-generated in `/evidence/` |
| **Discovery loop, action execution, recorder, escalation — as *code*** | ✅ Real, exercised end-to-end in `tests/test_discovery_pipeline.py` and pre-generated in `/evidence/`, against a real Playwright browser and a real local app. This test caught and fixed two genuine bugs (a `navigate` tool used the wrong input key; a click that triggers navigation wasn't awaited before the next observation) that a "does it compile" pass would have missed. |


I want to be direct about this rather than let the volume of code imply
more than what's actually verified: the deterministic-replay path is fully
proven end to end, including one genuine business-outcome error. The
discovery path is proven end to end *mechanically* (browser control,
locator building, risk/escalation, artifact recording, and that the
resulting artifact actually replays) — but the one input that can't be
faked without misrepresenting the project — an LLM actually deciding what
to click — is the step left for you.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium
export ANTHROPIC_API_KEY=sk-ant-...   # required only for `discover`
```

## Demo path

### 1. Run the tests (no API key, no network — validates schema, replay engine, safety, escalation)

```bash
PYTHONPATH=. python3 -m pytest tests/ -v
```

This spins up the bundled mock bank app (`mock_target_app/app.py`) on
localhost, hand-builds a `CapabilityArtifact` (bypassing the LLM entirely,
by design — see REPORT.md §1), and replays it with a real headless
browser: a happy path, two different business-outcome errors ("member not
found", "deposit too low"), an allowlist violation, and a real escalation
on the irreversible "submit" step.

### 2. Run the real LLM-driven discovery step (requires your API key)

Target: [demoqa.com/automation-practice-form](https://demoqa.com/automation-practice-form)
— a public, purpose-built automation-practice form with genuinely
non-trivial controls (a custom date-picker, non-native cascading
dropdowns), chosen per the brief's "your call" on target application.

```bash
python3 -m src.cli discover \
  --goal "Fill out the practice form for a new customer: first name Jordan, last name Lee, email jordan.lee@example.com, mobile number 9998887777, then submit and reach the confirmation screen showing the submitted details" \
  --url "https://demoqa.com/automation-practice-form" \
  --app-id demoqa-practice-form \
  --base-url https://demoqa.com \
  --allowed-domain demoqa.com \
  --param first_name=Jordan --param last_name=Lee \
  --param email=jordan.lee@example.com --param mobile=9998887777 \
  --headed
```

`--headed` is recommended for your first run so you can watch it work (and
so the terminal-based operator console has somewhere real to intervene if
the agent gets stuck or hits the risky "Submit" click, which the safety
policy will pause on by default — see REPORT.md §6). Drop `--headed` for
headless. Evidence (structured log + screenshots) lands in
`evidence/discovery-<timestamp>/`; the resulting artifact is saved under
`artifacts/`.

### 3. Replay the artifact it produced — deterministically, no LLM

```bash
python3 -m src.cli replay \
  --artifact-id demoqa-practice-form__fill-out-the-practice-form... \
  --param first_name=Alex --param last_name=Rivera \
  --param email=alex.rivera@example.com --param mobile=1234567890 \
  --headed
```

(Use the exact `artifact_id` printed by step 2 — it's derived from the
goal text.) Notice this call uses *different* param values than discovery:
that's the point — the artifact is a reusable, parameterized capability,
not a hardcoded macro. Try it again with an invalid mobile number or a
blank required field to see a business-outcome result instead of a crash.

### 4. Run the bundled mock app standalone (optional, for poking around)

```bash
python3 mock_target_app/app.py
# then open http://127.0.0.1:5055
```

## Project layout

```
src/
  artifact/    schema.py (the capability contract), recorder.py, store.py
  agent/       perception.py, llm_client.py, actions.py, loop.py (discovery)
  replay/      locator_resolver.py, engine.py, outcomes.py (deterministic replay)
  safety/      policy.py (allowlist, risk classification, redaction)
  escalation/  manager.py (intervention requests, live-session handoff)
  evidence/    logger.py (structured logs + screenshots)
  cli.py
mock_target_app/   local Flask fixture app used by tests (and optionally as a target)
tests/             real Playwright-driven tests against the mock app
artifacts/         saved CapabilityArtifact JSON files
evidence/          per-run structured logs + screenshots
```

## Configuration

- `CUA_MODEL` env var (or `--model`) overrides the Claude model used for
  discovery. Default: `claude-sonnet-4-5-20250929`.
- `--allowed-domain` sets the safety allowlist's permitted domain for a run
  (see `src/safety/policy.py`).
- `--no-interactive` auto-approves escalations instead of prompting at the
  terminal — useful for CI/unattended demo runs, not recommended for a real
  irreversible-action run.

## Known limitations / what I'd build next

See `/REPORT.md` §7 ("Cuts").
