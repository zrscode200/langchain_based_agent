# MUSE Internal Retrieval Guide

MUSE is the **search and discovery layer** over internal sources. It is not the
authoritative program store. A returned record identifies *candidate* source
material; it establishes nothing about applicability, currentness, completeness,
or authority.

Read only the fragment you need.

## 1. Two tools

| Tool | Use |
| --- | --- |
| `muse_population` | Define or narrow a program-scoped population, and see its records, its count, and the distribution of values across it |
| `muse_read` | Read documents that a population identified, or a document reference recorded in earlier program work |

**They are not layered.** One tool's output is the next one's input; neither calls
the other. Deciding to read is a separate act from deciding what exists.

Everything about *how* a request is encoded is the connector's — which endpoint,
which engine, how a value is escaped, which field name each source uses for a
concept. You supply what you want to know. Every parameter whose wrong value would
produce a silently wrong answer is set for you and is not exposed.

The program filter is injected from the active workspace. **It is not an argument
and cannot be removed.** A source with no validated program-filter mapping is
excluded, and the exclusion is reported.

## 2. Defining a population

```text
muse_population(
  sources, about, where, where_not, since, until,   define one
  population,                                       or narrow one you hold
  semantic, question, order, depth, offset,          how to retrieve
  fields, distribution, value_prefix)                what the answer includes
```

`about` is free text across everything. `where` and `where_not` are lists of
clauses:

```text
{"in": [<field roles>], "any_of": [<values>], "match": "phrase" | "exact"}
```

- **`in` takes a role, not a field name.** Roles are stable across sources where
  field names are not: `anywhere`, `title`, `body`, `status`, plus ten taxonomy
  roles on `meds` — `issue`, `manufacturing_step`, `analytical_method`,
  `document_type`, `material_type`, `commercialization_step`, `drug_name`,
  `site_location`, `cmo_name`, `dosage_package_form`. A real field name is also
  accepted and is validated before anything is sent.
- **A role a source cannot fill is reported as unfillable, never substituted.**
  Measured live: `scited` fills neither `body` nor `status`, and `mrl_slides` fills
  no `status`. Substituting the nearest-looking field would confer a state the
  source does not represent.
- **But `status` on `signals` is not a document status.** It resolves there to an
  *experiment's* lifecycle, because that is the only status an electronic lab
  notebook has. So a `status` constraint on `signals` succeeds and constrains
  something different from what the same constraint means on `meds`. The role
  resolves; the meaning does not carry across.
- **A taxonomy role takes a clause of its own.** It cannot share one with another
  role, and one clause cannot name two taxonomy roles — both are refused with the
  reason. Use separate clauses; separate clauses are AND-ed.
- **`any_of` is OR.** Do not write `"A OR B"` as text — it is not an operator here
  and matches nothing.
- **`match: "exact"` is what makes a count defensible**, and it needs the exact
  stored value. Two ways to get one: read it off a distribution, or use
  `value_prefix` — `{"status": "Eff"}` returns the field's matching values, which is
  the route when a distribution is unavailable (see §6). `phrase` is
  case-insensitive and forgiving; `exact` matches the whole value and is
  case-sensitive.
- **`depth` defaults to `complete`**, which retrieves the population in one call and
  is what a defensible count needs. `sample` and `survey` are **refused** today —
  mapping them onto record counts is unfinished, and a guessed number would cap an
  exhaustive question silently.

`since` and `until` are applied by the connector, not by the index, and the result
reports both counts — what the source matched and what survived the date filter.

## 3. Narrowing is the whole method

**A large count is a population, not an answer.** The distribution is how you
narrow it without reading anything: it tells you what is *in* those records.

```text
muse_population(sources=["med_comms"], about="MK-6070")
  → count 19 · distribution { subtype: Protocols 8 · Study Reports 7 }

muse_population(population="<handle>", where=[{"in": ["subtype"],
                "any_of": ["Protocols"], "match": "exact"}])
  → count 8      exact, because the value came from the distribution
```

