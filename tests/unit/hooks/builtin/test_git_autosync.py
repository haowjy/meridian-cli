from meridian.lib.hooks.builtin.git_autosync import GIT_AUTOSYNC


def test_is_excluded_path_matches_glob_and_directory_patterns() -> None:
    assert GIT_AUTOSYNC._is_excluded_path("logs/debug.log", ("*.log",)) is True
    assert GIT_AUTOSYNC._is_excluded_path("tmp/output.txt", ("tmp/",)) is True
    assert GIT_AUTOSYNC._is_excluded_path("src/main.py", ("tmp/", "*.log")) is False


def test_parse_nul_paths_handles_empty_and_normalized_content() -> None:
    assert GIT_AUTOSYNC._parse_nul_paths("") == ()
    assert GIT_AUTOSYNC._parse_nul_paths("a\0b\0") == ("a", "b")
