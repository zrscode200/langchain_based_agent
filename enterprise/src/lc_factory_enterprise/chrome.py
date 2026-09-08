"""Optional enterprise branding, adapted from the uploaded client."""
from .upstream_cli import StatusBar, WelcomeBanner
from textual.content import Content

_TOKENS_TAIL = " / Tokens:"
"""Where `StatusBar._context_segment` stops being the context percentage.

Upstream assembles `Context: NN% / Tokens: NNNk` as one segment, so the token
count can only be dropped by trimming the rendered result. A rename upstream
makes the trim a no-op rather than an error, which is what
`test_status_bar_drops_tokens_and_cost` exists to catch.
"""

_UPSTREAM_BRAND = "dcode"
_BRAND = "DDT-agent"
"""Enterprise client title; the CLI owns its separate version response."""


def trim_token_count(segment: Content) -> Content:
    """Cut the token count off a rendered context segment.

    Split out from the widget so it is testable: the widgets themselves resolve
    theme colors through `self.app`, so they cannot be built outside a mounted
    Textual app.

    Returns:
        The segment up to `_TOKENS_TAIL` with styling intact, or the segment
            unchanged when upstream's layout no longer contains it.
    """
    cut = segment.plain.find(_TOKENS_TAIL)
    return segment.truncate(cut) if cut != -1 else segment


def rebrand_title(banner: Content) -> Content:
    """Swap upstream's product name on a rendered banner for this project's.

    Substituted rather than stripped so the rest of the title line survives: the
    leading glyph, and the `(debug enabled)` / `(experimental)` tags, which
    upstream appends to the same line. `Content.divide` re-indexes spans, so a
    replacement of a different length keeps every other style and link intact.

    The class comes off the instance rather than an import. That predates the
    reasoning widgets, which do import `textual` directly; this function needs no
    such import regardless.

    Returns:
        The banner with `_UPSTREAM_BRAND` replaced by `_BRAND`, or unchanged when
            upstream no longer renders that name.
    """
    start = banner.plain.find(_UPSTREAM_BRAND)
    if start == -1:
        return banner
    before, _, after = banner.divide([start, start + len(_UPSTREAM_BRAND)])
    return type(banner).assemble(before, (_BRAND, "bold"), after)


class _QuietStatusBar(StatusBar):
    """Status bar without the token count or the running session cost.

    The context percentage is kept: it is the only on-screen indication of how
    close the conversation is to the auto-compaction trigger.
    """

    def _context_segment(self, count: int, *, approximate: bool = False) -> Content:
        """Return upstream's context segment with the token count trimmed off.

        Returns:
            `Context: NN%`, styling intact.
        """
        return trim_token_count(super()._context_segment(count, approximate=approximate))

    def _cost_text(self) -> str:
        """Drop the session cost.

        Returns:
            The empty string; `_render_tokens` filters segments with no text,
                so nothing takes its place on the metrics line.
        """
        return ""


class _RebrandedBanner(WelcomeBanner):
    """Welcome banner carrying this project's name and no version.

    Everything else upstream renders is left alone, including the MCP tool count
    — the one place a failed connector load is visible at startup.
    """

    def _build_banner(self) -> Content:
        """Build upstream's banner, then substitute the product name.

        `_hide_version` is forced here rather than in `__init__` because
        upstream's `__init__` both assigns it and calls this method, so an
        override there would be overwritten and would still paint once with the
        version showing. Setting it here also covers the surfaces that share the
        flag: the `(local)` tag and the `installed:` row.

        Returns:
            The banner with this project's name on the title line.
        """
        self._hide_version = True
        return rebrand_title(super()._build_banner())

