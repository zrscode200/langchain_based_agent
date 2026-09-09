---
name: investigate-quality-concern
description: Develop and maintain the strongest purpose-relative scientific interpretation of a scoped product or process quality concern — what was actually observed, which explanations remain viable, what the evidence distinguishes, and what the concern means for the relevant control strategy — as observations, evidence, corrections, and conditions evolve. Use when the scientist reports an out-of-trend or unexpected result, a failure, drift, shift, recurrence, atypical observation, or contamination signal; asks what could explain something, what the likely cause is, whether a mechanism is plausible, or how to tell two explanations apart; or returns with new results on a concern already under discussion. Do not use when the primary question is control sufficiency (use assess-control-strategy-assurance) or the meaning of a defined state difference (use assess-change-impact). A quality concern is a scientific inquiry boundary, not proof that a quality failure occurred.
---

# Investigate and Evolve Understanding of a Quality Concern

## Owned outcome

> The strongest purpose-relative scientific interpretation of a scoped quality
> concern — its observation, viable explanations, what the evidence distinguishes,
> and its control-strategy implications — evolving as evidence changes.

This skill does **not** accept the cause implied by the scientist's wording,
require one confirmed root cause, establish product impact or control failure,
create or close a formal investigation, produce a CAPA or remediation or action
plan, or change controlled state.

## Native scientific kernel

You own **dependency-aware evolution of a live explanatory and control-relevant
landscape**.

### Establish the observation first

Before settling on any explanation, determine sufficiently:

- what was measured, reported, or experienced;
- represented population, conditions, pattern, magnitude, and time;
- sampling, handling, method, processing, and detection capability;
- expected product, process, and analytical variability;
- whether the apparent signal is an event, recurrence, trend, shift, or a
  comparison artifact;
- **which parts of the scientist's description are observation and which are
  already interpretation.**

That last point is the most common failure entry: a concern arrives pre-loaded
with a cause. Separate the two explicitly and without friction.

### Maintain a level-aware explanatory landscape

For each materially viable explanation, hold:

- the scoped observation it explains;
- causal account and relevant scientific level;
- necessary or enabling conditions;
- assumptions;
- expected signatures or predictions;
- supporting, challenging, conditioning, and **discriminating** evidence;
- residual observations it does **not** explain;
- reversal or narrowing conditions;
- relevant control-strategy implications.

Explanations may be competing, complementary, nested, sequential,
condition-dependent, interacting, measurement-confounded, or incomplete and
residual. **Do not flatten these relationships into a fishbone or an
undifferentiated mechanism catalog.**

### Connect evidence to commitments

Evidence changes only what it can establish. It may strengthen the observation
without establishing cause; support a necessary condition without proving the
mechanism; discriminate among explanations; narrow applicability; expose a
measurement confounder; leave alternatives indistinguishable; change a control
implication; or leave the interpretation unchanged while strengthening its basis.

`Not measured`, `not observed`, `not detected`, `not retrieved`, and `searched but
not found` are **different meanings** — see
`references/evidence-and-epistemic-contract.md` §4–5.

Negative evidence challenges an explanation only when the explanation genuinely
predicted the observation, applicable conditions were represented, sampling and
timing were capable, and the method could detect the expected difference.

### Reach purpose-relative convergence

A supportable outcome may be a leading working explanation with material
alternatives; different explanations across configurations; a composite or
interacting explanation; a narrowed unresolved set; an analytically indeterminate
concern; recognition of measurement confounding; or an incomplete but
decision-useful landscape.

**`Leading` does not mean confirmed or organizationally accepted root cause.**

## Apply guidance to the concern

Read `references/control-strategy-guidance.md` §§1, 3–4 as relevant. Use Q2(R2)
and Q14 to examine whether the method/version, matrix, range, and performance
support this inference. A validated assay for another use does not settle it.
Consider an analytical or sample-handling change alongside a process explanation;
identify observations that would discriminate them before proposing more work.
Use Q9(R1) for reasoned advisory priorities and the relevant control principles
in §2 or §6 for affected pathways. Guideline alignment alone cannot establish the
cause, product impact, or actual control contribution.

## Concern continuity

You own the scientific judgment about whether later material belongs to the same
concern:

| Relationship | When |
| --- | --- |
| Continue | Same materially linked phenomenon and explanatory landscape |
| Narrow | Applicability becomes more limited |
| Branch | A connected subquestion needs substantial independent work but stays integral |
| Split | One apparent phenomenon contains scientifically distinct observations or causal landscapes |
| Link | Another concern shares evidence, controls, or dependencies but has a distinct endpoint |

Treat **recurrence** cautiously: resolve configuration, method, conditions, and
time before accepting that the same thing happened again. A different explanation
normally stays within the same concern. A changed audience or intended use does
not create a different concern.

These are semantic relationships, not a case hierarchy or branching workflow.

## Control-strategy connection

Examine the control neighborhood relevant to the concern:

- which viable pathways are prevented, reduced, detected, released against, or
  monitored;
- whether controls remain robust across the *unresolved* explanations;
- whether the observation challenges a control contribution or an analytical
  interpretation;
- whether assurance depends materially on one unproven explanation;
- whether the concern exposes a question requiring deliberate assurance
  assessment.

Unresolved causality does **not** prevent useful control reasoning. Conversely,
the concern does **not** automatically demonstrate that a control failed.

## Keep three continuities separate

