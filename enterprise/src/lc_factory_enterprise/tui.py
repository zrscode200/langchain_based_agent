"""Enterprise CLI over OG; client customizations last only for this launch."""
import sys
from contextlib import contextmanager


@contextmanager
def client_adaptations():
    from lc_factory.upstream import server_manager_module
    from . import launch
    from .chrome import _QuietStatusBar, _RebrandedBanner
    from .replay import _ReplayAwareSubagentPanel
    from .upstream_cli import app_module
    from .workspace import find_workspace_root, project_utils_module

    replacements = [
        (server_manager_module, "_scaffold_workspace", launch.scaffold_workspace),
        (project_utils_module, "find_git_root", find_workspace_root),
        (app_module, "StatusBar", _QuietStatusBar),
        (app_module, "WelcomeBanner", _RebrandedBanner),
        (app_module, "SubagentPanel", _ReplayAwareSubagentPanel),
    ]
    originals = [(obj, name, getattr(obj, name)) for obj, name, _ in replacements]
    try:
        for obj, name, value in replacements:
            setattr(obj, name, value)
        yield
    finally:
        for obj, name, value in reversed(originals):
            setattr(obj, name, value)


def main():
    if sys.argv[1:] == ["--version"]:
        from . import __version__
        print(f"DDT-agent {__version__} (OG enterprise integration)")
        return
    from .maintenance import disabled_automatic_maintenance
    with disabled_automatic_maintenance():
        from .upstream_cli import cli_main
        from lc_factory.runtime_config import client_runtime_environment
        from lc_factory.skill_policy import client_skill_policy
        from lc_factory.background_ui import client_background_tasks
        from lc_factory.web_launcher import client_web_frontend
        with client_adaptations(), client_runtime_environment(), client_skill_policy(), client_background_tasks(), client_web_frontend():
            cli_main()
