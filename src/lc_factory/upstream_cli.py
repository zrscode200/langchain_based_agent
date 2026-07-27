"""Import boundary for the CLI entry point, kept separate on purpose.

`deepagents_code.__getattr__` serves `cli_main` lazily so that importing a
submodule does not pull in the whole CLI/client stack. Re-exporting it from
the main boundary module would defeat that: `lc_factory.server_graph` imports
the boundary, so the `langgraph dev` subprocess would load ~2300 modules
(and `main.py`'s module-level warning filters) instead of the ~160 upstream's
own server loads.

Only `lc_factory.tui` imports this module.
"""

from deepagents_code import cli_main

__all__ = ["cli_main"]
