---
name: assess-control-strategy-assurance
description: Determine the strongest scoped conclusion about what assurance a demonstrated control strategy can actually support under deliberate scientific challenge, where that conclusion is conditional or indeterminate, and which precisely qualified potential gaps materially limit it. Use when the scientist asks whether controls are sufficient or adequate, whether quality is assured, where the weaknesses or gaps are, what could go wrong that is not covered, whether the strategy would withstand challenge or inspection, or asks for an assurance, robustness, or gap assessment or scientific priorities. Do not use when the scientist wants a description of how control works (use establish-control-strategy-understanding), an explanation of a specific observation (use investigate-quality-concern), or the meaning of a state difference (use assess-change-impact). Assess does not mean assure.
---

# Assess Control-Strategy Assurance and Potential Gaps

## Owned outcome

> The strongest scoped conclusion about what assurance the demonstrated control
> strategy can support under deliberate scientific challenge — and which
> precisely qualified potential gaps materially limit it.

Answers: *what must be assured for this quality target, what does the
demonstrated strategy actually supply, and what assurance can their relationship
support?*

**`Assess` does not mean `assure`.** This skill does not certify or accept the
strategy, require that a gap be found, automatically convert potential gaps into
failures or established risks, prescribe required remediation or owners or dates, initiate review or governance or
monitoring, or change authoritative state.

## Native scientific kernel — connected two-sided reconstruction

This is what the skill owns and must not delegate.

### What must be assured? (top-down)

- defined quality intent and the assurance proposition being tested;
- materially viable variability, exposure, and failure pathways;
- applicable operating and lifecycle conditions;
- unresolved causal alternatives that could change what needs controlling;
- what must be prevented, reduced, detected, intercepted, released against, or
  monitored;
- what must be analytically observable.

### What does the demonstrated strategy supply? (bottom-up)

- documented control intent;
- current applicability;
- implementation and execution;
- demonstrated operational performance;
- demonstrated **scientific** contribution;
- analytical and sampling capability;
- collective coverage and interactions;
- dependencies, shared vulnerabilities, and residual possibility.

### Compare, and iterate

```text
what must be assured
        <->
what the demonstrated strategy supplies
        |
        v
strongest supportable assurance conclusion
```

**Why both sides are mandatory:** the top-down side prevents the existing control
inventory from defining its own adequacy test. The bottom-up side prevents
documented controls, specifications, methods, or passing results from being
treated as demonstrated assurance. Dropping either side is the characteristic
failure of this skill.

Possible conclusions — scientific meanings, not statuses:

- supported within scope;
- supported conditionally;
- different across configurations;
- indeterminate from the current basis;
- insufficient to support a stronger proposition;
- materially limited by one or more potential gaps.

## Potential-gap semantics

> A missing, weakly supported, inapplicable, unobservable, contradictory, or
> commonly vulnerable connection in the scoped assurance argument that could
> materially change what the strategy can support.

Each material potential gap must identify:

- the assurance proposition it limits;
- the affected pathway, control relationship, dependency, or observation;
- applicable configuration, conditions, and time;
- supporting evidence and reasoning;
- what remains unknown or unassessed;
- what the finding does **not** establish;
- the information-producing move most likely to resolve or narrow it.

A potential gap is **not** a demonstrated failure, product impact, deviation,
deficiency, risk score, regulatory or supply or commercial consequence, required
remediation, or accepted decision.

Valid result: *"No material potential gap was identified within the assessed
scope and available basis."* That is **not** an unqualified declaration of
adequacy — say so explicitly.

## Apply guidance and give scientific advice

Read `references/control-strategy-guidance.md` §§1–3 and §4 when analytical
capability carries assurance. Use applicable Q8/Q11/Q6B principles to examine
the combined control contribution, including justified upstream control and
the distinction between characterization and specifications.

When priorities or options are requested, compare material issues against the
quality proposition using Q9(R1): plausible failure pathways, existing protection,
uncertainty, and information likely to change the assessment. Explain why a
consequential unobserved pathway merits different attention from a resolvable
documentation uncertainty. Give a conditional preference when supported, including
what would reverse it; state when available evidence cannot rank alternatives.
Scientific risk reasoning is allowed, with explicit basis and assumptions; a gap
count does not supply a risk score. Affirm supported areas without inventing gaps.

## Deepening

Deepen according to the load-bearing uncertainty, never a functional checklist.
You do **not** need to exercise every functional area, control, source lane, or
method.

The next useful move may be: inspect a pivotal source; resolve applicable program
state; analyze relevant data; assess analytical capability; examine a challenge
pathway; request specialist judgment; invoke independent challenge; design
decision-changing evidence; or stop with a precise limitation.

## Composition

**Apply continuously:** `references/evidence-and-epistemic-contract.md`,
`references/program-state-and-currentness.md`,
`references/assurance-review-and-authority.md`.

**Orientation, not inheritance.** Start from `PROGRAM.md`'s recovery index and
open what bears on the assurance proposition, checking each entry's triggers for
staleness — see `references/workspace-and-materialization.md` §9.1.

Read prior control-strategy understanding and prior assessments as orientation
and navigation. Then **independently requalify the relationships that materially
carry your assurance conclusion.** A prior constructive account is never
inherited as proof of sufficiency.

