"""Ctrl+C must reach the TUI even when SIGINT lands on a non-main thread.

A plain ``signal.signal`` handler runs only when the main thread executes
Python code again; an idle Textual loop can block in ``select()`` forever, so
``SignalTUI`` re-installs the handler on the asyncio loop (wakeup fd).
"""

import asyncio
import signal
import threading

from tui.app import SignalTUI


async def test_sigint_from_worker_thread_wakes_the_loop() -> None:
    app = SignalTUI.__new__(SignalTUI)
    fired = asyncio.Event()
    app._sigint_callback = fired.set
    loop = asyncio.get_running_loop()
    app._install_sigint_on_loop()
    try:
        # Deliver SIGINT to a worker thread, not the main one.
        target = threading.Thread(target=threading.Event().wait, args=(2,))
        target.start()
        assert target.ident is not None
        signal.pthread_kill(target.ident, signal.SIGINT)
        await asyncio.wait_for(fired.wait(), 2)
        target.join()
    finally:
        loop.remove_signal_handler(signal.SIGINT)


async def test_no_callback_installs_nothing() -> None:
    app = SignalTUI.__new__(SignalTUI)
    app._sigint_callback = None
    before = signal.getsignal(signal.SIGINT)
    app._install_sigint_on_loop()
    assert signal.getsignal(signal.SIGINT) is before
