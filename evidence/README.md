# What's in here

- `replay-*` (two runs): real deterministic replay runs — one success, one
  `business_outcome` ("member not found") — against the local mock app.
  Fully genuine: no LLM involved, exactly as replay is supposed to run in
  production.

- `discovery-*` (one run): the real `run_discovery` loop (real Playwright,
  real perception/action execution, a real risk-triggered escalation on
  the irreversible "Submit" click) — but driven by a **scripted fake model**
  (`tests/test_discovery_pipeline.py:FakeAgentClient`), not a live Anthropic
  API call. This is disclosed in the artifact's own provenance field
  (`recorded_by_model: "SCRIPTED-FAKE-MODEL..."`) so it can never be
  mistaken for a genuine discovery run if this JSON is read on its own later.

**A genuine LLM-driven discovery run has not been produced.** My sandbox
has no outbound access to arbitrary external sites and no Anthropic API
key. Running `python3 -m src.cli discover ...` (see README.md) with a real
key against demoqa.com is the one remaining step — its evidence will land
here as `discovery-<timestamp>` using the real `DiscoveryAgentClient`, and
its provenance will name the real model instead of the scripted fake.
