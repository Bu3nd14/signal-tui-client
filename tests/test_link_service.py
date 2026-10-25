"""Device-link service (server.link) and its link.* operations."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from server.client import ApiError
from server.link import (
    LinkError,
    LinkService,
    list_signal_accounts,
    persist_signal_account,
    resolve_linked_signal_account,
    whatsapp_status_is_linked,
)


class TestSignalLinkPersistence:
    def test_persists_only_linked_account_and_preserves_config(
        self, tmp_path, monkeypatch
    ):
        from protocols import rpc as signal_rpc

        monkeypatch.delenv("SIGNAL_USER_NUMBER", raising=False)
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({"web": {"token": "existing"}}), encoding="utf-8"
        )
        completed = SimpleNamespace(
            returncode=0,
            stdout="+391111111111\n+392222222222\n",
        )

        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            patch.object(
                signal_rpc, "find_signal_cli", return_value=Path("signal-cli")
            ),
            patch("server.link.subprocess.run", return_value=completed) as run_cli,
        ):
            account = resolve_linked_signal_account("+39 222 222 2222")
            persist_signal_account(account)

        assert account == "+392222222222"
        assert json.loads(config_file.read_text(encoding="utf-8")) == {
            "web": {"token": "existing"},
            "user_number": "+392222222222",
        }
        run_cli.assert_called_once_with(
            ["signal-cli", "listAccounts"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_single_linked_account_does_not_require_phone_input(
        self, tmp_path, monkeypatch
    ):
        from protocols import rpc as signal_rpc

        monkeypatch.delenv("SIGNAL_USER_NUMBER", raising=False)
        completed = SimpleNamespace(returncode=0, stdout="+391111111111\n")
        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            patch.object(
                signal_rpc, "find_signal_cli", return_value=Path("signal-cli")
            ),
            patch("server.link.subprocess.run", return_value=completed),
        ):
            account = resolve_linked_signal_account("")

        assert account == "+391111111111"

    def test_new_account_is_selected_from_pre_link_snapshot(
        self, tmp_path, monkeypatch
    ):
        from protocols import rpc as signal_rpc

        monkeypatch.delenv("SIGNAL_USER_NUMBER", raising=False)
        completed = SimpleNamespace(
            returncode=0,
            stdout="+391111111111\n+392222222222\n",
        )
        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            patch.object(
                signal_rpc, "find_signal_cli", return_value=Path("signal-cli")
            ),
            patch("server.link.subprocess.run", return_value=completed),
        ):
            account = resolve_linked_signal_account("", {"+391111111111"})

        assert account == "+392222222222"

    def test_new_account_overrides_stale_entered_phone(self, tmp_path, monkeypatch):
        from protocols import rpc as signal_rpc

        monkeypatch.delenv("SIGNAL_USER_NUMBER", raising=False)
        completed = SimpleNamespace(
            returncode=0,
            stdout="+391111111111\n+392222222222\n",
        )
        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            patch.object(
                signal_rpc, "find_signal_cli", return_value=Path("signal-cli")
            ),
            patch("server.link.subprocess.run", return_value=completed),
        ):
            account = resolve_linked_signal_account("+391111111111", {"+391111111111"})

        assert account == "+392222222222"

    def test_multiple_accounts_without_match_are_rejected(self, tmp_path, monkeypatch):
        from protocols import rpc as signal_rpc

        monkeypatch.delenv("SIGNAL_USER_NUMBER", raising=False)
        completed = SimpleNamespace(
            returncode=0,
            stdout="+391111111111\n+392222222222\n",
        )
        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            patch.object(
                signal_rpc, "find_signal_cli", return_value=Path("signal-cli")
            ),
            patch("server.link.subprocess.run", return_value=completed),
            pytest.raises(RuntimeError, match="multiple linked accounts"),
        ):
            resolve_linked_signal_account("")

        assert not (tmp_path / "config.json").exists()

    def test_environment_account_conflict_is_rejected(self, tmp_path, monkeypatch):
        from protocols import rpc as signal_rpc

        monkeypatch.setenv("SIGNAL_USER_NUMBER", "+399999999999")
        completed = SimpleNamespace(returncode=0, stdout="+391111111111\n")
        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            patch.object(
                signal_rpc, "find_signal_cli", return_value=Path("signal-cli")
            ),
            patch("server.link.subprocess.run", return_value=completed),
            pytest.raises(RuntimeError, match="SIGNAL_USER_NUMBER"),
        ):
            resolve_linked_signal_account("")


class TestSignalLinkPersistenceEdgeCases:
    """Edge cases of the Signal link persistence patch (PR #214)."""

    def test_list_signal_accounts_failure(self, tmp_path, monkeypatch):
        """signal-cli listAccounts with non-zero exit raises RuntimeError."""
        from protocols import rpc as signal_rpc

        monkeypatch.delenv("SIGNAL_USER_NUMBER", raising=False)
        completed = SimpleNamespace(returncode=1, stdout="")
        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            patch.object(
                signal_rpc, "find_signal_cli", return_value=Path("signal-cli")
            ),
            patch("server.link.subprocess.run", return_value=completed),
            pytest.raises(RuntimeError, match="could not list the linked accounts"),
        ):
            list_signal_accounts()

    def test_single_account_matching_previous_snapshot(self, tmp_path, monkeypatch):
        """Only one account, already in the pre-link snapshot: use it."""
        from protocols import rpc as signal_rpc

        monkeypatch.delenv("SIGNAL_USER_NUMBER", raising=False)
        completed = SimpleNamespace(returncode=0, stdout="+391111111111\n")
        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            patch.object(
                signal_rpc, "find_signal_cli", return_value=Path("signal-cli")
            ),
            patch("server.link.subprocess.run", return_value=completed),
        ):
            account = resolve_linked_signal_account("", {"+391111111111"})

        assert account == "+391111111111"

    def test_no_accounts_reported_is_rejected(self, tmp_path, monkeypatch):
        """Empty listAccounts output raises RuntimeError."""
        from protocols import rpc as signal_rpc

        monkeypatch.delenv("SIGNAL_USER_NUMBER", raising=False)
        completed = SimpleNamespace(returncode=0, stdout="")
        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            patch.object(
                signal_rpc, "find_signal_cli", return_value=Path("signal-cli")
            ),
            patch("server.link.subprocess.run", return_value=completed),
            pytest.raises(RuntimeError, match="did not report a linked account"),
        ):
            resolve_linked_signal_account("")

    def test_persist_rejects_invalid_config(self, tmp_path, monkeypatch):
        """Malformed config.json raises RuntimeError without overwriting."""
        from protocols import rpc as signal_rpc

        config_file = tmp_path / "config.json"
        config_file.write_text("{not valid json", encoding="utf-8")
        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            pytest.raises(RuntimeError, match="invalid or unreadable"),
        ):
            persist_signal_account("+391111111111")

        assert config_file.read_text(encoding="utf-8") == "{not valid json"

    def test_persist_rejects_non_dict_config(self, tmp_path, monkeypatch):
        """config.json containing a non-object JSON value raises RuntimeError."""
        from protocols import rpc as signal_rpc

        config_file = tmp_path / "config.json"
        config_file.write_text("[1, 2]", encoding="utf-8")
        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            pytest.raises(RuntimeError, match="must contain a JSON object"),
        ):
            persist_signal_account("+391111111111")

    def test_persist_creates_config_when_missing(self, tmp_path, monkeypatch):
        """A missing config.json is created with just the user_number."""
        from protocols import rpc as signal_rpc

        config_file = tmp_path / "config.json"
        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            patch.object(
                signal_rpc, "find_signal_cli", return_value=Path("signal-cli")
            ),
        ):
            persist_signal_account("+391111111111")

        assert json.loads(config_file.read_text(encoding="utf-8")) == {
            "user_number": "+391111111111"
        }

    def test_persist_cleans_temp_file_on_replace_failure(self, tmp_path, monkeypatch):
        """A failed os.replace leaves no temporary files behind."""
        from protocols import rpc as signal_rpc

        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            patch(
                "server.link.os.replace",
                side_effect=OSError("disk full"),
            ),
            pytest.raises(OSError, match="disk full"),
        ):
            persist_signal_account("+391111111111")

        leftovers = list(tmp_path.glob(".config.json.*"))
        assert leftovers == []


