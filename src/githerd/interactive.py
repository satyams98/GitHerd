from __future__ import annotations

import os
import subprocess
from pathlib import Path

_NON_INTERACTIVE_GUARDS = ("GIT_TERMINAL_PROMPT", "GCM_INTERACTIVE")
_ALLOWED_SUBCOMMANDS = frozenset({"fetch", "pull"})


def interactive_env() -> dict[str, str]:
    """The normal environment minus the guards that keep git from prompting."""
    env = os.environ.copy()
    for name in _NON_INTERACTIVE_GUARDS:
        env.pop(name, None)
    return env


def run_git_interactive(repo: Path, *args: str) -> int:
    """Run git with the real terminal attached so it can ask for credentials.

    Used only for the explicit "authenticate" hand-off; credentials never pass
    through githerd, the LLM or any outcome. Because the non-interactive guards are
    lifted, only ``fetch`` and ``pull`` may be run this way (``ValueError`` otherwise).
    """
    if not args or args[0] not in _ALLOWED_SUBCOMMANDS:
        raise ValueError("interactive git hand-off is limited to fetch and pull")
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args], env=interactive_env(), check=False
        )
    except FileNotFoundError:
        return 127
    return completed.returncode