**Compose another skill** when its full responsibility becomes material:
`establish-control-strategy-understanding` to recover the qualified account and
dependency map; `assess-change-impact` when a defined state difference is
central; `design-decision-changing-evidence` when an empirical uncertainty
materially limits the conclusion.

Keep native to this skill unless clearly justified otherwise: constructing the
assurance proposition, the two-sided reconstruction, control-contribution
reasoning, collective coverage and shared-dependency reasoning, potential-gap
qualification, and integration of the final conclusion.

**Tools.** `ics-muse` to inspect load-bearing controls, documented basis, state,
performance and monitoring records, exception and quality history.
`ics-public-science` to challenge the internal argument with known failure
pathways, shared dependencies, method limitations, and contradictory evidence —
note that absence of literature is **not** a control-strategy gap, and literature
is not a best-practice checklist.

**Retrieval shape.** Reconstruct both sides from the qualified evidence already
available; retrieve only unresolved material. For missing internal basis, use `muse_population` constrained on load-bearing controls,
performance and monitoring records, and exception history. When external
literature discovery is needed, use
`literature_search` against both PubMed and Europe PMC for the aligned
public-safe scope, then `literature_get` only for selected exact records.
Search the two indexes independently because neither is a superset, report
their coverage separately, and preserve provider-local rankings rather than
merging them.
Guides: `references/muse-search-guide.md`,
`references/external-source-search-guide.md`.

When the candidate failure pathways or dependencies may be incomplete, bounded public discovery can challenge the framing itself. The documented control inventory must not define its own adequacy. Use the external guide to explore relevant alternatives, then determine whether their conditions apply; a published vulnerability is not itself a program gap.

**When this needs the document's words.** A control's *demonstrated* performance
lives in the report body — inspect supplied text or read it with `muse_read`. A hit count establishes that a
report exists and never what it showed, and a status establishes even less. Where
the answer is a number measured against an acceptance criterion, prefer the
locator: extraction loses table structure, so a real limit can arrive attached to
the wrong lot.

**Contributors.** `independent-challenger` is especially characteristic here —
but not mandatory. Use it against omitted pathways, anchoring, unsupported
transfer, shared dependencies, and false coherence. Cognitive independence
creates neither empirical evidence nor qualified human review.
`scientific-contributor` for a substantial pathway, evidence lane, control
domain, or configuration. `specialist-contributor` for analytical, statistical,
process, formulation, device, or manufacturing depth.

You own the connected reconstruction, gap qualification, and integrated
conclusion.

## Assurance over your own assessment

Keep separate: (1) the assurance you are **assessing** in the strategy, and (2)
assurance **performed over your own assessment** — source inspection, state
resolution, deterministic reproduction, lineage tracing for false independence,
independent challenge for omitted reasoning, qualified review for specialist
judgment, new evidence when the limitation is empirical.

No blanket `verified` or `reviewed` label applies. Report what was checked, what
each check established, and what remains unchecked.

## Materialization

Judge each independently. Where these go — filenames, front matter, the recovery
index, supersession, change over time: `references/workspace-and-materialization.md`
§9. Conversation only is a legitimate result.

| Situation | Result |
| --- | --- |
| Narrow assurance question | Conversation only |
| Substantial continuing work | Persistent work surface |
| Complete delegated assessment | Reasoning-bearing assurance and potential-gap assessment |
| Material program-significant finding | Selected ground contribution |
| Different audience or proposed use | Projection from the identified carrier |
| Exact composition must stay comparable over time | Stable snapshot (§9.8) |

A delegated assessment should carry: purpose, intended use, and the assurance
proposition; represented scope and time; top-down assurance need; bottom-up
demonstrated strategy; evidence and applicability; analytical capability;
collective coverage and dependencies; strongest supportable conclusion; precisely
qualified potential gaps; uncertainty, dissent, inaccessible and unassessed
areas; assurance performed over the assessment; limitations and non-claims.

Control maps, pathway views, evidence tables, gap views, summaries, and briefings
remain **views**.

**No gap register, action plan, governance package, or monitoring process follows
automatically.**

## Completion

Complete when the assurance proposition and applicable scope are adequately
resolved; materially viable pathways have been considered; relevant controls and
analytical capabilities are understood; collective coverage, dependencies, and
vulnerabilities have been examined; the strongest supportable conclusion is
stated; material potential gaps — or the absence of identified ones — are
explained; inaccessible, conflicting, indeterminate, and unassessed areas remain
visible; and another available move is unlikely to change the conclusion
materially.

Deliver the conclusion first, then scope, basis, collective control reasoning,
potential gaps, limitations, actual checks, unresolved areas, and — when useful —
the information most likely to change it. When advice was requested, also deliver
reasoned priorities or a conditional option preference, with reversal conditions.

## Characteristic failures to avoid

- Letting the control inventory define its own adequacy test (no top-down side).
- Treating documented controls or passing results as demonstrated assurance.
- Inheriting a prior constructive account as proof of sufficiency.
- Manufacturing a gap to look rigorous, or suppressing one to look reassuring.
- Automatically escalating a potential gap into failure, established risk, impact,
  or required remediation without a separate supporting basis.
- Missing shared dependencies and calling repeated evidence corroboration.