# ─── service ───────────────────────────────────────────────────────────────


def _manager(**backends):
    manager = MagicMock()
    manager.get.side_effect = lambda proto: backends.get(proto)
    return manager


def _service(**backends) -> LinkService:
    manager = _manager(**backends)
    return LinkService(lambda: manager)


def _signal_proc(lines, returncode=None):
    proc = MagicMock()
    proc.stdout.readline.side_effect = [*lines, ""]
    proc.poll.return_value = returncode
    proc.pid = 4242
    return proc


@pytest.fixture
def signal_cli(monkeypatch):
    """No real signal-cli: ``Popen`` returns what the test sets in ``popen.proc``."""
    from protocols import rpc as signal_rpc

    monkeypatch.setattr(
        signal_rpc, "find_signal_cli", lambda: Path("signal-cli"), raising=True
    )
    monkeypatch.setattr(
        "server.link.list_signal_accounts", lambda: ["+391111111111"], raising=True
    )
    with patch("server.link.subprocess.Popen") as popen:
        yield popen


class TestStatus:
    def test_reports_configuration_without_io(self):
        signal = SimpleNamespace(user_number="+391111111111")
        telegram = SimpleNamespace(needs_pairing=True)
        status = _service(
            signal=signal, whatsapp=SimpleNamespace(), telegram=telegram
        ).status()
        assert status == {
            "signal": {"available": True, "account": "+391111111111", "linked": True},
            "whatsapp": {"available": True, "account": "", "linked": None},
            "telegram": {"available": True, "account": "", "linked": False},
        }

    def test_unconfigured_protocols(self):
        status = _service(signal=SimpleNamespace(user_number="")).status()
        assert status["signal"] == {"available": True, "account": "", "linked": False}
        assert status["whatsapp"]["available"] is False
        assert status["telegram"] == {
            "available": False,
            "account": "",
            "linked": None,
        }


