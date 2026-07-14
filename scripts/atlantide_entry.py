"""PyInstaller entry shim.

PyInstaller freezes a *script file*, not a ``package:function`` entry point. This
module is that script: it calls the same ``main`` the ``atlantide`` console script
runs (see ``[project.scripts]`` in ``pyproject.toml``).
"""

from atlantide.cli.main import main

if __name__ == "__main__":
    main()