Pass the **handle** back rather than restating the question. A restated population
is a different population: on `meds`, four restatements of one intent returned four
different counts, the largest around **forty thousand times** the smallest, every one
HTTP 200 and every one plausible, with nothing in any response saying which
population it described. The
handle removes that by construction, and the result says whether re-running it
narrowed further or reproduced the same set.

**One exception, and the result tells you when you are in it: a similarity search gets no
handle.** Its recipe is a query embedding rather than a set of conditions, so nothing could
replay it, and its count is of chunks near that embedding rather than a figure a later count
can be compared against. Instead the result carries `question_asked` — the text that was
matched — and `population_not_offered` saying why no handle exists. **If you need a
population you can narrow and re-measure, run the request again with `where`.** Do not
restate the question to get back to it: that is the failure this whole section is about.

**Do not paginate through a large population.** Narrow it.

**A record set is not automatically the population.** One request can retrieve at most
10,000 records — the API's ceiling, not a choice of ours — and above that the result
says so in as many words, naming how many matched, how many are present, and that the
rest are described by nothing in the result. Read the count against the number of
records before treating a set as complete. Nothing is ever dropped silently, but
"nothing dropped silently" is not the same as "nothing dropped".

**Taxonomy roles are a different kind of claim from a word match.** *"The source
classifies this as an Out of Specification investigation"* is a source statement;
*"the phrase appears somewhere in it"* is your inference from word occurrence. The
tag is more precise and sometimes recovers documents the phrase does not appear in
at all. You never need to know a term in advance — read it off the distribution of
the population you already hold.

## 4. The five sources are five different systems

| Datasource id | What it is | Program boundary | Order of magnitude |
| --- | --- | --- | --- |
| `meds` | controlled and strategy documents | exact | ~1,000 |
| `med_comms` | medical communications | exact | ~20 |
| `scited` | published literature | exact | ~5 |
| `mrl_slides` | slide decks and slides | exact | ~70 |
| `signals` | electronic lab notebook | **broad first stage only** | ~15 |

**Orders of magnitude, not counts, and deliberately so.** `meds` was measured at
1,014, then 799, then 1,014 again over three days — a reindex moved about a fifth of
the index and then moved it back. **The count on the result is the only authoritative
one.** Do not carry a number from here, or from an earlier turn, into a conclusion.

What *did* hold across both moves is the arithmetic: the population minus the records
matching `(A or B)` equalled the `where_not` count exactly, in every state. Relations
between counts are stable; the counts themselves are not.

Use the **datasource id**, never a display name.

**Record shapes differ substantially, and the gaps are category differences rather
than defects.** `scited` is published literature, so "current version" and
"document status" are not properties it has. `mrl_slides` is a slide repository.
`signals` is a notebook, and its `experiment_status` is the lifecycle of an
*experiment*, not of a document. The result says *this source does not represent a
document version* rather than filling the slot.

**`signals` is the cautionary one.** Its boundary is a broader project code, and it
was measured to admit program records *and* controls, mixed records, and records for
an entirely different target. That the filter is applied does not make every
admitted record about this program. Records admitted by a broad code still need
exact program relevance established per record, and comparator material must stay
distinguishable rather than absorbed as program evidence.

**Index freshness is per source and per moment.** `scited` was six days stale while
`meds` was six hours. It is re-read at the start of every call and reported with the
count, so it is not a property of the source and not a startup snapshot. If it could
not be re-read, the result says that too, and the dates are then whatever the
connector last saw.

**Nothing is merged across sources.** Not counts into a total, not records into a
uniform shape, not relevance into one ranking — a term score has no fixed range and
is local to its source and its query, so two sources' scores are not comparable in
either direction. Four sources answering out of five is reported as four, never as
completeness.