class TestSignalFlow:
    def test_start_extracts_link_and_names_the_device(self, signal_cli):
        signal_cli.return_value = _signal_proc(
            ["some error occurred", "sgnl://linkdevice?uuid=abc", "more"]
        )
        qr = _service().start("signal", device_name="My Laptop")
        assert qr == {"kind": "url", "data": "sgnl://linkdevice?uuid=abc"}
        assert signal_cli.call_args.args[0] == ["signal-cli", "link", "-n", "My Laptop"]

    def test_snapshot_failure_does_not_block_the_qr(self, signal_cli, monkeypatch):
        monkeypatch.setattr(
            "server.link.list_signal_accounts",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        signal_cli.return_value = _signal_proc(["sgnl://linkdevice?uuid=abc"])
        service = _service()
        assert service.start("signal")["data"] == "sgnl://linkdevice?uuid=abc"
        assert service._sessions["signal"].accounts_before == set()

    def test_no_link_url_raises_and_kills_the_process(self, signal_cli):
        proc = _signal_proc(["no link here"])
        signal_cli.return_value = proc
        service = _service()
        with pytest.raises(LinkError, match="Could not find Signal link URL"):
            service.start("signal")
        proc.terminate.assert_called_once()
        assert service._sessions == {}

    def test_poll_follows_the_subprocess(self, signal_cli):
        proc = _signal_proc(["sgnl://linkdevice?uuid=abc"])
        signal_cli.return_value = proc
        service = _service()
        service.start("signal")
        assert service.poll("signal")["state"] == "waiting"  # still running
        proc.poll.return_value = 1
        assert service.poll("signal")["state"] == "waiting"  # failed: keeps waiting
        proc.poll.return_value = 0
        assert service.poll("signal") == {
            "state": "finalize",
            "needs_2fa": False,
            "qr": None,
        }

    def test_poll_without_a_link_is_an_error(self):
        with pytest.raises(LinkError, match="No link in progress"):
            _service().poll("signal")

    def test_cancel_terminates_a_running_subprocess_only(self, signal_cli):
        proc = _signal_proc(["sgnl://linkdevice?uuid=abc"])
        signal_cli.return_value = proc
        service = _service()
        service.start("signal")
        service.cancel("signal")
        proc.terminate.assert_called_once()
        assert service._sessions == {}
        service.cancel("signal")  # nothing left: no-op

        done = _signal_proc(["sgnl://linkdevice?uuid=abc"], returncode=0)
        signal_cli.return_value = done
        service.start("signal")
        service.cancel("signal")
        done.terminate.assert_not_called()

    def test_cancel_survives_terminate_failure(self, signal_cli):
        proc = _signal_proc(["sgnl://linkdevice?uuid=abc"])
        proc.terminate.side_effect = Exception("kill failed")
        signal_cli.return_value = proc
        service = _service()
        service.start("signal")
        service.cancel("signal")
        proc.terminate.assert_called_once()

    def test_cancel_while_starting_does_not_leak_the_process(self, signal_cli):
        service = _service()
        proc = MagicMock()
        proc.poll.return_value = None

        def popen(*_a, **_k):
            service.cancel("signal")  # the user closed the screen meanwhile
            return proc

        signal_cli.side_effect = popen
        with pytest.raises(LinkError, match="cancelled"):
            service.start("signal")
        proc.terminate.assert_called_once()

    def test_new_start_replaces_and_stops_the_previous_link(self, signal_cli):
        first = _signal_proc(["sgnl://linkdevice?uuid=1"])
        second = _signal_proc(["sgnl://linkdevice?uuid=2"])
        signal_cli.side_effect = [first, second]
        service = _service()
        service.start("signal")
        service.start("signal")
        first.terminate.assert_called_once()
        second.terminate.assert_not_called()

    def test_abandoned_sessions_are_reaped(self, signal_cli):
        proc = _signal_proc(["sgnl://linkdevice?uuid=1"])
        signal_cli.return_value = proc
        service = _service()
        service.start("signal")
        service._sessions["signal"].created -= 10_000
        with patch("server.link.list_signal_accounts", return_value=[]):
            signal_cli.return_value = _signal_proc(["sgnl://linkdevice?uuid=2"])
            service.start("signal")
        proc.terminate.assert_called_once()

    def test_cancel_all(self, signal_cli):
        proc = _signal_proc(["sgnl://linkdevice?uuid=1"])
        signal_cli.return_value = proc
        service = _service()
        service.start("signal")
        service.cancel_all()
        proc.terminate.assert_called_once()
        assert service._sessions == {}

    def test_finalize_updates_active_backend(self, signal_cli, tmp_path):
        from protocols import rpc as signal_rpc

        backend = SimpleNamespace(user_number="")
        proc = _signal_proc(["sgnl://linkdevice?uuid=1"], returncode=0)
        signal_cli.return_value = proc
        service = _service(signal=backend)
        service.start("signal")
        with (
            patch.object(signal_rpc, "PROJECT_DIR", tmp_path),
            patch(
                "server.link.list_signal_accounts",
                return_value=["+391111111111", "+392222222222"],
            ),
        ):
            # accounts_before is {"+391111111111"} from the pre-link snapshot
            account = service.finalize_signal("")
        assert account == "+392222222222"
        assert backend.user_number == "+392222222222"
        assert json.loads((tmp_path / "config.json").read_text()) == {
            "user_number": "+392222222222"
        }
        assert service._sessions == {}

    def test_finalize_rejects_switch_before_persisting(self):
        backend = SimpleNamespace(user_number="+391111111111")
        service = _service(signal=backend)
        with (
            patch(
                "server.link.resolve_linked_signal_account",
                return_value="+392222222222",
            ),
            patch("server.link.persist_signal_account") as persist,
            pytest.raises(RuntimeError, match="switching Signal accounts"),
        ):
            service.finalize_signal("")
        persist.assert_not_called()
        assert backend.user_number == "+391111111111"

    def test_finalize_without_backend(self):
        service = _service()
        with (
            patch(
                "server.link.resolve_linked_signal_account",
                return_value="+391111111111",
            ),
            patch("server.link.persist_signal_account") as persist,
        ):
            assert service.finalize_signal("") == "+391111111111"
        persist.assert_called_once_with("+391111111111")

    def test_failed_finalize_keeps_the_session(self, signal_cli):
        proc = _signal_proc(["sgnl://linkdevice?uuid=1"], returncode=0)
        signal_cli.return_value = proc
        service = _service(signal=SimpleNamespace(user_number=""))
        service.start("signal")
        with (
            patch(
                "server.link.resolve_linked_signal_account",
                side_effect=RuntimeError("multiple linked accounts found"),
            ),
            pytest.raises(RuntimeError),
        ):
            service.finalize_signal("")
        assert "signal" in service._sessions


class TestWhatsAppFlow:
    def _wa(self, **status):
        rest = MagicMock()
        rest.get_session_status.return_value = status
        return SimpleNamespace(_rest=rest), rest

    def test_working_session_needs_no_qr(self):
        wa, _ = self._wa(status="WORKING", me={"id": "1@c.us"})
        qr = _service(whatsapp=wa).start("whatsapp")
        assert qr == {
            "kind": "info",
            "data": "WhatsApp is already linked and working ✅\nNo QR needed.",
        }

    def test_current_qr_png_is_base64(self):
        wa, rest = self._wa(status="scan_qr")
        rest.get_pairing_qr.return_value = b"\x89PNG"
        qr = _service(whatsapp=wa).start("whatsapp")
        assert qr["kind"] == "png"
        assert base64.b64decode(qr["data"]) == b"\x89PNG"

    def test_text_qr_is_passed_through(self):
        wa, rest = self._wa(status="working", me=None)
        rest.get_pairing_qr.return_value = "fresh-qr"
        assert _service(whatsapp=wa).start("whatsapp") == {
            "kind": "text",
            "data": "fresh-qr",
        }

    @pytest.mark.parametrize("state", ["failed", "stopped", "stop", ""])
    def test_dead_session_is_restarted_with_reset(self, state):
        wa, rest = self._wa(status=state)
        rest.get_fresh_pairing_qr.return_value = "fresh-qr"
        assert _service(whatsapp=wa).start("whatsapp")["data"] == "fresh-qr"
        rest.get_fresh_pairing_qr.assert_called_once_with(reset=True)
        rest.get_pairing_qr.assert_not_called()

    def test_restart_failure(self):
        wa, rest = self._wa(status="failed")
        rest.get_fresh_pairing_qr.return_value = None
        with pytest.raises(LinkError, match=r"Failed to restart session \(status was"):
            _service(whatsapp=wa).start("whatsapp")

    def test_falls_back_to_a_fresh_qr_without_reset(self):
        wa, rest = self._wa(status="pending")
        rest.get_pairing_qr.return_value = None
        rest.get_fresh_pairing_qr.return_value = b"png"
        assert _service(whatsapp=wa).start("whatsapp")["kind"] == "png"
        rest.get_fresh_pairing_qr.assert_called_once_with(reset=False)

    def test_no_qr_available(self):
        wa, rest = self._wa(status="pending")
        rest.get_pairing_qr.return_value = None
        rest.get_fresh_pairing_qr.return_value = None
        with pytest.raises(LinkError, match="No QR available"):
            _service(whatsapp=wa).start("whatsapp")

    @pytest.mark.parametrize(
        "backends", [{}, {"whatsapp": SimpleNamespace(_rest=None)}]
    )
    def test_backend_missing(self, backends):
        with pytest.raises(LinkError, match="WhatsApp backend not available"):
            _service(**backends).start("whatsapp")

    def _started(self, **status):
        wa, rest = self._wa(status="scan_qr")
        rest.get_pairing_qr.return_value = "qr"
        service = _service(whatsapp=wa)
        with patch("server.link.time.time", return_value=1000.0):
            service.start("whatsapp")
        rest.get_session_status.return_value = status
        return service, rest

    def test_poll_done_when_working_with_identity(self):
        service, _ = self._started(status="working", me={"id": "1@c.us"})
        assert service.poll("whatsapp")["state"] == "done"

    def test_poll_working_without_identity_is_not_done(self):
        service, _ = self._started(status="working", me=None)
        assert service.poll("whatsapp")["state"] == "waiting"

    def test_qr_is_not_renewed_before_a_minute(self):
        service, rest = self._started(status="scan_qr")
        with patch("server.link.time.time", return_value=1059.0):
            assert service.poll("whatsapp")["qr"] is None
        rest.get_fresh_pairing_qr.assert_not_called()

    def test_qr_is_renewed_after_a_minute_without_reset(self):
        service, rest = self._started(status="scan_qr")
        rest.get_fresh_pairing_qr.return_value = b"newpng"
        with patch("server.link.time.time", return_value=1060.0):
            result = service.poll("whatsapp")
        assert result["state"] == "waiting"
        assert result["qr"]["kind"] == "png"
        rest.get_fresh_pairing_qr.assert_called_once_with(reset=False)
        # the clock restarted: no second renewal right away
        with patch("server.link.time.time", return_value=1061.0):
            assert service.poll("whatsapp")["qr"] is None

    def test_qr_not_renewed_in_other_states(self):
        service, rest = self._started(status="starting")
        with patch("server.link.time.time", return_value=5000.0):
            assert service.poll("whatsapp")["qr"] is None
        rest.get_fresh_pairing_qr.assert_not_called()

    def test_missing_fresh_qr_is_shown_as_text(self):
        service, rest = self._started(status="pending")
        rest.get_fresh_pairing_qr.return_value = None
        with patch("server.link.time.time", return_value=2000.0):
            assert service.poll("whatsapp")["qr"] == {
                "kind": "text",
                "data": "ERROR: No fresh QR available",
            }

    def test_renewal_failure_retries_on_the_next_poll(self):
        service, rest = self._started(status="scan_qr")
        rest.get_fresh_pairing_qr.side_effect = [RuntimeError("waha down"), b"png"]
        with patch("server.link.time.time", return_value=2000.0):
            assert service.poll("whatsapp")["qr"] is None
            assert service.poll("whatsapp")["qr"]["kind"] == "png"

    def test_poll_without_rest_is_waiting(self):
        wa = SimpleNamespace(_rest=MagicMock())
        wa._rest.get_pairing_qr.return_value = "qr"
        wa._rest.get_session_status.return_value = {"status": "scan_qr"}
        service = _service(whatsapp=wa)
        service.start("whatsapp")
        wa._rest = None
        assert service.poll("whatsapp")["state"] == "waiting"


class TestTelegramFlow:
    def _tg(self, **attrs):
        tb = MagicMock(_connected=False, _needs_2fa=False)
        for key, value in attrs.items():
            setattr(tb, key, value)
        return tb

    def test_start_returns_the_login_url(self):
        tb = self._tg()
        tb.get_pairing_qr.return_value = "tg://login?token=abc"
        assert _service(telegram=tb).start("telegram") == {
            "kind": "url",
            "data": "tg://login?token=abc",
        }

    def test_already_logged_in_is_info(self):
        tb = self._tg()
        tb.get_pairing_qr.return_value = "INFO: Already logged in"
        assert _service(telegram=tb).start("telegram")["kind"] == "info"

    @pytest.mark.parametrize(
        ("raw", "message"),
        [("ERROR: network down", "network down"), (None, "No QR data")],
    )
    def test_start_errors(self, raw, message):
        tb = self._tg()
        tb.get_pairing_qr.return_value = raw
        with pytest.raises(LinkError, match=message):
            _service(telegram=tb).start("telegram")

    def test_missing_backend(self):
        with pytest.raises(LinkError, match="Telegram backend not available"):
            _service().start("telegram")

    def _started(self, tb):
        tb.get_pairing_qr.return_value = "tg://login?token=1"
        service = _service(telegram=tb)
        with patch("server.link.time.time", return_value=1000.0):
            service.start("telegram")
        tb.get_pairing_qr.reset_mock()
        return service

    def test_poll_done_when_connected(self):
        tb = self._tg()
        service = self._started(tb)
        tb._connected = True
        assert service.poll("telegram")["state"] == "done"

    def test_2fa_is_reported_once(self):
        tb = self._tg()
        service = self._started(tb)
        with patch("server.link.time.time", return_value=1001.0):
            assert service.poll("telegram")["needs_2fa"] is False
            tb._needs_2fa = True
            assert service.poll("telegram")["needs_2fa"] is True
            assert service.poll("telegram")["needs_2fa"] is False

    def test_qr_is_renewed_after_a_minute_and_rearms_2fa(self):
        tb = self._tg(_needs_2fa=True)
        service = self._started(tb)
        tb.get_pairing_qr.return_value = "tg://login?token=2"
        with patch("server.link.time.time", return_value=1001.0):
            assert service.poll("telegram")["needs_2fa"] is True
        with patch("server.link.time.time", return_value=1060.0):
            result = service.poll("telegram")
        assert result["qr"] == {"kind": "url", "data": "tg://login?token=2"}
        assert result["needs_2fa"] is False
        with patch("server.link.time.time", return_value=1061.0):
            assert service.poll("telegram")["needs_2fa"] is True  # re-armed

    def test_no_renewal_before_a_minute(self):
        tb = self._tg()
        service = self._started(tb)
        with patch("server.link.time.time", return_value=1059.0):
            assert service.poll("telegram")["qr"] is None
        tb.get_pairing_qr.assert_not_called()

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("INFO: Already logged in", {"kind": "info", "data": " Already logged in"}),
            ("ERROR: flood wait", {"kind": "error", "data": "flood wait"}),
            (None, {"kind": "error", "data": "No QR data"}),
        ],
    )
    def test_renewal_messages(self, raw, expected):
        tb = self._tg()
        service = self._started(tb)
        tb.get_pairing_qr.return_value = raw
        with patch("server.link.time.time", return_value=2000.0):
            assert service.poll("telegram")["qr"] == expected

    def test_renewal_failure_retries_on_the_next_poll(self):
        tb = self._tg()
        service = self._started(tb)
        tb.get_pairing_qr.side_effect = [RuntimeError("boom"), "tg://login?token=3"]
        with patch("server.link.time.time", return_value=2000.0):
            assert service.poll("telegram")["qr"] is None
            assert service.poll("telegram")["qr"]["data"] == "tg://login?token=3"

    @pytest.mark.parametrize("ok", [True, False])
    def test_submit_2fa(self, ok):
        tb = self._tg()
        tb.complete_2fa.return_value = ok
        assert _service(telegram=tb).submit_2fa("secret") is ok
        tb.complete_2fa.assert_called_once_with("secret")

    def test_submit_2fa_without_backend(self):
        with pytest.raises(LinkError, match="Telegram backend not available"):
            _service().submit_2fa("secret")


