#!/usr/bin/env python3
"""Megachat entry point.

``megachat serve [...]`` runs the headless server (core + socket + web
adapter, no TUI).  Anything else starts the TUI, which attaches to a running
server or spawns ``megachat serve`` (``--connect PATH`` attaches to a given
socket, ``--in-process`` runs the core inside the TUI; design §5).  The TUI is
imported lazily so the serve path never loads Textual.
"""

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "serve":
        from server.main import serve

        return serve(args[1:])
    from signal_tui import main as tui_main

    return tui_main(args)


if __name__ == "__main__":
    sys.exit(main())
