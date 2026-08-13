"""Build-time generation of the shell tab-completion files shipped in the wheel.

swapsack completes through argcomplete, but argcomplete's completer does
nothing until it has been registered with the shell. The documented way to do
that is a per-command line in a shell rc file::

    eval "$(register-python-argcomplete swapsack)"

which is one manual step too many for something that should just work. Shipping
the very same snippet as package data removes the step entirely:
bash-completion's dynamic loader resolves the real path of the command being
completed and probes ``<prefix>/share/bash-completion/completions/<cmd>``, so an
installed swapsack finds its own completion file whether it lives in a venv, in
``pipx``'s or ``uv tool``'s private tree, or under ``pip install --user``. zsh
autoloads the same file from ``<prefix>/share/zsh/site-functions/_<cmd>``: the
snippet opens with ``#compdef swapsack`` and branches on ``$ZSH_VERSION``, so a
single generated file serves both shells.

The snippet is generated here rather than committed so it can never drift from
the argcomplete actually pinned in the build, and so no generated artifact
needs to live in the working tree. It is self-contained shell code — it execs
``swapsack`` itself in completion mode — so nothing requires
``register-python-argcomplete`` to be present at runtime.

Users who prefer argcomplete's global hook are still served: ``cli.py`` carries
the ``PYTHON_ARGCOMPLETE_OK`` marker that hook insists on.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

SCRIPT = "swapsack"

# Basename to install to, keyed by the wheel shared-data destination. The paths
# are fixed by the shells, not by us: bash-completion looks the file up under
# the command's own name, zsh under an underscore-prefixed one.
SHARED_DATA = {
    SCRIPT: f"share/bash-completion/completions/{SCRIPT}",
    f"_{SCRIPT}": f"share/zsh/site-functions/_{SCRIPT}",
}


def completion_snippet(script: str = SCRIPT) -> str:
    """Return the bash/zsh registration snippet for ``script``.

    This is exactly what ``register-python-argcomplete <script>`` prints; going
    through the library avoids depending on that console script existing in the
    build environment.
    """
    from argcomplete.shell_integration import shellcode

    return shellcode([script])


class CompletionBuildHook(BuildHookInterface):
    """Write the completion files to a temp dir and add them to the wheel."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel":
            return

        self._staging = Path(tempfile.mkdtemp(prefix="swapsack-completions-"))
        snippet = completion_snippet()
        for basename, destination in SHARED_DATA.items():
            source = self._staging / basename
            source.write_text(snippet)
            # Absolute sources are fine here; hatchling only joins relative ones
            # against the project root.
            build_data["shared_data"][str(source)] = destination

    def finalize(
        self, version: str, build_data: dict[str, Any], artifact_path: str
    ) -> None:
        staging = getattr(self, "_staging", None)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
