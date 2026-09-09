# Workspace and Materialization

How to decide what — if anything — should persist, and in which form.

## 1. Three forms, three purposes

```text
work     = continuity of activity
artifact = identity of a work product
ground   = continuity of scientific understanding
```

They are **not** maturity levels, not a graduation pipeline, and not mandatory
objects. They may coexist, reference one another, and change independently.

```text
                    ┌──────────────→ artifact
sources + context → scientific work
                    └──────────────→ ground

ground   ──────────────────────────→ artifact
artifact ── selected contribution ─→ ground
ground   ──────────────────────────→ new work
```

Arrows are possible relationships, not required stages.

## 2. Four independent judgments

Ask each separately. None implies the others:

1. Should selected meaning support **future program work**? → ground
2. Does evolving activity need **continuity**? → work
3. Does someone need an **independently usable product**? → artifact
4. Must an **exact state** stay recoverable for comparison, review, or reliance?
   → snapshot, or a frozen boundary where a reliance act exists (§9.8 — frozen is
   deferred in this workspace)

A great many engagements answer "no" to all four. Substantial reasoning may
remain entirely conversational — that is a success, not an omission.

## 3. Continuing ground

> Selected, qualified, revision-aware scientific meaning that future program work
> should be able to recover and reconsider.

Ground is a candidate when meaning:

- materially changes or qualifies program understanding;
- establishes an important observation, relationship, alternative, contradiction,
  uncertainty, or dependency;
- may affect future scientific work or a program decision;
- would be materially costly or risky to reconstruct;
- must survive collaboration or later evidence;
- is explicitly requested for preservation.

Ground is **not** an artifact collection, a report archive, a transcript store, a
list of everything generated, a copy of every source, one program narrative, or
accepted organizational truth. Most retrieved information, conversational
reasoning, intermediate analysis, and discarded mechanisms should stay transient.

A ground entry should carry, as material: the scientific meaning; applicable
scope, state, conditions, and time; why it matters for future work;
qualification, alternatives, uncertainty, limitations; origin and integration
basis; dependencies and reconsideration triggers.

### Never write circular ground

```text
BAD:  Artifact A concluded Y.
GOOD: Under configuration C and evidence basis E, interpretation Y holds,
      subject to limitation L and unresolved alternative Z.
      Complete rationale: artifacts/<artifact>.md, revision N.
```

The first makes the artifact evidence for itself. Preserve the *meaning and its
basis*, and link to the carrier for the composed rationale.

## 4. Persistent work

Useful when work spans interactions; waits for evidence, execution, specialist
input, or a scientist decision; contains several materially connected scientific
lines; requires substantial collaboration, correction, or disagreement handling;
would be costly or unsafe to reconstruct; or is explicitly requested to continue.

**Complexity, skill activation, tool use, or subagent use alone does not justify
persistence.**

Preserve only what responsible continuation needs (see
`correction-and-dependency-aware-revision.md` §6). Not task tracking, action
ownership, scheduling, status reporting, formal case state, CAPA management,
governance workflow, or organizational closure.

A living artifact may itself carry the required continuity. Do not create a
duplicate work object merely to record that work exists.

Stopping requires no artifact, no ground contribution, no definitive closure.

## 5. Artifacts

> A deliberately composed, bounded, identifiable scientific or program work
> product created for a defined independent use or audience.

Identity comes from **work-product purpose** — not from persistence, importance,
length, polish, citations, skill completion, or model generation.

Normally justified when: the scientist requests a deliverable; another person must
inspect, challenge, or review a defined body of work; prospective intent must
survive handoff or execution; a stable basis is needed for comparison or decision
support; complete rationale would be costly to lose; or an exact review or
reliance referent is needed.

An artifact may be created directly, emerge during work, or be written after the
science is done. It does not require prior persistent work.

### Roles (may compose in one artifact)

| Role | Contribution |
| --- | --- |
| Reasoning-bearing scientific artifact | A bounded interpretation and its composed rationale |
| Evidence-generation artifact | Prospective scientific intent, before results are known |
| Bounded review or decision-support package | The exact material, basis, questions, and snapshot placed before participants |
| Communication or exchange projection | Selected qualified meaning for an audience or format |

Control maps, evidence tables, hypothesis trees, timelines, gap views, matrices,
plots, and summaries normally remain **views**, not separate artifacts, unless an
independent use requires separate identity.

### Delivery can be the end

A complete artifact may be the valid endpoint of an engagement. Delivery does not
initiate a ground revision, review, acceptance, action tracking, governance,
release, stewardship, or an authoritative transition.

## 6. One primary carrier

> Preserve one primary durable carrier for each bounded body of scientific
> meaning within a defined scientific role and scope.

Other continuity, work-product, presentation, review, and reliance roles
**reference** that carrier rather than maintaining synchronized copies.

Repetition of expression is fine; duplication of maintenance responsibility is
not. A conclusion may appear in an assessment, a briefing, today's answer, and a
historical decision package. Only the carrier responsible for its scientific role
and scope receives ongoing maintenance.