def test_whatsapp_working_requires_authenticated_identity():
    assert whatsapp_status_is_linked({"status": "WORKING", "me": {"id": "123@c.us"}})
    assert not whatsapp_status_is_linked({"status": "WORKING", "me": None})
    assert not whatsapp_status_is_linked({"status": "FAILED", "me": {"id": "1@c.us"}})


# ─── operations ────────────────────────────────────────────────────────────


class _Host:
    def __init__(self, manager, reconnect=None):
        self.manager = manager
        self.bus = None
        if reconnect is not None:
            self.reconnect = reconnect


def _client(manager, reconnect=None):
    from server.api import ChatAPI
    from server.client import InProcessClient

    host = _Host(manager, reconnect)
    host.api = ChatAPI(host)  # type: ignore[attr-defined]
    return InProcessClient(host)  # type: ignore[arg-type]


class TestOps:
    def test_status_shape(self):
        manager = _manager(
            signal=SimpleNamespace(user_number="+391"), whatsapp=SimpleNamespace()
        )
        out = _client(manager).call("link.status")
        assert out["protocols"]["signal"] == {
            "available": True,
            "account": "+391",
            "linked": True,
        }
        assert out["protocols"]["whatsapp"]["available"] is True
        assert out["protocols"]["telegram"]["available"] is False

    def test_start_poll_cancel_round_trip(self, signal_cli):
        proc = _signal_proc(["sgnl://linkdevice?uuid=abc"], returncode=0)
        signal_cli.return_value = proc
        client = _client(_manager())
        out = client.call("link.start", {"protocol": "signal", "device_name": "Dev"})
        assert out == {"qr": {"kind": "url", "data": "sgnl://linkdevice?uuid=abc"}}
        assert client.call("link.poll", {"protocol": "signal"}) == {
            "state": "finalize",
            "needs_2fa": False,
            "qr": None,
        }
        assert client.call("link.cancel", {"protocol": "signal"}) == {"ok": True}
        with pytest.raises(ApiError) as exc:
            client.call("link.poll", {"protocol": "signal"})
        assert exc.value.code == "conflict"

    def test_default_device_name(self, signal_cli):
        signal_cli.return_value = _signal_proc(["sgnl://linkdevice?uuid=abc"])
        _client(_manager()).call("link.start", {"protocol": "signal"})
        assert signal_cli.call_args.args[0][-1] == "Signal-TUI-Client"

    def test_start_errors_keep_the_user_facing_text(self, signal_cli):
        signal_cli.return_value = _signal_proc(["nothing"])
        client = _client(_manager())
        with pytest.raises(ApiError) as exc:
            client.call("link.start", {"protocol": "signal"})
        assert exc.value.code == "backend_error"
        assert exc.value.message.startswith("Could not find Signal link URL")

        with pytest.raises(ApiError) as exc:
            client.call("link.start", {"protocol": "whatsapp"})
        assert exc.value.code == "backend_down"
        assert exc.value.message == "WhatsApp backend not available"

    def test_unknown_protocol_is_invalid(self):
        with pytest.raises(ApiError) as exc:
            _client(_manager()).call("link.start", {"protocol": "irc"})
        assert exc.value.code == "invalid_request"

    def test_finalize_error_is_a_backend_error(self):
        client = _client(_manager(signal=SimpleNamespace(user_number="")))
        with (
            patch(
                "server.link.resolve_linked_signal_account",
                side_effect=RuntimeError("signal-cli did not report a linked account"),
            ),
            pytest.raises(ApiError) as exc,
        ):
            client.call("link.finalize", {"protocol": "signal"})
        assert exc.value.code == "backend_error"
        assert exc.value.message == "signal-cli did not report a linked account"

    def test_finalize_returns_the_account(self):
        client = _client(_manager(signal=SimpleNamespace(user_number="")))
        with (
            patch(
                "server.link.resolve_linked_signal_account",
                return_value="+391111111111",
            ),
            patch("server.link.persist_signal_account"),
        ):
            out = client.call("link.finalize", {"protocol": "signal", "phone": "+39"})
        assert out == {"account": "+391111111111"}

    def test_submit_2fa(self):
        tb = MagicMock()
        tb.complete_2fa.return_value = False
        client = _client(_manager(telegram=tb))
        assert client.call("link.submit_2fa", {"password": "x"}) == {"ok": False}

    def test_reconnect_goes_to_the_host(self):
        reconnect = MagicMock()
        client = _client(_manager(), reconnect)
        assert client.call("link.reconnect", {"protocols": ["telegram", "signal"]}) == {
            "ok": True
        }
        reconnect.assert_called_once_with({"telegram", "signal"})

    def test_reconnect_none_is_still_ok(self):
        reconnect = MagicMock()
        _client(_manager(), reconnect).call("link.reconnect", {"protocols": []})
        reconnect.assert_called_once_with(set())

    def test_reconnect_rejects_unknown_protocols(self):
        with pytest.raises(ApiError) as exc:
            _client(_manager(), MagicMock()).call(
                "link.reconnect", {"protocols": ["irc"]}
            )
        assert exc.value.code == "invalid_request"

    def test_reconnect_unsupported_host(self):
        with pytest.raises(ApiError) as exc:
            _client(_manager()).call("link.reconnect", {"protocols": ["signal"]})
        assert exc.value.code == "not_supported"

    def test_reconnect_only_reconnects_the_named_backends(self):
        """Through a real core: only the touched protocol's worker is spawned."""
        from server.bus import EventBus
        from server.core import ChatCore

        with (
            patch("server.core.BackendManager"),
            patch(
                "server.core.SignalBackend", return_value=MagicMock(protocol="signal")
            ),
            patch("server.core.whatsapp_enabled", return_value=False),
            patch("server.core.telegram_enabled", return_value=False),
        ):
            core = ChatCore(EventBus())
        core.telegram_backend = MagicMock()
        core.whatsapp_backend = MagicMock()
        spawned = []
        core._spawn = lambda _target, name: spawned.append(name)  # type: ignore[method-assign]
        from server.client import InProcessClient

        client = InProcessClient(core)
        client.call("link.reconnect", {"protocols": ["telegram"]})
        assert spawned == ["core-connect-telegram"]
        client.call("link.reconnect", {"protocols": []})
        assert spawned == ["core-connect-telegram"]


def test_shutdown_cancels_running_links(signal_cli):
    from server.bus import EventBus
    from server.core import ChatCore

    proc = _signal_proc(["sgnl://linkdevice?uuid=1"])
    signal_cli.return_value = proc
    with (
        patch("server.core.BackendManager"),
        patch("server.core.SignalBackend", return_value=MagicMock(protocol="signal")),
        patch("server.core.whatsapp_enabled", return_value=False),
        patch("server.core.telegram_enabled", return_value=False),
    ):
        core = ChatCore(EventBus())
    core.api.link._manager = lambda: _manager()
    core.api.link.start("signal")
    core.stop = MagicMock()  # type: ignore[method-assign]
    with patch("protocols.db._prune_cache"):
        core.shutdown()
    proc.terminate.assert_called_once()
