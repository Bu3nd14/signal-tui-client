#!/usr/bin/env python3
"""Unified launcher for Signal TUI Client — replaces every ``.sh`` script.

    python3 launcher.py install [options]            # was install.sh
    python3 launcher.py aliases                       # was install.sh --aliases
    python3 launcher.py whatsapp start|stop [--no-wait]   # was scripts/start_whatsapp.sh
    python3 launcher.py backend-restart [--no-wait]   # was scripts/restart_backend.sh
    python3 launcher.py server start|stop|status      # was scripts/start_on_server.sh
    python3 launcher.py handover to-server|to-local|status  # was scripts/tui_handover.sh
    python3 launcher.py profile pyspy|strace [duration]     # was profiling/run_*.sh
    python3 launcher.py test                           # was tests/run_regression_tests.sh

``install``, ``whatsapp start``, ``backend-restart``, ``server start`` and
``handover to-server``/``to-local`` all start WAHA with CPU/RAM caps
(docker-compose.resources.yml) applied by default; pass --no-docker-limits
to any of them to skip that (e.g. on a host that doesn't delegate the
memory cgroup controller, such as an unprivileged Proxmox/LXC container
without nesting=1).

Kept dependency-free (stdlib only) so ``install`` can run before anything in
requirements.txt is on disk. See ``launcher/`` for the implementation of
each command.
"""

from __future__ import annotations

import argparse
import sys

from launcher import backend, handover, install, server, test, whatsapp
from launcher import profile as profile_mod


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launcher.py",
        description=(
            "Signal TUI Client — unified launcher (replaces install.sh, "
            "scripts/*.sh, profiling/*.sh, tests/run_regression_tests.sh)"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser(
        "install", help="Full installer (signal-cli, venv, deps, aliases)"
    )
    p_install.add_argument(
        "--no-venv", action="store_true", help="Use the system Python instead of a venv"
    )
    p_install.add_argument(
        "--version",
        default="",
        metavar="X.Y.Z",
        help="Download a specific signal-cli version",
    )
    p_install.add_argument(
        "--skip-signal-cli", action="store_true", help="Don't download signal-cli"
    )
    p_install.add_argument(
        "--update", action="store_true", help="Update signal-cli to latest"
    )
    p_install.add_argument(
        "--whatsapp", action="store_true", help="Also start the WAHA WhatsApp HTTP API"
    )
    p_install.add_argument(
        "--check-whatsapp",
        action="store_true",
        help="Only check WhatsApp prerequisites",
    )
    p_install.add_argument(
        "--no-web",
        action="store_true",
        help="Don't install optional Web UI dependencies",
    )
    p_install.add_argument(
        "--aliases",
        action="store_true",
        help="Install only the web reader shell aliases",
    )
    p_install.add_argument(
        "--no-docker-limits",
        action="store_true",
        help=(
            "Start WAHA without CPU/RAM caps (default: docker-compose.resources.yml "
            "is applied on top). Use this if your host doesn't delegate the memory "
            "cgroup controller, e.g. an unprivileged Proxmox/LXC container without "
            "nesting=1."
        ),
    )

    sub.add_parser("aliases", help="Install only the web reader shell aliases")

    p_wa = sub.add_parser(
        "whatsapp", help="Start/stop the WAHA WhatsApp HTTP API container"
    )
    wa_sub = p_wa.add_subparsers(dest="wa_command", required=True)
    p_wa_start = wa_sub.add_parser("start")
    p_wa_start.add_argument("--no-wait", action="store_true")
    p_wa_start.add_argument(
        "--no-docker-limits",
        action="store_true",
        help="Start WAHA without CPU/RAM caps (default: applied)",
    )
    wa_sub.add_parser("stop")

    p_backend = sub.add_parser(
        "backend-restart",
        help="Restart the signal-cli daemon (configured account) and WAHA",
    )
    p_backend.add_argument("--no-wait", action="store_true")
    p_backend.add_argument(
        "--no-docker-limits",
        action="store_true",
        help="Restart WAHA without CPU/RAM caps (default: applied)",
    )

    p_server = sub.add_parser(
        "server", help="Start/stop/status the TUI on this machine (tmux + WAHA)"
    )
    server_sub = p_server.add_subparsers(dest="server_command", required=True)
    p_server_start = server_sub.add_parser("start")
    p_server_start.add_argument(
        "--no-docker-limits",
        action="store_true",
        help="Start WAHA without CPU/RAM caps (default: applied)",
    )
    server_sub.add_parser("stop")
    server_sub.add_parser("status")

    p_handover = sub.add_parser(
        "handover",
        help="Move the TUI session between this machine and the remote server",
    )
    handover_sub = p_handover.add_subparsers(dest="handover_command", required=True)
    p_handover_to_server = handover_sub.add_parser("to-server")
    p_handover_to_server.add_argument(
        "--no-docker-limits",
        action="store_true",
        help="Start WAHA on the remote server without CPU/RAM caps (default: applied)",
    )
    p_handover_to_local = handover_sub.add_parser("to-local")
    p_handover_to_local.add_argument(
        "--no-docker-limits",
        action="store_true",
        help="Start WAHA locally without CPU/RAM caps (default: applied)",
    )
    handover_sub.add_parser("status")

    p_profile = sub.add_parser("profile", help="CPU (py-spy) / I/O (strace) profiling")
    profile_sub = p_profile.add_subparsers(dest="profile_command", required=True)
    p_pyspy = profile_sub.add_parser("pyspy")
    p_pyspy.add_argument("duration", nargs="?", type=int, default=120)
    p_strace = profile_sub.add_parser("strace")
    p_strace.add_argument("duration", nargs="?", type=int, default=120)

    sub.add_parser("test", help="Run the regression test suite (pytest, isolated venv)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "install":
        return install.run(args)
    if args.command == "aliases":
        return install.run_aliases_only()
    if args.command == "whatsapp":
        if args.wa_command == "start":
            return whatsapp.start(
                no_wait=args.no_wait, docker_limits=not args.no_docker_limits
            )
        return whatsapp.stop()
    if args.command == "backend-restart":
        return backend.run(no_wait=args.no_wait, docker_limits=not args.no_docker_limits)
    if args.command == "server":
        if args.server_command == "start":
            return server.start_all(docker_limits=not args.no_docker_limits)
        if args.server_command == "stop":
            return server.stop_all()
        return server.status()
    if args.command == "handover":
        if args.handover_command == "to-server":
            return handover.to_server(docker_limits=not args.no_docker_limits)
        if args.handover_command == "to-local":
            return handover.to_local(docker_limits=not args.no_docker_limits)
        return handover.status()
    if args.command == "profile":
        if args.profile_command == "pyspy":
            return profile_mod.run_pyspy(args.duration)
        return profile_mod.run_strace(args.duration)
    if args.command == "test":
        return test.run()

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
