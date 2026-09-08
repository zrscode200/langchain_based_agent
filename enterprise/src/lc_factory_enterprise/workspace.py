"""Select the nearest enterprise workspace boundary before client capture."""
from pathlib import Path
from deepagents_code import project_utils as project_utils_module
from deepagents_code._git import find_git_root as upstream_find_git_root


def find_workspace_root(start_path):
    current = Path(start_path).expanduser().resolve()
    if not current.is_dir():
        current = current.parent
    home = Path.home().resolve()
    marker = next((p for p in (current, *current.parents)
                   if p != home and (p / ".deepagents").is_dir()), None)
    git = upstream_find_git_root(current)
    if marker is None:
        return git
    if git is None:
        return marker
    return marker if len(marker.parts) >= len(git.parts) else git


def install_workspace_policy():
    """Select the enterprise resolver for this dedicated server process."""
    project_utils_module.find_git_root = find_workspace_root
