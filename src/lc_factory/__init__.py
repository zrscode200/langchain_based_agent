"""lc_factory: agent factory layer over a pinned deepagents-code baseline."""

from lc_factory._env import MIDDLEWARE_REF_ENV, reserve_middleware_ref_env

# MUST stay here, and must stay first. Python runs this module before any
# submodule, and importing any other module in this package reaches
# `deepagents_code.config`, whose module-level `__getattr__` bootstraps
# settings and loads `.env` files into `os.environ`. Reserving after that point
# is a no-op against a value a project `.env` already set. See
# `lc_factory._env.reserve_middleware_ref_env` for the full reasoning.
reserve_middleware_ref_env()

__version__ = "0.1.0"

__all__ = ["MIDDLEWARE_REF_ENV", "__version__", "reserve_middleware_ref_env"]
