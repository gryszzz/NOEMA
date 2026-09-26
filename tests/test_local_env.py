import os

from noema.local_env import load_local_env


def test_local_env_loads_without_overriding_existing(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".env.local"
    path.write_text('A="from-file"\nB="two"\n')
    monkeypatch.setenv("A", "existing")

    loaded = load_local_env(str(path))

    assert loaded["A"] == "from-file"
    assert os.environ["A"] == "existing"
    assert os.environ["B"] == "two"
