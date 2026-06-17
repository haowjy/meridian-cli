from pathlib import Path

from tests.support.git import is_git_env_key, isolated_git_env


def test_is_git_env_key_matches_case_insensitively() -> None:
    assert is_git_env_key("GIT_DIR")
    assert is_git_env_key("git_work_tree")
    assert not is_git_env_key("PATH")


def test_isolated_git_env_strips_git_keys_case_insensitively(tmp_path: Path) -> None:
    env = isolated_git_env(
        base_env={
            "git_dir": "/real-checkout/.git",
            "Git_Work_Tree": "/real-checkout",
            "PATH": "/bin",
        },
        global_config_path=tmp_path / "gitconfig",
    )

    assert "git_dir" not in env
    assert "Git_Work_Tree" not in env
    assert env["PATH"] == "/bin"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == str(tmp_path / "gitconfig")
