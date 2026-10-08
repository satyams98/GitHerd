from __future__ import annotations

import os
import subprocess
from pathlib import Path

_NON_INTERACTIVE_GUARDS = ("GIT_TERMINAL_PROMPT", "GCM_INTERACTIVE")


def interactive_env() -> dict[str, str]:
    """The normal environment minus the guards that keep git from prompting."""
    env = os.environ.copy()
    for name in _NON_INTERACTIVE_GUARDS:
        env.pop(name, None)
    return env


def run_git_interactive(repo: Path, *args: str) -> int:
    """Run git with the real terminal attached so it can ask for credentials.

    Used only for the explicit "authenticate" hand-off; credentials never pass
    through githerd, the LLM or any outcome.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args], env=interactive_env(), check=False
        )
    except FileNotFoundError:
        return 127
    return completed.returncode
