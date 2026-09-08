"""Compatibility imports for configurations from the enterprise source upload.

The optional enterprise distribution owns these adapters; OG never imports it
during ordinary factory or server startup.
"""
try:
    from lc_factory_enterprise.merck_models import (
        MerckChatModel, MerckAnthropicChatModel, MerckChatOpenAI,
        MerckChatAnthropic, MerckEndpointError, MerckEndpointUnreachableError,
        MerckEndpointReadTimeoutError, MerckEndpointStreamError,
    )
except ModuleNotFoundError as exc:
    if exc.name != "lc_factory_enterprise":
        raise
    raise ModuleNotFoundError(
        "Install the matching lc-factory-enterprise package to use enterprise model adapters"
    ) from exc

__all__ = ["MerckChatModel", "MerckAnthropicChatModel", "MerckChatOpenAI",
           "MerckChatAnthropic", "MerckEndpointError", "MerckEndpointUnreachableError",
           "MerckEndpointReadTimeoutError", "MerckEndpointStreamError"]
