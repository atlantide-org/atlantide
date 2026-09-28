"""A small component to exercise :mod:`atlantide.testing` with.

Built on the ``local`` and ``random`` providers, which ship with atlantide, so the
installed-plugin mutability covers it. Nothing is applied.

- ``page``: a ``local.File``; ``path`` is immutable, ``content`` mutable.
- ``token``: a ``random.Uuid`` whose immutable ``keepers`` hold a ``Ref`` to the
  page, so a page change replaces it conditionally.
- ``marker``: an optional ``local.Null``, present with ``marker=True``.
"""

from __future__ import annotations

from atlantide.core import Component, Lifecycle, child
from atlantide.providers.local import File, Null
from atlantide.providers.random import Uuid

REGION = "eu-north-1"
NAME = "site"
#: The children every instance has, by ``child()`` name.
CORE = ("page", "token")
#: The child ``marker=True`` adds.
OPTIONAL = ("marker",)


class Site(Component):
    def __init__(
        self,
        name: str,
        *,
        path: str = "index.html",
        content: str = "hello",
        marker: bool = False,
        protect: bool = False,
    ) -> None:
        guard = Lifecycle(prevent_destroy=True) if protect else None
        self.page = child(File, "page", path=path, content=content, lifecycle=guard)
        self.token = child(Uuid, "token", keepers={"page": self.page.checksum})
        self.marker = child(Null, "marker", triggers={"path": path}) if marker else None
