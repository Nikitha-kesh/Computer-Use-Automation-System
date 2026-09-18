# REPORT

## 1. Architecture

Single Python process, no queues/services — justified because the brief
explicitly discourages premature scaling infrastructure, and every core
requirement (discovery, artifact, replay, escalation, safety, evidence) is
naturally sequential within one run. The seam that *would* let this split
into services later is already there: `EvidenceLogger`, `ArtifactStore`,
and `EscalationManager` are the three stateful boundaries, and each is a
small class with a narrow interface (filesystem-backed today, trivially
swappable for a real DB/queue/operator-console service later).

Two execution paths share almost everything but never share a decision-maker:

```
Discovery:  perceive(page) -> LLM decides tool call -> execute_action() -> record transcript
Replay:     artifact.steps (already decided) -> execute_action() -> report outcome
```

`execute_action()` (src/agent/actions.py) is the single choke point both
paths call to actually touch the browser. This is the most important
architectural decision in the system: it guarantees replay does *literally*
what discovery did, mechanically, not "something equivalent." The only
thing discovery has that replay doesn't is `src/agent/llm_client.py` +
`loop.py`'s decision logic; the only thing replay has that discovery
doesn't is `locator_resolver.py`'s fallback-chain resolution against a
*pre-recorded* locator instead of a live-perceived element.

**Trade-off accepted:** perception (src/agent/perception.py) walks the live
DOM/accessibility surface via injected JS rather than using
screenshot+coordinates as the primary channel. This is more work up front
and doesn't generalize to a pure-image surface, but it's the right bias
for this environment: the brief's own description of these apps ("fairly
consistent," "changes slowly") means durable, structural locators pay off,
whereas coordinates break on the first pixel-shift. Section 4 covers how
this extends to a surface where DOM isn't available at all.

## 2. Artifact schema

`src/artifact/schema.py` is the focal point of the design. Key decisions:

- **Locators are ranked candidate lists, not single selectors**
  (`ElementLocator.candidates: list[LocatorCandidate]`), each tagged with
  the strategy used (`role_name > test_id > label_text > css > xpath >
  text`, in the order I generally trust them) and a confidence score.
  Replay tries them in list order and takes the first that resolves to
  exactly one visible element. This is the single biggest lever for
  robustness: a tenant that renamed a CSS class doesn't break an artifact
  whose primary candidate is `role_name`.
- **Steps reference input params by name** (`value_template: "{{member_id}}"`)
  rather than embedding literals, so one recorded artifact serves every
  future invocation — and, combined with `TargetSpec.tenant_id` being
  optional, the same shape supports tenant-agnostic "base" artifacts (§4).
- **Every step carries a `RiskLevel`**, assigned at record time by a small
  heuristic (`safety/policy.py:classify_risk`) based on the action type and
  the target element's accessible name (does it look like "Submit",
  "Transfer", "Delete"?). This is what lets the replay engine apply
  different handling to irreversible steps without every artifact author
  reinventing that logic.
- **Outputs are typed and traced to the step that produced them**
  (`OutputField.source_step_id`), so a reviewer can audit exactly where a
  returned value came from.
- **`Checkpoint` is a first-class field, separate from steps** — it's the
  thing that turns "we clicked some buttons" into "we verified we actually
  got somewhere," and it's what a calling agent implicitly trusts when it
  reads `outputs` back.