| Situation | Primary carrier |
| --- | --- |
| Concise correction or observation needing future continuity | Ground-native entry |
| Integrated living program understanding | The control-strategy assurance spine neighborhood |
| Complete bounded assurance, concern, or change assessment | Artifact scientific revision |
| Prospective plan that must survive execution | Evidence-generation artifact |
| Interrupted consultation with no independently useful product | Work continuity |
| Living assessment where the science actually evolves | The living artifact itself — no duplicate work or ground copy |
| Exact state considered for review or reliance | Existing carrier + immutable frozen boundary — deferred, see §9.8 |

When findings from an artifact change program understanding:

```text
Artifact A remains historically intact
  -> explicit scientific integration
  -> spine revision adopts, narrows, challenges, or otherwise
     incorporates the selected meaning
```

They are **not** synchronized thereafter.

## 7. Views and projections

A current answer, control map, briefing, or machine-readable projection may
present an identified carrier without becoming another maintained scientific
state.

But if a projection introduces materially different interpretation,
qualification, recommendation rationale, or decision framing, that addition is
**new bounded scientific meaning** — not presentation.

## 8. Layered identity

Distinguish, and never collapse:

- **scientific revision** — scope, basis, assumptions, alternatives,
  interpretation, assurance conclusion, recommendation rationale, or prospective
  commitment changed materially;
- **work-product identity** — an independently usable product needs its own
  referent;
- **representation identity** — only wording, organization, visualization,
  audience, or format changed;
- **historical-boundary identity** — which exact state was considered for review,
  execution, communication, release, or decision.

Formatting and wording alone never create a new scientific revision.

```text
living    != automatically current or supportable
snapshot  != reviewed or approved
frozen    != current scientific truth
released  != scientifically verified
```

## 9. Directory conventions in this workspace

```text
workspace/programs/<program>/
├── PROGRAM.md   program coordinate, identifiers, scope limitations,
│                and the recovery index of what durable material exists
├── ground/      one file per durable meaning
├── work/        one file per resumable engagement
└── artifacts/   one file per bounded work product
```

Conventions for v1, not a schema and not a required runtime topology. Nothing
here creates an obligation to materialize — §2's four judgments still decide
that, and answering "no" to all four remains a success.

This workspace is not an archive you write to. It is the state you **reason
from**, so §9.1 matters more than the rest of §9.

### 9.1 Reading is the harder half

Only the index is always available. Everything else is read by your choice —
which keeps mental load down and stale material out of context, at the cost that
material you do not open cannot help you. **No one downstream checks your
answer.** Recovering the wrong neighborhood and answering confidently from it is
the most likely way this system is wrong.

So:

**Read the index first, and open by purpose.** Not everything relevant — what
could change the conclusion. Stop on scoped sufficiency, not on having read the
directory.

**Recovered is orientation, not current truth.** Prior ground and artifacts are
reusable only when scope matches, basis remains recoverable, assumptions remain
applicable, and dependencies have not materially changed. Their presence in your
context does not mean any skill used or accepted them. See
`program-state-and-currentness.md` §5.

**Check the triggers you recovered.** Each entry states what should reopen it.
Ask whether any of it has happened since. That is the staleness check, and it is
the read-time purpose of the same key you write.

**Your own past inference is still inference.** Material here was largely written
by you. When it comes back as `basis`, keep its claim kind visible — recorded
fact, observation, analysis, your inference, qualified judgment, accepted
decision, authoritative state. Recovering your own earlier reasoning and treating
it as evidence is how this memory becomes self-confirming. See
`evidence-and-epistemic-contract.md` §6 and §8.

**Recovered is not used.** Say what the reasoning actually rested on, not what
was in context.

**Say what you could not recover.** Unindexed, inaccessible, and absent are
different, and a gap in your own state is a limitation on the answer.

### 9.2 Only the companion writes here

A delegated contributor cannot write under `workspace/programs/` at all. Its
return is a proposal; materializing any part of it is the companion's act, after
reintegration. This is enforced, not advisory.

### 9.3 Naming and reference

Name a file for its meaning, in kebab-case: `elisa-detection-boundary.md`, not
`note-3.md` and not a bare date.

Cite by **name**, never by path shape: `[[elisa-detection-boundary]]`. Names
survive reorganization; paths do not.

Citation is one-directional. A file declares what it rests on and what should
reopen it. It does **not** maintain a list of everything citing it — that is
recovered by search when needed. Nothing is maintained in two places, so nothing
can silently drift.

### 9.4 Front matter

Three keys, on ground entries and artifacts. Everything else belongs in prose.

```yaml
---
scope: what this applies to — configuration, conditions, and time, stated as a
       delta from PROGRAM.md rather than a restatement of it
basis: [what this rests on, as links]
triggers: [what should cause this to be reconsidered]
---
```

`basis` is links rather than prose citation so common origin can be traced: two
entries whose basis resolves to the same artifact are **one** piece of evidence,
not two. See `evidence-and-epistemic-contract.md` §3.