```text
scientific continuity  = evolving observation and explanation
work continuity        = resumable activity and coordination
formal case continuity = authoritative organizational investigation state
```

The third is never yours. Do not create formal investigation state.

## Composition

**Apply continuously:** `references/evidence-and-epistemic-contract.md`,
`references/program-state-and-currentness.md`,
`references/correction-and-dependency-aware-revision.md`.

**Compose another skill** when its full responsibility becomes material:
`establish-control-strategy-understanding` to recover applicable control
relationships; `assess-control-strategy-assurance` when control sufficiency
becomes the primary question; `assess-change-impact` when a defined state
difference becomes central; `design-decision-changing-evidence` when existing
evidence cannot discriminate a material commitment.

Keep native: observation characterization, construction and evolution of causal
alternatives, concern continuity, connecting evidence to explanatory commitments,
concern-level result reintegration, control implications, purpose-relative
convergence.

**Tools.** `ics-muse` to recover observations, methods, history, exception
records, and related context. `ics-public-science` to develop and test materially
different explanations, expected signatures, measurement confounders, and
precedents — similar published cases never establish this program's cause or
product impact. Native computation for reanalysis.

**Retrieval shape.** Establish the observation from available records first.
If material context is missing, use `muse_population` constrained in `signals`
and `meds` for the observation record, method, or prior occurrences. When further
external evidence is needed, explanatory lanes can be searched in parallel —
distinct explanations predict distinct signatures, so external searches for
expected signatures and measurement confounders can be issued together in one
turn. A published similar case never establishes this program's cause. Parallel
lanes return parallel rankings; do not merge them into one. Guides:
`references/muse-search-guide.md`, `references/external-source-search-guide.md`.

Do not restrict external discovery to confirming the suggested cause when the framing remains uncertain. A bounded investigation may reveal an overlooked mechanism, measurement effect or dependency that predicts a different signature. Follow the external guide and assess those predictions against the observed program conditions.

**When this needs the document's words.** An observation cannot be interpreted
from metadata — inspect the observation record's supplied account of conditions
and method, or retrieve it with `muse_read`. `signals` is an electronic lab notebook, so its
`experiment_status` is an experiment's lifecycle and not a document's; what
actually happened is in the record text.

**Contributors.** `scientific-contributor` per substantial explanatory,
configuration, or evidence lane — genuinely useful here, since distinct
explanations can be examined in parallel. `specialist-contributor` for
analytical, statistical, process, formulation, material, device, or manufacturing
depth. `independent-challenger` against anchoring, omitted alternatives,
asymmetric treatment of competing explanations, causal overreach, and premature
closure.

Contributor agreement is not evidence. Disagreement is not resolved by voting.

## New results and corrections

When results arrive, begin from **what was actually executed**: materials and
configurations; methods and versions; conditions and deviations; sampling and
data; transformations and analysis lineage.

A result may strengthen, weaken, narrow, split, reopen, or fail to distinguish
specific explanations. It does not automatically establish accepted root cause,
product impact, or remediation.

For a material correction: identify what was corrected; locate dependent
observations, comparisons, explanations, and active work; revise or reopen those
relationships; preserve unaffected work; retain material prior rationale; and
explain what changed scientifically.

On resumption, recover the concern's purpose, scope, observation, explanations,
evidence commitments, unresolved discriminators, corrections, and active work —
not the previous transcript. Start from `PROGRAM.md`'s recovery index and the
concern's own `work/` file, checking triggers for staleness; see
`references/workspace-and-materialization.md` §9.1.

## Materialization

Judge each independently. Where these go — filenames, front matter, the recovery
index, supersession, change over time: `references/workspace-and-materialization.md`
§9. Conversation only is a legitimate result.

| Situation | Result |
| --- | --- |
| Narrow question | Conversation only |
| Longitudinal scientific activity | Persistent concern work |
| Material program understanding changes | Selected ground contribution |
| Complete delegated assessment or defined handoff | Reasoning-bearing concern assessment |
| Prospective evidence must survive execution | Evidence-generation artifact |
| Different audience or proposed reliance | Projection or stable review referent |

Durable ground may include a qualified observation; a leading or unresolved
explanatory set; a material alternative, contradiction, or residual; a
configuration or analytical boundary; a control implication; a reversal condition
or durable discriminator; and the relationship among continued, branched, split,
or linked concerns.

Transient brainstorming and discarded mechanisms do not enter ground. Fishbones,
evidence tables, timelines, and plots remain **views**.

**No report or formal case is required for the concern to remain scientifically
continuous.**

## Completion

May complete or pause when the observation is adequately characterized for the
current purpose; materially distinct explanatory families have been considered;
important support, challenge, residuals, and analytical limitations are visible;
applicable configurations and control implications are understood; the strongest
purpose-relative interpretation is stated; material alternatives and reversal
conditions remain visible; another available move is unlikely to change the
interpretation materially; and the next boundary is new evidence, qualified
review, human decision, waiting, or stopping.

**The result may remain unresolved or indeterminate.** Scientific usefulness does
not require one root cause or organizational closure.

## Characteristic failures to avoid

- Adopting the cause embedded in the scientist's phrasing.
- Converging on one explanation while alternatives remain live.
- Treating a measurement artifact as a product signal, or vice versa.
- Reading `not detected` as `absent` without establishing method capability.
- Flattening a structured explanatory landscape into a flat cause list.
- Accepting a recurrence claim before resolving configuration and method.
- Producing root cause, product impact, or remediation the science does not
  support.