- **`review_status: draft | approved`** exists but nothing in the core path
  requires approval before replay runs (that's the optional "confidence &
  approval" stretch goal, not implemented) — what *is* wired up is that
  `RiskPolicy.decide()` takes `artifact_approved` as an input and, even
  when `True`, still defaults to requiring human confirmation on
  irreversible steps unless an operator explicitly opts into
  `auto_approve_irreversible`. I'd rather ship an artifact type that's
  "annoyingly cautious by default" than one that silently trusts itself
  with money movement.

## 3. Determinism & error handling

Determinism comes from three things: (1) replay never calls the model —
every decision was already made at record time; (2) the locator resolver
is a pure function of (live DOM, candidate list) with no randomness; (3)
`ValueResolver`-style template substitution (`resolve_template` in
`replay/engine.py`) is the only place param values enter the flow, so the
same artifact + same args always attempts the same sequence.

Error/outcome handling follows the three-way split the brief asks for,
implemented as a closed enum (`replay/outcomes.py:OutcomeKind`) so a
calling agent can never mistake one kind for another by accident:

- **`BUSINESS_OUTCOME`** — detected via a caller-supplied
  `detect_business_outcome(page) -> (code, message) | None` hook, checked
  after *every* step (not just failed ones, since an app can "succeed" at
  the Playwright level while rendering an error banner). Demonstrated for
  "member not found" and "deposit below minimum" against the mock app.
- **`RECOVERABLE_RETRIED`** — `interstitial_rules: list[Callable]` are
  tried whenever a step fails before falling through to escalation (e.g.
  dismissing a one-time cookie banner), and each step's own `RetryPolicy`
  governs plain timeout/transient retries. If any interstitial fired
  during a run, the final success is reported as `RECOVERABLE_RETRIED`
  rather than plain `SUCCESS`, so flakiness is visible to the caller even
  when the net result was fine.
- **`HARD_FAILURE`** — anything left over: a locator that never resolves,
  a checkpoint that's never reached, a blocked allowlist/risk violation.
  Always carries `failed_step_id`, `expected`, `observed` for debugging.
- **`ESCALATED`** — a step required human confirmation (irreversible risk)
  or exhausted its retries with no business outcome to explain it; the
  engine hands off to a human (§5) and reports this kind if the human
  couldn't/didn't resolve it. If the human *did* resolve it, the run
  continues and the eventual kind is whatever it would otherwise have
  been, with `escalation_id` populated so the caller can see a human was
  involved even in an overall-successful run.

Secondarily, on **UI drift**: the ranked-candidate locator design degrades
gracefully against small drift (a renamed CSS class, a re-ordered DOM) by
falling through to the next candidate. It does *not* detect semantic drift
(a field quietly changed meaning) — that's flagged as a known gap in §7.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is exactly the `execute_action()` /
`ElementLocator` boundary described in §1: everything above it (Step,
Checkpoint, artifact schema) is surface-agnostic; everything below it is
swappable per surface. For a **legacy web app** (framesets, no test IDs),
the existing `LocatorStrategy` set already covers it — `frame_path` on
`LocatorCandidate` addresses framesets/iframes, and the strategy ordering
naturally falls back to `css`/`xpath`/`text` when `role_name`/`test_id`
aren't available, since role/name comes from the accessibility tree which
legacy markup still exposes (buttons are still buttons even with div-soup
around them). For a **desktop app**, `perceive()` and `execute_action()`
would be reimplemented against an OS accessibility API (e.g. UIAutomation/
AT-SPI via a library like `pywinauto` or `atspi`) instead of Playwright,
but `LocatorStrategy.ROLE_NAME` maps directly onto desktop accessibility
roles/names with no schema change at all — this is why I biased toward
accessibility-first perception over screenshot+coordinates in §1. Only a
new `ActionType` handler set and a new perception backend would need to
exist; `CapabilityArtifact`, `Step`, `Checkpoint` are untouched.

**Multi-tenant reuse.** `TargetSpec` already separates `vendor_product`
(shared identity across tenants running the same underlying app) from
`tenant_id` (`None` = tenant-agnostic base artifact). The design I'd build
out with more time: record once against a "base" tenant with
`tenant_id=None`, and let a specific tenant's config carry a small
**override layer** — a sparse patch keyed by `step_id` that can replace a
locator candidate list, a value template, or skip a step, without forking
the whole artifact. Reuse-without-rebuild then works as: try the base
artifact's locators first; if a candidate fails for tenant X consistently
across N replays, that's the drift signal (tracked via the optional
"multi-run stability" stretch idea — a per-tenant success-rate counter per
`(artifact_id, step_id)`) that should prompt either promoting a
tenant-specific override or re-recording. I did not build the override
layer or the stability tracker — see §7 — but the schema was shaped
specifically so they slot in without a breaking change (an
`overrides: dict[tenant_id, dict[step_id, StepOverride]]` sidecar,
resolved at replay time before `execute_action` is called).

## 5. Escalation & handoff

"Stuck" is detected in three concrete ways, not a vague catch-all: (1) the
LLM itself calls `request_human_help` (discovery only — it's given this as
an explicit tool and instructed to use it rather than guess); (2) N
consecutive action failures during discovery (`MAX_STUCK_RETRIES = 3`);
(3) a step exhausts its `RetryPolicy` during replay with no business
outcome explaining the failure. All three funnel into the same
`EscalationManager.raise_intervention()` → `handoff_to_human()` pair
(`src/escalation/manager.py`), so there's one code path to reason about,
not three.