`triggers` is not decoration, and it works in both directions. Writing it is how
a later change finds this entry at all — an entry with no stated trigger is
invisible to every future correction. Reading it is the staleness check of §9.1.

### 9.5 What each form carries

| Form | Carries | Contract |
| --- | --- | --- |
| Ground entry | The scientific meaning and its basis — never `Artifact A concluded Y` | §3 above |
| Work | The scientific position needed to continue responsibly, not the transcript | `correction-and-dependency-aware-revision.md` §6 |
| Artifact | The complete bounded rationale for its defined use | §5 above |

Living content is edited in place; git history is the revision mechanism for v1.
A material revision changes scope, basis, assumptions, alternatives,
interpretation, assurance conclusion, recommendation rationale, or prospective
commitment. Rewording alone is not a revision (§8).

### 9.6 Work is your working state

`work/` is not a filing requirement. It is how you hold a problem that is larger
than one exchange — many connected lines, a lot of context, evidence you are
waiting on, or a question that spans time. Use it when reconstruction would be
costly or unsafe, not because a skill activated or a subagent ran.

A work file preserves the **position**, not the transcript:

- purpose and the scientific outcome requested;
- applicable scope, state, conditions, and time;
- qualified starting basis;
- current position and the alternatives still live;
- unresolved evidence and dependencies;
- material contributions and how each was integrated;
- the scientist's corrections, choices, and redirections;
- what would count as useful continuation, or as responsible stopping.

Leave out routine prompts, private reasoning, tool chatter, abandoned branches,
and intermediate edits. On resumption, recover the position — do not replay the
conversation. See `correction-and-dependency-aware-revision.md` §6.

It is **not** task tracking, action ownership, scheduling, status reporting,
formal case state, or organizational closure. A living artifact may already carry
the continuity you need; do not create a work file merely to record that work
exists. Stopping requires no work file at all.

### 9.7 The affected neighborhood

When something changes, the material to revisit is the **connected** part —
neither the whole program nor only the line that changed.

```text
what changed
  -> search basis and triggers for candidates
     -> judge which are scientifically material
        -> revise · narrow · challenge · supersede · hold
           -> or state that no scientific change is needed
```

Search finds candidates. Only scientific judgment determines materiality: a
connected file is not necessarily an affected conclusion.

Two outcomes are routinely skipped and must not be:

- **Preserved.** Say what was examined and left standing, not just what changed.
- **No change needed.** A legitimate and common result. State it rather than
  silently leaving things alone.

**Supersession is a note, never a deletion.** A superseded entry stays and gains
a statement of the scope, use, configuration, time, and change for which
something else should now be relied on instead. Earlier meaning usually remains
necessary to interpret history. See
`correction-and-dependency-aware-revision.md` §4.

The affected neighborhood is *resolved* each time, exactly like currentness and
like a control-strategy slice. It is never stored as a grouping.

### 9.8 Change over time

The program changes, so understanding has to track change. That is why this state
is versioned — not for audit, and not because anyone will inspect it.

The trace lives in two places, and only one of them is yours to write:

- **git holds the bytes.** Exact earlier state is recoverable without you
  maintaining anything.
- **the entry holds the meaning of its changes.** Supersession notes accumulate
  rather than replace each other (§9.7), so an entry carries its own evolution:
  what changed, when, why, and for which scope something else is now relied on
  instead.

That is the temporal trace. Read it when you need to know how understanding got
here, or whether a conclusion has been narrowed since it was formed.

A separate point-in-time copy — a **stable snapshot**, in §8's terms — is worth
making only when comparison genuinely fails without one. None has been needed
yet. And a snapshot is for recovery and comparison; it is not review or approval.

**Frozen reliance boundaries are deferred.** A frozen boundary records the exact
material placed into review, execution, release, or a decision — a handover this
workflow does not currently have, since correction and acceptance happen in
conversation. Do not attempt to create one: any path containing `frozen` is
refused for every actor, including you, so the attempt will only fail.

### 9.9 Ground stays discrete

A ground entry is one bounded meaning. Ground is not a program narrative (§3).

If material wants to become the joined-up account of how the control strategy
hangs together — connecting intent, pathways, controls, contribution,
observability, evidence, and shared dependency across configurations — that is
the **integrated program account**, and its carrier is deliberately not yet
decided. Say so and keep the pieces discrete. Do not assemble it inside `ground/`
as a substitute; that would make a later carrier a migration instead of an
addition.

The primary-carrier row for integrated living program understanding (§6) is
therefore an open slot, not a missing file.

### 9.10 The recovery index

`PROGRAM.md` carries an index of what durable material exists: name, one line of
what it is, scope, and triggers.

That is enough to decide what to open **without opening anything**, which is the
point. The index is a flat list; grouping resolves at read time from scope and
triggers rather than from any directory hierarchy — a single entry is legitimately
connected to several neighborhoods at once, and filing it under one would be
lossy.

Add an entry when material is created; update its line when scope or triggers
change materially. An entry that exists and is unindexed is unfindable.
