"""QuickBooks Online (F05; BLUEPRINT §6.2; D-25). Core Accounting REST API only:
the Projects GraphQL API is not used. Read-only: nothing in this package creates
or edits a QuickBooks transaction.

- ``constants``: the pinned minor version, URLs, limits, each with its source.
- ``oauth``: pure helpers (authorization URL, the ``state`` value).
- ``client``: the one module that talks HTTP (token endpoint and API).
- ``tokens``: the locked refresh and the needs-reconnect transition.
- ``connect``: the connect / callback / disconnect service used by ``app.api.qbo``.
"""

SYSTEM = "qbo"
