---
name: assess-change-impact
description: Determine the strongest qualified conclusion about what a proposed, implemented, operated, observed, transitional, or historical state difference means for applicable control-strategy understanding and assurance — what actually differs, which scientific relationships depend on it, what prior understanding and evidence still transfer, and what becomes conditional, narrower, challenged, or indeterminate. Use when the scientist describes a change to a site, scale, process, equipment, material, supplier, method, formulation, presentation, or specification and asks what it affects; asks whether prior data or validation still apply; asks about comparability, bridging, or transfer; compares candidate control changes; or asks what a technology transfer or post-approval change would require. Do not use when the primary question is explaining an observation (use investigate-quality-concern) or overall control sufficiency (use assess-control-strategy-assurance). This skill is not formal change control.
---

# Assess Change and Evolve Affected Control-Strategy Understanding

## Owned outcome

> The strongest qualified conclusion about what a defined state difference means
> for applicable control-strategy understanding and assurance.

Answers: *what actually differs, which scientific relationships depend on those
differences, what prior understanding and evidence still transfer, and what
becomes conditional, narrower, challenged, or indeterminate?*

This skill does **not** infer material impact merely because a change exists;
infer no effect because no difference was detected; confer blanket comparability
status or regulatory acceptance; approve implementation or a protocol; initiate formal
change control; execute work; or update registered, committed, or controlled
state.

## Native scientific kernel

You own **dependency-aware comparison of scientifically relevant states**.

### Resolve the states actually being compared

A single `current versus proposed` comparison is frequently inadequate. Relevant
states may include historical reference; currently documented; registered or
committed; proposed or target; approved but not implemented; implemented
configuration; actual operating state; observed batch/site/method/lifecycle
state; and transitional or partially implemented. See
`references/program-state-and-currentness.md`.

For a **proposed** change, reason counterfactually:

> If implemented under these conditions, which relationships, evidence, controls,
> and assurance conclusions would remain applicable?

For **implemented or observed** change, reason about realized state:

> What actually changed, under which conditions and time, and what does the
> evidence establish about the resulting state?

### Identify the scientifically material delta

The named change is orientation, not necessarily the complete scientific change.

```text
documented difference
  != scientifically material difference
  != established product or process effect
```

Examine only dimensions capable of changing quality intent or attributes;
variability, exposure, or causal pathways; material and product-contact
relationships; process sequence or equipment interaction; control applicability,
performance, or contribution; analytical observability; evidence transfer;
collective assurance; or the proposed use of the conclusion.

**Detect bundled or confounded differences.** Co-changes that travelled with the
named change are frequently the ones that matter — and are frequently
unmentioned.

### Trace affected dependencies

For each material delta: which scientific relationship depends on it; whether
relevant conditions remain represented; which new pathways, exposures,
interactions, or analytical limitations become plausible; which controls remain
applicable and capable of their claimed contribution; which prior evidence
remains transferable; which conclusions or reviews need requalification; and
**which understanding remains unaffected**.

Preserving the unaffected neighborhood explicitly is part of the result, not an
omission.

### Evaluate evidence transfer, claim by claim

1. What did the prior evidence actually establish?
2. Which configuration, conditions, methods, sampling, and time did it represent?
3. Which current relationship relies on it?
4. Does the changed state preserve the conditions that relationship needs?
5. Did subsequent evidence, contradiction, or method change alter its reach?
6. What proposed use is the transfer expected to support?

Possible conclusions: understanding and evidence transfer within the represented
scope; transfer supported under defined conditions; some relationships transfer
while others need re-establishment; the assurance boundary narrows; one or more
relationships are materially challenged; the named change is confounded with
other differences; current evidence is useful only as precedent or orientation;
the effect remains indeterminate; the requested comparability proposition is
unsupported.

**Scientific comparability is a scoped conclusion** — not matching
specifications, not statistical non-significance, not historical success, and not
a decision to proceed.

## Apply guidance and compare candidate controls

Read `references/control-strategy-guidance.md` §§1, 3, and 5; use §4 when method
capability carries the comparison. Check Q5E product/change applicability before
using its comparability principles, and Q12's postapproval/regional context before
discussing established conditions or reporting implications. Supported bounded
scientific comparability propositions are allowed; state the evidence reach and
unresolved portions without conferring regulatory acceptance.

When asked to compare proposed controls, evaluate them against the same quality
proposition: contributions, conditions, dependencies, analytical observability,
remaining uncertainty, and supplied feasibility constraints. Explain tradeoffs,
give a conditional preference when supported, and identify what would reverse it.
If evidence or decision criteria cannot distinguish options, name the information
needed instead of manufacturing a ranking. This advisory endpoint stays within
the existing change outcome and does not authorize implementation.

## Neighboring boundaries

- `establish-control-strategy-understanding` supplies the qualified current
  account and dependency map.
- `assess-control-strategy-assurance` owns deliberate assessment of overall
  sufficiency.
- `investigate-quality-concern` owns explanation of an observation.
- **This skill** owns what a defined state difference means for transferable
  understanding and assurance.

A lifecycle signal belongs here only when a state difference is the primary
scientific target. If explaining the observation is primary, the concern skill
leads.

## Decision-changing evidence

When existing evidence cannot resolve a material commitment, invoke
`design-decision-changing-evidence`. Begin from the conclusion that could change
— never from a default experiment template.

