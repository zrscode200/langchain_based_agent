---
name: establish-control-strategy-understanding
description: Construct, recover, qualify, explain, or selectively revise the strongest supportable current account of how a scoped product or process is understood to achieve and maintain a quality outcome — including its control relationships, evidence basis, assumptions, and uncertainties. Use when the scientist asks how something is currently controlled, how a control strategy works, what the current understanding is, why a control exists, or asks for a control-strategy account, description, or reconstruction; and when prior understanding must be recovered or requalified after a source, method, site, material, or process change. Do not use when the primary question is whether the strategy is sufficient (use assess-control-strategy-assurance), what explains an observation (use investigate-quality-concern), or what a defined state difference means (use assess-change-impact).
---

# Establish and Evolve Control-Strategy Understanding

## Owned outcome

> The strongest qualified current account of how a scoped control strategy is
> intended and demonstrated to work — for this scientist's purpose, represented
> configuration, evidence basis, and time.

This is **constructive** work. It surfaces limitations encountered while building
the account. It does **not** perform the deliberate adversarial gap assessment
owned by `assess-control-strategy-assurance`.

It does not declare the strategy adequate, approve anything, update authoritative
state, or initiate review or governance.

## Proportionate expressions

Not modes — read the situation and act accordingly.

**Concise current answer.** Existing understanding is sufficiently qualified.
Recover it, check scope and currentness, resolve only load-bearing dependencies,
answer directly. No persistent work, artifact, contributor, or broad retrieval.

**Complete delegated reconstruction.** Understanding is sparse, fragmented,
conflicting, or spread across sources and functions. Establish scope and intended
use, recover and qualify prior understanding, selectively acquire evidence,
compose bounded contributions where they add value, reconstruct the assurance
relationship, deliver a reasoning-bearing account.

**Recovery after material change.** A prior account exists but a dependency
changed. Locate dependent relationships, preserve unaffected meaning, requalify
what is affected, and explain what still holds, what narrows, and what became
uncertain.

## Native scientific kernel

You own the **construction of the control-strategy assurance relationship**. Do
not delegate this away; a version of this skill that only routes has failed.

```text
quality intent
-> relevant quality attributes
-> variability, exposure, and challenge pathways
-> applicable materials, operations, equipment, interfaces, conditions
-> control intent and applicability
-> implementation and execution
-> operational performance
-> scientific control contribution
-> analytical observability and criteria
-> collective relationships and shared dependencies
-> evidence, contradiction, uncertainty, and lifecycle state
```

This is a reasoning landscape, **not** a mandatory report structure and not a
persisted graph.

### Six control meanings that must stay distinct

| Meaning | Question |
| --- | --- |
| Documented intent | What is represented and intended? |
| Applicability | Does it apply to this configuration and time? |
| Implementation | Is it actually configured and performed? |
| Operational performance | Does it perform its operational function? |
| Scientific contribution | Does that performance address the relevant pathway? |
| Collective relationship | What does the control collection support together? |

Silently promoting a documented control to a demonstrated contribution is the
characteristic failure of this skill.

## Apply control-strategy guidance

Read `references/control-strategy-guidance.md` §§1–2 for the applicable ICH
framework. Connect quality intent and attributes to process/material knowledge,
the selected controls, their criteria rationale, and demonstrated performance.
Explain how upstream, in-process, and release controls contribute together;
specifications alone neither describe the complete strategy nor prove it works.
Use §4 for analytical dependencies and §6 when lifecycle or viral-safety questions
are material. Preserve the distinction between guideline principles, recorded
program commitments, and the account actually supported by program evidence.

## Adaptive process

**Frame what is actually requested.** Distinguish scientific topic, observation,
proposed mechanism, control-strategy question, assurance question, delegated
deliverable, and proposed use. Do not inherit every presupposition in the
scientist's wording.

**Resolve a conclusion-sensitive envelope.** Retrieve or clarify a condition only
when changing it could affect applicable evidence, control interpretation,
analytical meaning, represented assurance, the supportable conclusion, or the
intended use. See `references/program-state-and-currentness.md`.

**Recover and requalify prior understanding.** Start from `PROGRAM.md`'s recovery
index; open only what could change this account, and check each entry's triggers
for staleness — see `references/workspace-and-materialization.md` §9.1.

Prior ground and artifacts are reusable when scope matches, basis remains
recoverable, assumptions remain applicable, dependencies have not materially
changed, contradictions remain represented, and qualification suffices for the
present use. Prior work is qualified context — never independent evidence or
automatic current truth.

