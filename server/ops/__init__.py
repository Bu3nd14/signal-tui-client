"""Built-in operations; importing this package registers them.

Convention: one module per domain (``meta``, ``contacts``, ``chats``, ...),
each declaring ``@operation(...)`` handlers whose request/response models live
in :mod:`server.models_api`.
"""

from server.ops import contacts, meta  # noqa: F401