`raise_intervention` builds an `InterventionRequest` carrying exactly what
the brief asks for: which capability/goal, the current step, a screenshot,
and the reason — and logs it as evidence immediately, before any human has
responded, so the intervention itself is never lost even if the human
never shows up.

`handoff_to_human` is the control-transfer model: a single `control:
agent|human` flag flips to `human`, and the human is dropped into a bare
CLI loop (`operator>`) that issues commands (`click <css>`, `fill <css>
<value>`, `screenshot`, `state`, `approve`, `reject`) **against the exact
same live Playwright `Page` object** the automation was just driving —
there is one browser context per run, never a fresh session for the
handoff. Every command the human runs is itself logged as an evidence
event, so "what did the human do" is auditable the same way "what did the
agent do" is. When the human types `approve` (or `reject`), control flips
back to `agent` and the caller (loop.py or engine.py) decides how to
proceed — retry the step once, continue, or report `ESCALATED`.

**What's mocked, deliberately:** the operator UI is a terminal, not a
built console — exactly what the brief's scope note allows. What's real:
the pause, the same-session control transfer, the logged human actions,
and the resume/decision logic downstream of it.

## 6. Safety

Three independent layers (`src/safety/policy.py`), enforced at both
discovery and replay time (defense in depth against a hand-edited
artifact):

- **Allowlist** (`AllowlistConfig`) — domain glob patterns + route
  prefixes + an explicit set of permitted `ActionType`s. Checked before
  every navigation and before every action, not just at the start of a
  run. A violation is always a `HARD_FAILURE`, never silently ignored.
- **Risk policy** (`RiskPolicy`) — `SAFE` steps proceed automatically;
  `RISKY_REVERSIBLE` (a fill/select/check that mutates an unsubmitted
  form) proceeds automatically but is flagged in evidence; `RISKY_
  IRREVERSIBLE` (submit/transfer/delete-shaped actions) defaults to
  `REQUIRE_CONFIRMATION` even on an artifact marked `approved`, unless an
  operator explicitly sets `auto_approve_irreversible=True`. This is
  deliberately conservative for regulated financial data: the cost of an
  unnecessary confirmation is a terminal prompt; the cost of an
  unconfirmed wrong irreversible action is a real transaction.
- **Redaction** (`Redactor`) — applied only at the persistence boundary
  (evidence logs, artifact JSON), never to in-memory values used to
  actually fill forms. Flags by declared PII param name (`InputParam.pii`)
  and by a secret/PII-shaped key-name regex and value-shape regex (SSN,
  card-number patterns) as a second line of defense for values that end up
  in a field not obviously named for them.

**Limits, honestly:** the redaction regex is a heuristic, not a guarantee
— it will not catch every possible PII shape, and a determined artifact
author could still write a step whose `description` field leaks something.
The risk classifier is also a heuristic (keyword match on accessible
names) — it will misclassify a button labelled unusually. Both are
appropriate for a take-home; neither is what I'd ship to production
unreviewed.

## 7. Cuts

What I deliberately left out, and why:

- **Cross-tenant override layer + per-tenant drift tracking** (§4) — the
  schema is shaped to take it (`vendor_product`/`tenant_id` split) but the
  override-resolution and stability-scoring code isn't built. This is the
  single most valuable next piece given more time — it's the actual
  answer to "hundreds of tenants running ~20 apps each."
- **Desktop/native surface backend** — designed for (§4), not implemented;
  the brief explicitly doesn't require this.
- **Agent-facing capability/tool-calling interface** — artifacts are saved
  as reviewable JSON and loadable by `ArtifactStore`, but there's no
  HTTP/tool-calling front door an external agent could discover and invoke
  by name. Next step: a thin FastAPI layer that lists `artifacts/*.json`
  as callable tools with their `input_params`/`output_fields` as the JSON
  schema.
- **Confidence & approval workflow** — `review_status` exists in the
  schema but nothing gates unattended replay on it; `auto_approve_
  irreversible` is the closer analog today, and it's off by default.
- **Screen recording of a discovery run** — optional per the brief; not
  included, but every discovery run's evidence directory has full
  structured logs + screenshots at each step and on failure, which is
  fully sufficient to reconstruct what happened.
- **Semantic-drift detection** — the locator fallback chain survives
  *structural* drift (renamed class, reordered DOM) but has no way to
  notice that a field's *meaning* silently changed (e.g. "Account Type"
  became "Product Type" with the same DOM shape). Would need periodic
  artifact re-validation against a schema of expected output shapes.
