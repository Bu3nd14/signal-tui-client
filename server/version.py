"""Version identifiers a server announces to its clients (``rpc.hello``,
the runtime file)."""

#: Human-readable server version (informational; clients must not parse it).
SERVER_VERSION = "0.1.0"

#: Wire protocol version of the Unix-socket adapter; bumped on any
#: incompatible change of its framing, methods or error mapping.
API_VERSION = 1
