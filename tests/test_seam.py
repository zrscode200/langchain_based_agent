"""Wave 2.1: the middleware injection seam.

The parity suite (`test_parity.py`) proves the factory composes what v0
composes when nothing is injected. This suite proves the opposite direction:
that injected middleware lands where the seam promises, and that the two ways
the SDK's name-based merge can mis-handle an injection are rejected loudly
instead of silently mis-composing.

Observation point: the parity suite intercepts `create_deep_agent`, which sees
only the factory's own block. Placement is a property of the *final* stack, so
these tests intercept `deepagents.graph.create_agent` — one level deeper, after
the SDK has merged its base stack, our block, and its tail.
"""

from __future__ import annotations

import pytest
from langchain.agents.middleware.types import AgentMiddleware

from lc_factory.assembly import (
    _PHASE_ORDER,
    _SDK_RESERVED_MIDDLEWARE_NAMES,
    _normalize_injected_middleware,
    _validate_injected_middleware,
)


class _Probe(AgentMiddleware):
    """A middleware that does nothing but carry a distinguishable name."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    @property
    def name(self) -> str:
        return self._name


# --- normalization ---------------------------------------------------------


def test_none_normalizes_to_empty_phases():
    """`middleware=None` must produce no injections at all.

    This is what keeps the no-injection composition byte-identical to v0, so
    it is asserted directly rather than only via the parity suite.
    """
    resolved = _normalize_injected_middleware(None)
    assert set(resolved) == set(_PHASE_ORDER)
    assert all(not items for items in resolved.values())


def test_bare_sequence_lands_in_the_default_phase():
    probe = _Probe("probe")
    resolved = _normalize_injected_middleware([probe])
    assert resolved["before_verification"] == [probe]
    assert not resolved["first"]
    assert not resolved["last"]


def test_mapping_is_keyed_by_phase():
    first, last = _Probe("a"), _Probe("b")
    resolved = _normalize_injected_middleware({"first": [first], "last": [last]})
    assert resolved["first"] == [first]
    assert resolved["last"] == [last]
    assert not resolved["before_verification"]


def test_unknown_phase_is_rejected():
    """A typo'd phase key would otherwise drop the caller's middleware silently."""
    with pytest.raises(ValueError, match="Unknown middleware phase") as excinfo:
        _normalize_injected_middleware({"verification": [_Probe("probe")]})
    # The message must be actionable: it names the bad key and the valid ones.
    assert "verification" in str(excinfo.value)
    assert "before_verification" in str(excinfo.value)


# --- guards ----------------------------------------------------------------


@pytest.mark.parametrize("reserved", sorted(_SDK_RESERVED_MIDDLEWARE_NAMES))
def test_sdk_reserved_name_collision_is_rejected(reserved):
    """The dangerous mode: the SDK would replace its own middleware in place.

    Both halves of the SDK's stack are reachable, because its merge compares
    against the stack it has *fully* assembled. The tail is the worse half —
    it holds the approval gate.
    """
    injected = _normalize_injected_middleware([_Probe(reserved)])
    with pytest.raises(ValueError, match="reserved by the deepagents SDK") as excinfo:
        _validate_injected_middleware([_Probe(reserved)], injected)
    assert reserved in str(excinfo.value)


def test_duplicate_name_in_composed_stack_is_rejected():
    """Pre-empts langchain's own error, which names nothing.

    `create_agent` raises `AssertionError: Please remove duplicate middleware
    instances.` — loud, but it identifies neither the middleware nor the fact
    that an injection caused it.
    """
    injected = _normalize_injected_middleware([_Probe("GoalToolsMiddleware")])
    stack = [_Probe("GoalToolsMiddleware"), _Probe("GoalToolsMiddleware")]
    with pytest.raises(ValueError, match="Duplicate middleware name") as excinfo:
        _validate_injected_middleware(stack, injected)
    assert "GoalToolsMiddleware" in str(excinfo.value)


def test_validation_passes_for_a_clean_injection():
    injected = _normalize_injected_middleware([_Probe("probe")])
    _validate_injected_middleware([_Probe("probe"), _Probe("other")], injected)


def test_reserved_names_constant_still_covers_the_real_sdk_stack(tmp_path):
    """Re-derive BOTH halves of the SDK's stack; the constant must cover them.

    The SDK inserts a new (non-colliding) middleware immediately after the last
    entry of its *core* stack, so a probe's index is an exact discriminator:
    everything before it is core, everything after it is tail. Both are
    reachable for name-replacement, because the merge compares against the
    fully assembled stack — so guarding only the core would leave the approval
    gate exposed.

    Every conditional branch is switched on so the derived set is as wide as
    the SDK can produce for this model.
    """
    import deepagents.graph as sdk_graph
    from deepagents_code._fake_models import _ToolBindingFakeModel

    captured: dict[str, list[AgentMiddleware]] = {}
    real_create_agent = sdk_graph.create_agent

    def spy(*args, **kwargs):
        captured["middleware"] = list(kwargs.get("middleware") or [])
        return real_create_agent(*args, **kwargs)

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    probe = _Probe("__lc_factory_probe__")
    sdk_graph.create_agent = spy
    try:
        sdk_graph.create_deep_agent(
            model=_ToolBindingFakeModel(messages=iter([])),
            middleware=[probe],
            subagents=[
                {
                    "name": "probe-subagent",
                    "description": "fixture",
                    "system_prompt": "fixture",
                }
            ],
            skills=[str(skills_dir)],
            memory=[str(tmp_path / "AGENTS.md")],
            interrupt_on={"probe_tool": True},
        )
    finally:
        sdk_graph.create_agent = real_create_agent

    names = [item.name for item in captured["middleware"]]
    assert probe.name in names, "probe was not composed; the discriminator is invalid"
    split = names.index(probe.name)
    core, tail = set(names[:split]), set(names[split + 1 :])
    # Guard the guard: a silently-empty half would make this vacuous.
    assert core, "derived an empty SDK core set; the discriminator is invalid"
    assert tail, "derived an empty SDK tail set; the discriminator is invalid"
    # The tail must actually contain the security-relevant middleware, or this
    # case stops covering the failure it exists for.
    assert "HumanInTheLoopMiddleware" in tail

    derived = core | tail
    assert derived <= _SDK_RESERVED_MIDDLEWARE_NAMES, (
        "the deepagents SDK stack gained middleware the seam does not guard: "
        f"{sorted(derived - _SDK_RESERVED_MIDDLEWARE_NAMES)}. An injection "
        "using one of those names would silently replace SDK middleware "
        "instead of landing at its requested phase."
    )


def test_profile_extra_middleware_names_are_all_reserved():
    """Harness profiles install their own middleware, and it is replaceable too.

    `extra_middleware` is appended before the SDK's custom-middleware merge, so
    those names are as collidable as the core and tail. They are model-
    dependent, so the constant carries the union across every built-in profile
    and this test keeps that union honest.

    Derived here rather than in `assembly.py` on purpose: reading these names
    at runtime would mean importing the SDK's private profile registry into
    `src/` and *constructing* every profile's middleware on each composition,
    just to learn what they are called.
    """
    from deepagents.profiles.harness import harness_profiles

    from lc_factory.upstream import _ensure_glm_5p2_profile_registered

    harness_profiles._ensure_harness_profiles_loaded()
    # The factory registers the GLM-5.2 profiles itself, at COMPOSITION time
    # (`assembly.py`), not at import. Without this the derivation only sees the
    # SDK's built-ins when this module runs alone, leaving the tripwire
    # order-dependent on exactly the profiles this project owns.
    _ensure_glm_5p2_profile_registered()
    assert [key for key in harness_profiles._HARNESS_PROFILES if "glm" in key.lower()], (
        "factory-registered GLM profiles are absent; the derivation would "
        "silently skip the profiles most likely to gain an extra"
    )
    derived = {
        item.name
        for profile in harness_profiles._HARNESS_PROFILES.values()
        for item in profile.materialize_extra_middleware()
    }
    assert derived, "no profile extras derived; this case is vacuous"
    # At least one is langchain stock, i.e. a name a caller might plausibly
    # bring themselves — the reason this class of collision matters at all.
    assert "ToolRetryMiddleware" in derived
    assert derived <= _SDK_RESERVED_MIDDLEWARE_NAMES, (
        "a harness profile installs middleware the seam does not guard: "
        f"{sorted(derived - _SDK_RESERVED_MIDDLEWARE_NAMES)}. Injecting one of "
        "those names would silently replace the profile's own middleware."
    )


def test_non_middleware_entries_are_rejected_at_normalization():
    """Shape errors must surface before any setup work, with a real message."""
    for bad in (None, "not-middleware", object()):
        with pytest.raises(ValueError, match="must be AgentMiddleware"):
            _normalize_injected_middleware([bad])


@pytest.mark.parametrize("wrap", [list, tuple, iter, lambda items: (m for m in items)])
def test_one_shot_iterables_are_not_silently_dropped(wrap):
    """Regression: validating by iterating consumed generators before use.

    A `map`, genexp or any one-shot iterable used to compose as EMPTY — the
    caller's middleware vanished with no error. Silent dropping is exactly what
    the phase-key check exists to prevent, so the shape check must not
    reintroduce it.
    """
    probe = _Probe("probe")
    resolved = _normalize_injected_middleware(wrap([probe]))
    assert [item.name for item in resolved["before_verification"]] == ["probe"]

    keyed = _normalize_injected_middleware({"first": wrap([probe])})
    assert [item.name for item in keyed["first"]] == ["probe"]


# --- placement in the final composed stack ---------------------------------


def _compose(tmp_path, **kwargs) -> list[str]:
    """Compose a factory agent; return the FINAL middleware names, in order.

    Intercepts `create_agent` rather than `create_deep_agent` so the SDK's own
    merge has already run — placement is a property of the final stack, and a
    seam that only ordered the factory's own block would prove nothing.
    """
    import deepagents.graph as sdk_graph
    from deepagents_code._fake_models import _ToolBindingFakeModel

    from lc_factory.assembly import create_factory_agent

    captured: dict[str, list[AgentMiddleware]] = {}
    real_create_agent = sdk_graph.create_agent

    def spy(*args, **kw):
        captured["middleware"] = list(kw.get("middleware") or [])
        return real_create_agent(*args, **kw)

    sdk_graph.create_agent = spy
    try:
        create_factory_agent(
            model=_ToolBindingFakeModel(messages=iter([])),
            assistant_id="lc-factory-seam",
            cwd=tmp_path,
            **kwargs,
        )
    finally:
        sdk_graph.create_agent = real_create_agent
    return [item.name for item in captured["middleware"]]


def test_no_injection_composes_no_extra_middleware(tmp_path):
    """The seam is inert unless used — asserted here, not only via parity."""
    baseline = _compose(tmp_path)
    assert _compose(tmp_path, middleware=None) == baseline
    assert _compose(tmp_path, middleware=[]) == baseline
    assert _compose(tmp_path, middleware={}) == baseline


def test_first_phase_is_outermost_factory_middleware(tmp_path):
    names = _compose(tmp_path, middleware={"first": [_Probe("probe")]})
    # Immediately ahead of the factory's own first entry, and therefore behind
    # the SDK core, which the seam cannot address.
    assert names.index("probe") == names.index("ConfigurableModelMiddleware") - 1
    assert names.index("FilesystemMiddleware") < names.index("probe")


def test_before_verification_sits_between_policy_and_the_verification_tail(tmp_path):
    names = _compose(tmp_path, middleware={"before_verification": [_Probe("probe")]})
    # After the last context/policy middleware...
    assert names.index("LocalContextMiddleware") < names.index("probe")
    # ...and immediately ahead of the verification tail.
    assert names.index("probe") == names.index("CLICompactionMiddleware") - 1
    assert names.index("probe") < names.index("ReliableRubricMiddleware")


def test_last_phase_is_innermost_factory_middleware(tmp_path):
    names = _compose(tmp_path, middleware={"last": [_Probe("probe")]})
    # After every factory middleware...
    assert names.index("ReliableRubricMiddleware") == names.index("probe") - 1
    # ...but still ahead of the SDK tail, which stays unaddressable.
    assert names.index("probe") < names.index("AnthropicPromptCachingMiddleware")
    assert names.index("probe") < names.index("HumanInTheLoopMiddleware")


def test_bare_sequence_matches_explicit_default_phase(tmp_path):
    assert _compose(tmp_path, middleware=[_Probe("probe")]) == _compose(
        tmp_path, middleware={"before_verification": [_Probe("probe")]}
    )


def test_all_three_phases_compose_in_order(tmp_path):
    names = _compose(
        tmp_path,
        middleware={
            "first": [_Probe("p-first")],
            "before_verification": [_Probe("p-mid")],
            "last": [_Probe("p-last")],
        },
    )
    assert names.index("p-first") < names.index("p-mid") < names.index("p-last")
    # Injection order within one phase is preserved too.
    multi = _compose(tmp_path, middleware={"first": [_Probe("p-a"), _Probe("p-b")]})
    assert multi.index("p-a") == multi.index("p-b") - 1


def test_before_verification_holds_in_a_richly_gated_config(tmp_path):
    """The phase boundary must not move when configuration adds middleware.

    `goal_criteria_tools` installs `GoalCriteriaMiddleware` and `fs_tools`
    installs a `FilesystemMiddleware` override — both land near the phase
    boundary, which is exactly where an anchor defined against *conditional*
    middleware would drift.
    """
    from deepagents_code.mcp_tools import MCPServerInfo
    from deepagents_code.tools import fetch_url

    names = _compose(
        tmp_path,
        middleware={
            "first": [_Probe("p-first")],
            "before_verification": [_Probe("probe")],
        },
        goal_criteria_tools=[fetch_url],
        rubric_grader_tools=[fetch_url],
        fs_tools=["read_file", "write_file"],
        mcp_server_info=[MCPServerInfo(name="fixture-fs", transport="stdio")],
    )
    assert "GoalCriteriaMiddleware" in names, "case no longer exercises the tail"
    # Still ahead of the verification tail, now with GoalCriteria present.
    assert names.index("probe") == names.index("GoalCriteriaMiddleware") - 1
    assert names.index("LocalContextMiddleware") < names.index("probe")
    # `first` still leads every factory middleware...
    assert names.index("p-first") == names.index("ConfigurableModelMiddleware") - 1
    # ...except the documented exception: with `fs_tools`, the factory's OWN
    # FilesystemMiddleware shares a name with the SDK's, so the SDK's merge
    # replaces in place and hoists it ahead of this phase. Pinned here so the
    # docstring's qualification cannot quietly stop being true.
    assert names.index("FilesystemMiddleware") < names.index("p-first")


# --- guards, end to end through the public entry point ---------------------


def test_sdk_collision_raises_instead_of_silently_replacing(tmp_path):
    """Without the guard this composes cleanly and destroys SDK scaffolding."""
    with pytest.raises(ValueError, match="reserved by the deepagents SDK"):
        _compose(tmp_path, middleware=[_Probe("FilesystemMiddleware")])


def test_injected_hitl_cannot_silently_replace_the_approval_gate(tmp_path):
    """Regression: the SDK tail holds the approval gate, and it is replaceable.

    Injecting langchain's stock `HumanInTheLoopMiddleware` is the most natural
    thing a caller might do with this parameter. Before the guard covered the
    SDK *tail* as well as its core, this composed with no error and swapped the
    factory's approval gate for the caller's — measured at this pin, the gated
    tool set went from eleven entries (including `execute`, `write_file`,
    `edit_file`, `delete`, `task`) down to the caller's single tool, leaving
    shell execution and file writes unattended.
    """
    from langchain.agents.middleware import HumanInTheLoopMiddleware

    with pytest.raises(ValueError, match="reserved by the deepagents SDK") as excinfo:
        _compose(
            tmp_path,
            middleware=[HumanInTheLoopMiddleware(interrupt_on={"my_tool": True})],
        )
    assert "HumanInTheLoopMiddleware" in str(excinfo.value)
    # The message must explain the stakes, not just the rule.
    assert "approval gate" in str(excinfo.value)


def test_factory_name_collision_raises_our_error_not_langchain_s(tmp_path):
    """langchain also rejects duplicates, but names neither the middleware nor
    the cause. The seam must get there first with an actionable message."""
    with pytest.raises(ValueError, match="Duplicate middleware name") as excinfo:
        _compose(tmp_path, middleware=[_Probe("GoalToolsMiddleware")])
    assert "GoalToolsMiddleware" in str(excinfo.value)


def test_unknown_phase_raises_before_any_setup_work(tmp_path):
    with pytest.raises(ValueError, match="Unknown middleware phase"):
        _compose(tmp_path, middleware={"exit_gate": [_Probe("probe")]})


# --- what stack position MEANS at runtime -----------------------------------


def test_stack_order_is_an_onion_before_forward_after_reversed():
    """Earlier in the stack is OUTERMOST, not "earlier in time".

    The seam's phase documentation makes a behavioral promise — `'first'` gets
    the final say on the way out — and position assertions alone cannot support
    it. This runs the hooks and records the order.

    Deliberately exercised on a minimal `create_agent` graph rather than a full
    factory graph. `create_agent` is the single place hooks are wired, for our
    stack and this one alike, so it isolates the ordering law from the factory
    stack's own side effects (git inspection, plugin discovery, file reads).
    Placement of factory phases within that law is proven separately above.
    """
    from deepagents_code._fake_models import _ToolBindingFakeModel
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, HumanMessage

    trace: list[str] = []

    def _probe_class(label: str) -> AgentMiddleware:
        class _OrderProbe(AgentMiddleware):
            @property
            def name(self) -> str:
                return f"probe-{label}"

            def before_agent(self, state, runtime):
                trace.append(f"before:{label}")

            def after_agent(self, state, runtime):
                trace.append(f"after:{label}")

        return _OrderProbe()

    agent = create_agent(
        model=_ToolBindingFakeModel(messages=iter([AIMessage(content="done")])),
        middleware=[_probe_class("a"), _probe_class("b"), _probe_class("c")],
    )
    agent.invoke({"messages": [HumanMessage(content="hi")]})

    assert trace == [
        "before:a",
        "before:b",
        "before:c",
        "after:c",
        "after:b",
        "after:a",
    ], (
        "middleware hook direction changed: the seam documents `first` as "
        "outermost (before_* first, after_* LAST). If this reverses, every "
        "phase guarantee in create_factory_agent's docstring is backwards."
    )
