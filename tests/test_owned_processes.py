import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

from app import processes


def test_posix_timeout_kills_only_owned_group(monkeypatch):
    killpg = Mock()
    monkeypatch.setattr(processes, "os", SimpleNamespace(name="posix", getpgid=lambda pid: pid, killpg=killpg))
    monkeypatch.setattr(processes, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))
    child = Mock(pid=123)
    child.poll.return_value = None
    child.wait.side_effect = [subprocess.TimeoutExpired("child", 5), 0]
    processes.terminate_owned(child)
    assert [call.args for call in killpg.call_args_list] == [(123, 15), (123, 9)]
    child.terminate.assert_not_called()


def test_posix_inherited_group_is_never_signalled(monkeypatch):
    killpg = Mock()
    monkeypatch.setattr(processes, "os", SimpleNamespace(name="posix", getpgid=lambda pid: 999, killpg=killpg))
    child = Mock(pid=123)
    child.poll.return_value = None
    processes.terminate_owned(child)
    killpg.assert_not_called()
    child.terminate.assert_called_once()


def test_finished_process_is_not_signalled():
    child = Mock()
    child.poll.return_value = 0
    processes.terminate_owned(child)
    child.terminate.assert_not_called()
    child.kill.assert_not_called()