## 5. Retrieval by similarity

`semantic=True` with a `question` retrieves by meaning rather than by term, and
returns the matching passage with each record. Two things travel with it:

- **Its count is labelled not defensible.** Rules, synonym tables and query
  rewriting all touch that number.
- **Most sources cannot do it.** Which ones is read from the index configuration at
  startup, not fixed here — today two of the five can. A source that cannot is
  **refused**, never answered by keyword search under a similarity label, so a
  similarity request across every source returns refusals alongside results. Those
  refusals are not empty populations. And where a source that *can* do it returns
  term matches anyway, the result says so.

Term relevance and similarity are **not the same quantity.** A term score has no fixed
range and its spread varies with the population; a similarity score is a cosine in
0..1. So one is not "higher" or "lower" than the other in any useful sense — they are
different measurements, and the result always names which engine produced it. Never
rank them together, and never carry a score from one into a comparison with the other.

## 6. What `meds` costs you, twice

`meds` is the largest source, and at present the only one whose program filter field
is not a field it can compute a distribution for. Filtering on it zeroes the whole
distribution, so on `meds` you get **either** the exact program population with no
distribution, **or** a distribution over a superset roughly 3.8× larger that
contains other programs' documents. Not both.

The same field carries a second cost. Whether a program filter is still live is
established from the source's own value list for that field, and `meds` computes none — so a
`meds` count reports `population_established: None`, and a `meds` zero names
`cause: "program_filter_unverified"`. The other four sources do compute one, and **measured
2026-09-01 each contains its configured literal**: `med_comms` `MK-6070` among 136 terms,
`mrl_slides` `MK-6070 (HPN328)` among 87, `signals` `M0060070` among 926, `scited` `MK-6070`
among 1001. Those four report `population_established: True`.

The distinction is worth holding because a filter whose stored value stopped matching returns
zero records at HTTP 200, indistinguishable from the programme having no documents. On the
four, a zero is a measured emptiness under a filter known to have matched something. On
`meds`, a zero is unknown coverage. Neither is proof that the material does not exist.

A value list carrying a `+` overflow bucket is a top-N rather than the field's vocabulary, so
a literal **absent** from a truncated list establishes nothing. `scited`'s list overflows at
1001 terms and contains its literal, so it is established there; absence from a list of that
shape would not be.

The result says which one you have and reports both counts on the superset path. It
never substitutes one for the other. Narrowing still works normally — one `where` on
`anywhere` took the population to about an eighth of itself in a measured call — you
just narrow with less to go on, and `value_prefix` is how you find an exact value
without a distribution to read it from.

**Nothing enforces that the other four stay this way.** They can be faceted because
those four filter fields happen to be configured facets today; a reindex could put any
source in `meds`' position, returning one facet that reads as "little to distribute"
rather than as a poisoned request. So read what each result says about its own
population instead of remembering which source behaves how.

## 7. Reading documents

```text
muse_read(documents, purpose, passages=None)
```

`documents` takes the handles a population gave you, or a document reference
recorded in earlier program work. `purpose` is required and states what the read is
meant to establish. `passages` returns the parts of each document that discuss
something, with their position, instead of the whole text — *"where does this
document discuss X"* is often the more precise question.

- **Most questions do not need the words.** Existence, extent, currency, locating a
  known document, and judging what is worth reading are answered from the population
  alone. Every record carries its extent, so the read decision is informed before
  anything is fetched.
- **What was requested is always diffed against what came back.** This endpoint
  drops documents it cannot return and still answers HTTP 200. Anything missing is
  named, with whether the reason is established or inferred.
- **Extracted text is not structurally faithful.** Table cells arrive as separate
  lines with row and column associations lost, so a real limit can attach to the
  wrong lot. Flagged when detected. **For a specification limit or acceptance
  criterion, the locator is the answer, not an extraction.**