**Deepen through the move that matters.** Inspect a source; retrieve
authoritative recorded state; analyze program data; examine external evidence;
request specialist judgment; challenge the interpretation; identify missing
empirical evidence; or stop with a precise limitation. **Not every gap is a
search gap.**

**Determine scoped sufficiency.** You may complete when applicable scope is
adequately resolved, material control and assurance relationships are
represented, pivotal evidence and dependencies are visible, contradiction and
uncertainty are qualified, and another retrieval or contribution is unlikely to
change the requested account materially — or when an unresolved limitation is
precisely stated.

The result may remain conditional, partitioned by configuration, or
indeterminate.

## Composition

**Apply continuously:** `references/evidence-and-epistemic-contract.md`,
`references/program-state-and-currentness.md`.

**Compose another skill only when its full responsibility becomes material:**
`assess-control-strategy-assurance` if sufficiency becomes the primary question;
`investigate-quality-concern` if explaining an observation takes over;
`assess-change-impact` if a defined state difference becomes central;
`design-decision-changing-evidence` if creating new evidence becomes necessary.

**Tools.** Native capabilities for workspace and artifact inspection, files, git
history and comparison, local analysis, tables, diagrams, authoring. `ics-muse`
to recover documented control basis, state, intent, and internal evidence.
`ics-public-science` for mechanisms, method knowledge, and boundary conditions —
which can establish scientific *plausibility*, never program implementation or
performance.

**Retrieval shape.** When the available basis needs additional internal evidence,
recover it with `muse_population`: an exact
document or method identifier if you have one, otherwise constrained against
document-class fields in `meds`. An empty result carries its own
diagnosis — read what the tool ruled out before concluding absence. Several sources in one turn is
normal — they carry different record classes. Detail:
`references/muse-search-guide.md`.

When the account may omit a control relationship or boundary condition, bounded public discovery can help identify what deserves examination. Use `references/external-source-search-guide.md` to choose relevant indexing, relationship or article reads; qualify any added perspective against program evidence.

**When this needs the document's words.** The documented basis is what a
controlling document *states* — it cannot be inferred from titles, types or
statuses, so inspect the supplied source text or retrieve it with `muse_read`. For a specification
limit or acceptance criterion the locator is the better answer than the extracted
text: extraction loses table structure, so cite the document rather than a number
pulled out of it.

**Contributors** — only when separate execution adds value:

- `scientific-contributor` for a substantial evidence or reconstruction lane
  (product/formulation, process and manufacturing conditions, materials/device/
  packaging, analytical observability, stability/transport/handling exposure).
- `specialist-contributor` for focused analytical, process, formulation, device,
  manufacturing, statistical, or regulatory depth.
- `independent-challenger` proportionately — for broad delegated reconstruction,
  consequential intended use, possible anchoring to an earlier report, possible
  omitted pathways, unsupported transfer across configurations, or apparent
  cross-functional coherence that may be hiding disagreement.

You reintegrate every contribution into one coherent account. Never concatenate.

## Materialization

Judge each independently. Where these go — filenames, front matter, the recovery
index, supersession, change over time: `references/workspace-and-materialization.md`
§9. Conversation only is a legitimate result.

| Situation | Result |
| --- | --- |
| Bounded question answered from adequate basis | Conversation only |
| Material understanding changed, no product needed | Selected ground contribution |
| Work must pause or continue later | Persistent work |
| Complete delegated account requested | Reasoning-bearing control-strategy assessment |
| Existing account needed for another audience | Projection from the identified carrier |
| Exact composition must stay comparable over time | Stable snapshot (§9.8) |

A reasoning-bearing assessment should carry purpose and intended use; represented
scope and time; quality intent and attributes; variability and challenge
pathways; control relationships; analytical dependencies; evidence basis and
applicability; configuration partitions; contradictions and uncertainty; the
strongest supportable account; and important limitations and non-claims.

Control maps, evidence tables, dependency views, configuration comparisons,
narratives, and summaries remain **views** of that composition.

## Completion

Deliver: the account actually established; represented configuration and time;
evidence reach; important control and analytical relationships; material
contradictions and alternatives; assumptions and uncertainty; checks performed;
what remains unresolved or inaccessible; what the account supports; what it does
**not** establish; and what information could materially change it.

Conclusion first. Provenance progressively.

## Characteristic failures to avoid

- Treating a prior report as current truth.
- Promoting documented intent to demonstrated contribution.
- Assembling false coherence from records never reconciled with each other.
- Transferring evidence across configuration, scale, site, or method without
  establishing the conditions still hold.
- Drifting into exhaustive gap interrogation — that is a different skill.
- Becoming a router across contributors and tools.
