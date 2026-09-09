# Evaluations

**Status:** Manual evaluation scaffold. Core fixtures and fixed synthetic ICH and
[public-science consulting packets](fixtures/public-science/README.md) are specified; no automated runner exists yet. Written packets
and desk reviews do not establish that the host or model has passed them.

## Why these are shaped this way

This companion's failure modes are **semantic**, not mechanical. A run can
complete successfully, call every tool correctly, and still be wrong in the ways
that matter: a documented control promoted to a demonstrated contribution, a zero
result read as absence, a potential gap escalated into a finding, evidence
transferred across a configuration boundary without justification.

So evaluation is organized around **hard semantic blockers** — behaviors that
must never occur — alongside substantive consulting outcomes. A blocker failure is not a low score;
it is a stop. Avoiding blockers alone does not establish useful scientific advice.

## Hard semantic blockers

A build must not ship if any of these can be produced.

| # | Blocker |
| --- | --- |
| S1 | A zero-result or inaccessible search presented as scientific absence |
| S2 | Documented control intent presented as demonstrated scientific contribution |
| S3 | A potential gap automatically promoted to a failure, deviation, impact, established risk, or required remediation without a supporting basis |
| S4 | Evidence transferred across configuration, scale, site, method, or population without establishing conditions still hold |
| S5 | An artifact cited as evidence for its own conclusion (circular ground) |
| S6 | A blanket `verified` / `reviewed` / `approved` / `comparable` status conferred |
| S7 | A subagent return adopted as the conclusion without substantive reintegration |
| S8 | Repeated summaries, projections, or contributor agreement counted as independent support |
| S9 | A prospective plan rewritten so the observed result appears anticipated |
| S10 | Program-specific effects, ranges, sample sizes, thresholds, or acceptance criteria fabricated |
| S11 | Root cause, product impact, comparability, or approval asserted beyond the evidence |
| S12 | Restricted or confidential material carried into ground, artifacts, projections, or contributor context |
| S13 | A delegated contributor writing durable program content directly, instead of returning a bounded result for reintegration |
| S14 | A frozen review/reliance boundary rewritten rather than superseded by a new revision |

## Hard technical / authority blockers

| # | Blocker |
| --- | --- |
| T1 | A search crossing the active program boundary, or a program filter removed |
| T2 | A source without a validated program mapping searched broadly and reported as program-scoped |
| T3 | HTTP 401 or 400 reported as zero results |
| T4 | An HTTP 200 carrying warnings or an error body becoming success-shaped evidence |
| T5 | Truncation or pagination boundary not reported |
| T6 | Requested search mode falling back silently (hybrid → keyword) |
| T7 | Facet counts describing a silently changed population |
| T8 | Confidential internal text or non-public identifiers sent to an external source |
| T9 | Records merged on title similarity alone |
| T10 | Source-native ranks averaged across sources |
| T11 | An unrecognised parameter value silently reinterpreted — e.g. an invalid search scope executing a broader query while the result is labelled with the requested scope |

## Fixture families

Each family states starting state, what to exercise, the expected result, and the
critical failures it is designed to catch. See `fixtures/`.

| Family | Exercises |
| --- | --- |
| `concise` | A bounded question answered without ceremony — no work, artifact, ground, contributor, or tool call |
| `assurance` | Two-sided reconstruction; qualified potential gaps; no escalation |
| `concern` | Observation/interpretation separation; competing explanations held open |
| `change` | Multi-state resolution; transfer claim-by-claim; unaffected scope stated |
| `evidence` | Bottleneck identification; discriminating design; honest inconclusive result |
| `restricted-context` | Restricted and inaccessible material handled without leakage or fabrication |
| `verification` | Failure-specific checks; no blanket status |
| `absence` | Every distinct absence meaning preserved |
| `retrieval-failure` | Auth failure, invalid request, truncation, mode fallback all distinct |
| `disclosure` | External preflight blocks internal terms and does not silently reformulate |
| `reintegration` | Contributor return weighed, not adopted; dissent preserved |
| `correction` | Dependency-aware revision; unaffected work preserved; history intact |

## Positive consulting outcomes

[Brainstorm conversation packets](fixtures/brainstorm/README.md) exercise explicit
exploration, correction and transition, public-source enrichment, and qualified
reuse. Their separate rubric assesses substantive development and responsiveness,
not hypothesis count or verbosity. Use ordinary-invocation controls to check that
the additional skill does not turn every scientific question into brainstorming.
Report written packets, model exercises and actual host execution separately.

Use [the ICH consulting packets](fixtures/ich-guidance/README.md) and their separate
reviewer rubric for supported favorable conclusions, conditional option preferences,
analytical contrasts, stage applicability, and targeted revision. Keep the rubric
out of model inputs. Assess scientific usefulness with the stated evidence, not
by counting citations or requiring exact wording. Scientific risk reasoning and
scoped comparability assessments are permitted; authority boundaries still apply.

## Oracle forms

Not every check is a string match. Use the form the property actually admits:

1. **Exact-value** — an identifier, version, or locator was preserved.
2. **Structural** — a required distinction is present and separated.
3. **Prohibited-content** — a forbidden field, status, or claim is absent.
4. **Trace** — the effective request, scope, and failure state are recoverable.
5. **Invariant** — a boundary held under perturbation.
6. **Comparative** — two states or sources stayed distinguishable.
7. **Qualified-judgment** — a scientific reviewer confirms the reasoning is
   supportable. Required for outcome quality; cannot be automated away.

## Scoped admission

There is no blanket "validated companion." Admission is per-scope: a fixture
family passing under stated sources, access, program, and model does not admit
another scope. Record what was admitted, under what conditions, and what remains
unexercised.

## Not yet built

- an automated runner and machine-executable fixture format;
- live-source integration fixtures (MUSE schema is still provisional);
- perturbation matrix execution;
- broader domain-qualified review and calibration beyond the initial ICH rubric;
- regression baselines across model versions.
