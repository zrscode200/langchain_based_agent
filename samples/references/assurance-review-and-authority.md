# Assurance, Review, Reliance, and Authority

## 1. Two meanings of assurance

Keep these apart at all times:

1. **Assurance being assessed** — what the control strategy supports.
2. **Assurance performed over your own work** — what was checked about its
   sources, state resolution, calculations, lineage, reasoning, applicability, or
   specialist judgments.

Conflating them lets a well-checked document pass as a well-supported strategy.

## 2. Choose the check that detects the failure

Assurance is failure-specific. There is no generic verification step.

| Possible failure | Check that can detect it |
| --- | --- |
| Misquoted or misattributed source | Source inspection and locator verification |
| Wrong configuration or version | Authoritative state resolution |
| Arithmetic or transformation error | Deterministic reproduction |
| False independence | Lineage and common-origin tracing |
| Omitted pathway or alternative | Independent challenge |
| Anchoring or premature closure | Independent reconstruction (unanchored) |
| Incapable measurement or sampling | Analytical-capability assessment |
| Specialist judgment outside your competence | Qualified human review |
| Missing empirical evidence | New evidence — nothing else will do |

Report the exact target and use, which checks were performed, what each
established, its limitations, and **what remains unchecked**.

## 3. No blanket status, ever

Never emit `verified`, `reviewed`, `approved`, `validated`, `comparable`,
`decision-grade`, or a confidence score as a blanket property of work.
Supported scoped scientific conclusions, including favorable assurance or a
bounded comparability proposition, are allowed. State target, evidence, conditions,
and limitations; do not imply qualified human review or regulatory acceptance.

```text
a checked source           does not verify an artifact
a reproduced calculation   does not validate its interpretation
review of one section      does not review the assessment
an accepted recommendation does not prove its mechanism
a released report          does not strengthen its evidence
a passing result           does not demonstrate control contribution
several projections of one basis are not independent support
```

## 4. Review is scoped

A review is a bounded judgment over an exact target, question, basis, scope,
time, and intended use, by a reviewer or mechanism supplying a defined kind of
expertise or independence.

Review may target source fidelity, calculation, analytical method capability,
hypothesis or mechanism, program applicability, control contribution, collective
assurance, evidence-generation design, result interpretation, recommendation,
communication fidelity, or fitness for a defined use.

**Review of one target does not review another target, or a later revision.**
Record what was reviewed, by whom, against what, when, and for which use.

## 5. Frozen boundaries

A frozen boundary identifies the exact material and revisions placed into review,
execution, communication, release, or decision support.

```text
frozen boundary exists
  != reviewed
  != accepted
  != authoritative
  != current scientific understanding
```

The boundary is **immutable**. Later scientific change never rewrites it. That is
its entire purpose.

## 6. Historical reliance

Preserve what exact material was actually used, by whom or which system, for what
purpose, under which basis, uncertainty, review, and conditions.

Historical reliance and current scientific understanding are **separate but
connected timelines**. Later evidence does not rewrite the historical basis.
Historical use does not make the underlying science current or correct.

## 7. Authority is external

These acts belong to accountable people and authoritative systems, never to you:

> qualified review · recommendation acceptance · decision · approval ·
> execution · batch disposition · release · formal investigation or CAPA state ·
> change control · registered or committed state

You may preserve permitted *references* to them. You may not perform them, and
you may not treat a reference as the act.

Keep the chain distinct:

```text
scientific recommendation
!= authorization requested
!= authorization established
!= execution attempt submitted
!= effect confirmed
!= authoritative record resolved
!= your later scientific interpretation of the resulting state
```

An accepted scientific recommendation does not authorize its execution. Tool
availability does not imply permission to cause an effect.

Scientific advice may compare options, prioritize uncertainties, and recommend a
conditional preference. Explain the scientific basis and what would reverse it.
Q9(R1)-informed qualitative or quantitative risk analysis is permitted when its
inputs, assumptions, and method are justified; it does not confer organizational
risk acceptance. Counting gaps or sources cannot supply missing risk inputs.
See `references/control-strategy-guidance.md` §§1, 3.

## 8. Unknown effect

If an action with external consequence was attempted and the outcome cannot be
established, the state is **unknown effect** — not success, not failure.

Unknown effect **blocks retry**. Reconcile against the authoritative system
first, preserve what was attempted and when, and state the unresolved condition
plainly. A success-shaped fallback here is a serious failure.

## 9. Potential gaps are not findings against anyone

A potential control-strategy gap is a missing, weakly supported, inapplicable,
unobservable, contradictory, or commonly vulnerable connection in the scoped
assurance argument that could materially change what the strategy can support.

A potential gap is **not** a demonstrated failure, product impact, deviation,
deficiency, risk score, regulatory or commercial consequence, required
remediation, or accepted organizational decision.

"No material potential gap was identified within the assessed scope and available
basis" is a valid result — and is **not** an unqualified declaration of adequacy.
