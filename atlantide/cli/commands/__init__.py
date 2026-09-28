"""One module (or sub-package) per command or command group.

Command modules define plain functions; :mod:`atlantide.cli.app` registers them,
so ``--help`` order and command names are decided in one place. They share only
the ``cli`` infrastructure (options, wiring, errors) and :mod:`atlantide.cli.views`.
"""
