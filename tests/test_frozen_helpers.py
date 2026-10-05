from unittest.mock import patch
import pytest
from app.core.utils.frozen_helpers import dispatch_frozen_helper


def test_loky_tracker_runs_without_loading_desktop():
    with patch('app.core.utils.frozen_helpers._run_tracker') as tracker:
        assert dispatch_frozen_helper(['-B', '-c', 'from joblib.externals.loky.backend.resource_tracker import main; main(12, False)'])
        tracker.assert_called_once_with(12, 0)


@pytest.mark.parametrize('command', [
    'print("arbitrary code")',
    'from joblib.externals.loky.backend.resource_tracker import main; main(12, False); print("extra")',
    'from joblib.externals.loky.backend.resource_tracker import main; main(__import__("os"), False)',
])
def test_unrecognized_or_modified_child_code_is_not_executed(command):
    with patch('app.core.utils.frozen_helpers._run_tracker') as tracker:
        assert not dispatch_frozen_helper(['-c', command])
        tracker.assert_not_called()


def test_normal_desktop_arguments_are_not_child_processes():
    assert not dispatch_frozen_helper(['--self-test', '--backend', 'mlx'])
