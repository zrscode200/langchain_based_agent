# Program State, Coordinates, and Currentness

## 1. There is no generic authoritative-state lookup

No tool can determine "the applicable pharmaceutical state." Recorded state is
distributed across documents, product and change systems, LIMS, manufacturing
systems, method repositories, regulatory records, program data, and qualified
human input — and those records routinely disagree.

```text
tool  -> returns what a system records
you   -> resolve which state each record represents,
         which configuration applies to the question,
         whether records conflict or remain incomplete,
         what cannot currently be established,
         and which differences are scientifically material
```

Conflicting records stay conflicting. Never merge them into a single
fictitious authoritative answer.

## 2. State meanings are not interchangeable

| State | Meaning |
| --- | --- |
| Historical reference | A prior baseline used for comparison |
| Currently documented | What controlled documents currently say |
| Registered or committed | What was filed or committed externally |
| Proposed or target | Intended, not yet decided |
| Approved but not implemented | Decided, not yet in effect |
| Implemented configuration | Actually configured |
| Actual operating state | Actually performed and operated |
| Observed state | What a batch, site, method, or lifecycle actually showed |
| Transitional / partial | Mid-change; parts differ |

Documented ≠ approved ≠ implemented ≠ operated ≠ observed. Most scientific
errors in change and assurance work come from silently substituting one of these
for another.

## 3. Program coordinate

A coordinate identifies *what* the science is about. Resolve only the dimensions
capable of changing the conclusion:

> product · formulation · strength · presentation · device · material · supplier ·
> site · scale · equipment · process · method · container · packaging ·
> lifecycle state · conditions · time

This is **not** an intake checklist. A question about analytical observability
may need method, version, and site and nothing else.

When materially different configurations remain in scope, **partition the
conclusion**. Do not average across them or silently pick one.

## 4. Program identity is typed, not a flat alias list

A program is not one string. Identifier relationships must stay typed, because
they have different scientific reach:

| Relationship | Reach |
| --- | --- |
| Exact alias / alternate spelling | Same program |
| Historical program or molecule name | Same program, earlier naming |
| Broader project / portfolio / accounting code | **Superset** — may admit other programs |
| Comparator or contextual program | Different program, retrieved for context |
| Unresolved candidate | Relationship not established |

A broader project code is a first-stage boundary only. Records admitted by it
still need exact program relevance established, and comparator or related-program
material must stay distinguishable rather than being absorbed as program
evidence.

The active program's boundaries and known limitations live in the program's
`PROGRAM.md`, and for retrieval specifically in
`references/muse-search-guide.md`. The per-source field mappings are the
connector's: it reads them from each source's own index at startup, and every
result is stamped with what it actually used. Datasource configuration and
indexed metadata drift, so a mapping recorded anywhere else is a snapshot rather
than the authority.

## 5. Currentness is resolved, never stored

There is **no** universal `current` field and no persisted current-view object.
Current scientific understanding is resolved for:

```text
program coordinate
+ scientific purpose
+ represented state and time
+ proposed use
+ available and permitted basis
```

Five different things called "current" must stay separate:

1. current **authoritative program state** — what the system of record holds;
2. current **source version** — the latest revision of a document or method;
3. current **scientific understanding** — the strongest presently supportable
   interpretation;
4. current **suitability for a proposed use** — whether it can carry this
   decision;
5. **historical reliance** — what was actually used before, and remains true as
   history.

A retrieved document being the newest version does not make its science current.
Recovering prior ground or an artifact is *orientation*, not proof that its
meaning still holds — and its presence in recovered context does not mean any
skill used or accepted it.

## 6. Resolving and comparing states

1. Identify which states the question actually requires — often more than two.
2. Retrieve what each owning system records, preserving version and locator.
3. Determine which state each record represents.
4. Identify differences, and which are scientifically material.
5. Identify conflicts, gaps, and what cannot be established.
6. State the applicable coordinate and its limitations.

For a **proposed** change the reasoning is counterfactual: *if implemented under
these conditions, which relationships, evidence, controls, and assurance
conclusions would remain applicable?*

For an **implemented or observed** change it is realized-state reasoning: *what
actually changed, under which conditions and time, and what does the evidence
establish about the resulting state?*

## 7. Material difference

```text
documented difference
  != scientifically material difference
  != established product or process effect
```

A difference is material only if it can change quality intent or attributes,
variability or causal pathways, material and product-contact relationships,
process or equipment interaction, control applicability or contribution,
analytical observability, evidence transfer, collective assurance, or the
proposed use of the conclusion.

Watch for **bundled or confounded** differences: the named change is orientation,
not necessarily the complete scientific change. Co-changes that arrived with it
are frequently the ones that matter.
