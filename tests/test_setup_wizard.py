import os
import stat

import pytest

from noema.setup_wizard import write_local_env


def test_local_setup_file_is_owner_only(tmp_path) -> None:
    path = tmp_path / ".env.local"
    write_local_env(
        {
            "NOEMA_FOUNDRY_API_KEY": "secret",
            "NOEMA_FOUNDRY_DEPLOYMENT": "astra",
        },
        path=str(path),
    )
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR
    text = path.read_text()
    assert 'NOEMA_FOUNDRY_API_KEY="secret"' in text


def test_local_setup_is_not_environment_export(tmp_path) -> None:
    path = tmp_path / ".env.local"
    os.environ.pop("NOEMA_TEST_SETUP", None)
    write_local_env({"NOEMA_TEST_SETUP": "x"}, path=str(path))
    assert os.getenv("NOEMA_TEST_SETUP") is None


def test_setup_refuses_symlink_to_another_file(tmp_path) -> None:
    from noema.setup_wizard import write_local_env

    original = tmp_path / "original.txt"
    original.write_text("untouched")
    path = tmp_path / ".env.local"
    path.symlink_to(original)
    with pytest.raises(OSError):
        write_local_env({"NOEMA_FOUNDRY_API_KEY": "secret"}, path=str(path))
    assert original.read_text() == "untouched"


def test_setup_rejects_newline_in_configuration_value(tmp_path) -> None:
    path = tmp_path / ".env.local"
    with pytest.raises(ValueError):
        write_local_env(
            {"NOEMA_FOUNDRY_API_KEY": "x\nNOEMA_ALLOW_LIVE_ORDERS=1"}, path=str(path)
        )
    assert not path.exists()
