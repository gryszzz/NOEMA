import subprocess

from noema.heartbeats import _live_checkout_branch


def _repo(tmp_path, branch):
    subprocess.run(["git", "init", "-q", "-b", branch, str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
                    "--allow-empty", "-m", "x"], check=True)
    (tmp_path / "data").mkdir()
    return str(tmp_path / "data" / "noema.db")


def test_main_is_fresh(tmp_path):
    r = _live_checkout_branch(_repo(tmp_path, "main"))
    assert r["success"] and not r["error"]


def test_other_branch_is_red(tmp_path):
    r = _live_checkout_branch(_repo(tmp_path, "feature"))
    assert r["success"] is None and "not main" in r["error"]