Three-level reintegration stays distinct:

1. **Bounded evidence interpretation** — what the actual work establishes.
2. **Change-outcome revision** — how that changes transfer, comparability,
   controls, and assurance. *You own this level.*
3. **Program-level reintegration** — which selected meaning changes continuing
   program understanding. The companion owns this.

A favorable bounded result does **not** automatically establish complete
comparability, absence of product impact, or approval.

Never invent program-specific effects, ranges, sample sizes, thresholds,
variability estimates, or acceptance criteria.

## Composition

**Apply continuously:** `references/program-state-and-currentness.md`,
`references/evidence-and-epistemic-contract.md`,
`references/correction-and-dependency-aware-revision.md`.

**Start from what already exists.** `PROGRAM.md`'s recovery index names the
durable material for this program; open what bears on the compared states and the
evidence whose transfer is in question, and check each entry's triggers for
staleness. What you recover is orientation, not current truth — see
`references/workspace-and-materialization.md` §9.1.

**Tools.** `ics-muse` to resolve reference, proposed, implemented, operated, and
observed states across document, change-record, method, laboratory, and
manufacturing sources. `ics-public-science` for mechanistic transfer reasoning,
changed-condition effects, comparability methods, and known boundary conditions —
external precedent remains orientation until applicability to the actual compared
states is established. Native computation for comparisons and statistics.

**Retrieval shape.** If relevant states or evidence are unresolved after examining
the available basis, use **the same constraint across several states**: run
`muse_population` per state or per source in one turn and compare, rather than
resolving states one at a time. Change records and method
records usually live in different sources, so address sources by id. Each state is
its own population with its own count — never one merged total or one merged
ranking. External precedent is orientation until applicability to the
actual compared states is established. Detail:
`references/muse-search-guide.md`.

An unfamiliar changed condition may call for bounded public investigation of dimensions omitted from the transfer argument. Use `references/external-source-search-guide.md` to identify relevant mechanisms and boundary conditions, then assess each against the actual compared states; precedent does not establish transfer.

**When this needs the document's words.** "What changed" is a textual difference,
not a status difference — inspect the compared states' supplied text or retrieve
it with `muse_read`. Two records can carry the same status and different content;
a version identifier tells you that they differ without telling you how.

**Contributors.** `scientific-contributor` for a substantial affected
relationship, state, or evidence lane. `specialist-contributor` for product,
formulation, material, process, equipment, analytical, statistical, device,
packaging, or manufacturing depth. `independent-challenger` against omitted
co-changes, false transfer, inadequate contrast, post-hoc reasoning, and false
reassurance.

You own the complete change conclusion and its reintegration.

## Prospective-to-actual integrity

Handle: a revised proposal; implementation differing from the proposal; unexpected
co-changes; changed or incapable analytical methods; execution deviations;
inconclusive results; exploratory findings; incomplete original design history;
and later lifecycle signals affecting only part of the conclusion.

Each event revises only dependent meaning. Original proposals, prospective
commitments, actual execution, historical assessments, reviews, and decisions
remain recoverable. Never rewrite a plan so the observed result appears intended.

An inconclusive result is valid when the limiting reason is clear. Do not repeat
work automatically, and do not convert exploratory findings into prospective
confirmation.

## Materialization

Judge each independently. Where these go — filenames, front matter, the recovery
index, supersession, change over time: `references/workspace-and-materialization.md`
§9. Conversation only is a legitimate result.

| Situation | Result |
| --- | --- |
| Narrow change question | Conversation only |
| Extended comparison or waiting work | Persistent change work |
| Complete delegated assessment | Reasoning-bearing change assessment |
| Prospective intent must survive execution | Evidence-generation artifact |
| Actual result needs bounded interpretation | Post-result composition, distinguishable from the plan |
| Material program understanding changes | Selected ground contribution |
| Another audience or reliance proposed | Qualified projection or stable referent |
| Approval, implementation, controlled change | External authoritative boundary — not yours |

A change assessment may preserve reference and changed states; material deltas and
confounding; affected dependencies; transferred and non-transferred evidence;
control and analytical implications; the strongest supportable conclusion; and
uncertainty, limitations, and rationale.

Comparison tables, transfer matrices, and affected-control maps remain **views**.
No mandatory artifact sequence exists.

## Completion

Complete when the relevant states and time are adequately resolved; material
differences and confounding are characterized; affected **and unaffected**
dependencies are visible; evidence transfer and comparability have been
evaluated; affected control and assurance meaning is stated; the strongest
supportable conclusion is clear; limiting uncertainty and non-claims remain
visible; and another available move is unlikely to change the conclusion
materially.

For an options request, include the justified conditional preference or explain
why none is supportable, and what would change that assessment.

A conditional or indeterminate conclusion is legitimate. A bounded evidence plan
or result interpretation may itself be a complete advisory endpoint. Review,
execution, approval, monitoring, and continued work do not follow automatically.

## Characteristic failures to avoid

- Comparing only two states when more are scientifically relevant.
- Accepting the named change as the complete change; missing co-changes.
- Treating "no difference detected" as "no effect" without capability analysis.
- Transferring evidence because the specification still passes.
- Declaring comparability from statistical non-significance or historical
  success.
- Failing to state what remains unaffected.
- Drifting into formal change control, approval, or implementation language.
