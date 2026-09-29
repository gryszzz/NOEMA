import os

from noema import local_env
from noema.local_env import load_local_env


def test_local_env_loads_without_overriding_existing(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".env.local"
    path.write_text('A="from-file"\nB="two"\n')
    monkeypatch.setenv("A", "existing")

    loaded = load_local_env(str(path))

    assert loaded["A"] == "from-file"
    assert os.environ["A"] == "existing"
    assert os.environ["B"] == "two"


def test_default_env_path_is_repository_local_from_any_working_directory(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    config = repo / ".env.local"
    config.parent.mkdir()
    config.write_text('NOEMA_RUNTIME_SOURCE="canonical"\n')
    other_directory = tmp_path / "unrelated"
    other_directory.mkdir()
    monkeypatch.chdir(other_directory)
    monkeypatch.setattr(local_env, "DEFAULT_LOCAL_ENV", config)

    loaded = load_local_env()

    assert loaded["NOEMA_RUNTIME_SOURCE"] == "canonical"
    assert local_env.env_local_present()
