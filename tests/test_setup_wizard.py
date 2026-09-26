import os
import stat

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