- **A classification and an access list are different things, and a record may carry
  both.** A classification constrains what may be derived from the content and travels
  with any meaning taken from it; an access list constrains only who may look. Every
  record states which of the two it has — one source carries classifications, all five
  carry access lists — and neither is ever presented as the other. An absent
  classification is never a default of "unclassified".

## 8. An empty result is a claim you have to defend

**A zero comes with its own diagnosis.** The result names, by name, which causes of
a false zero it ruled out. There are seven. **Six are measured API behaviours** that
return HTTP 200 with a plausible-looking empty result; the seventh, `silent_widening`,
is this connector's own — four ways to send a request that would come back with the
**whole population** dressed as a narrowed one, all refused before sending rather
than observed from MUSE:

`denied_datasource` · `broken_program_filter` · `nonexistent_condition_field` ·
`multi_word_phrase` · `question_mark_in_query` · `asterisk_in_condition` ·
`silent_widening`

Read that list before concluding anything. It is not a formality: whatever is *not*
on it is what the zero might still be.

Do not re-run the search hoping a wider field helps. Widening addresses **one** of
those seven, so on its own it turns one undiagnosed zero into two.

**A refusal is a different thing and uses a different vocabulary.** Nothing was
executed, the result says so, and it names which of four areas the problem lies in —
source access, filter integrity, query rewriting, field validity. Those four are
about a request that never ran; the seven above are about a request that ran and
found nothing. Do not read one list as the other.

The result names its own cause, so this guide does not list the causes — it lists the
distinctions worth holding onto, because collapsing any of them turns a mechanical
outcome into a scientific claim:

| these are not the same | why it matters |
| --- | --- |
| **a refusal** and **a zero** | a refused request says nothing was executed. It is not evidence of anything |
| **a source that did not answer** and **a source with no matching records** | one is unknown coverage, the other is a measured emptiness |
| **a source not searched** and **a source searched and empty** | budget or deadline reached is a coverage limitation, never absence |
| **denied access** and **nonexistent** | on a read these are indistinguishable, and the result says so rather than guessing |
| **an unfillable role** and **an unpopulated field** | the first is what the source *cannot* represent, the second is what this record happens to lack |
| **index staleness** and **document staleness** | one document was modified in March 2025 and indexed in June 2026 |
| **a data classification** and **an access list** | one constrains what may be derived, the other who may look |

An authentication failure is not zero results.

> No visible matches means only that no accessible indexed record matched the
> executed request under the represented sources, fields, filters, access, and
> time. It is **never** proof that information or evidence does not exist.

The connector never silently broadens source scope, removes program filters, invents
metadata, reconstructs inaccessible content, or returns a success-shaped fallback.

## 9. Relating records, not deduplicating them

Different scientific and lifecycle roles must stay distinct even when they discuss
the same subject: target profiles, validation and qualification records, plans,
execution records, statistical analyses, reports, notebooks, presentations,
publications.

Relate them when represented identifiers support the relationship. **Do not merge
them because titles or indexed content are similar.** Cross-source linking was
measured weak-to-moderate: shared exact identifiers are inconsistently represented
across sources, so a same-study cross-source chain often cannot be established.
Report that as a coverage limitation rather than inferring the link.

## 10. Operational limits, and what counts as provenance

The MUSE QA token is short-lived, and multi-call retrieval works within a shared
deadline and call budget. If either is reached, the sources not yet searched are
reported as *coverage unknown*, never as empty.

Every result carries its own scope, engine, freshness, exclusions and restrictions.
**A tool name is not provenance** — if material becomes important to a scientific
revision, preserve a source-use reference built from what that result actually said:
exact source identity, the effective request, represented state and time, and the
limitations that applied. The skill states the scientific meaning; the result states
what was retrieved.

Raw results are **transient dynamic context by default**, and tool use creates no
work, artifact, or program ground. What retrieval success does not establish is in
`AGENTS.md` §11 and is not repeated here.
