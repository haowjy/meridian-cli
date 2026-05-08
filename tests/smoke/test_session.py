"""Session log/search smoke tests.

Fills CLI-level gap for session command.
"""


def test_session_log_invalid_ref_fails(cli):
    """session log with invalid ref exits non-zero."""
    result = cli("session", "log", "invalid-ref-xyz-123")
    result.assert_failure()
    assert "Traceback" not in result.stderr


def test_session_search_invalid_ref_fails(cli):
    """session search with invalid ref exits non-zero."""
    result = cli("session", "search", "pattern", "invalid-ref-xyz-456")
    result.assert_failure()
    assert "Traceback" not in result.stderr


def test_session_repair_invalid_ref_fails(cli):
    """session repair with invalid ref exits non-zero."""
    result = cli("session", "repair", "invalid-ref-xyz-789")
    result.assert_failure()
    assert "Traceback" not in result.stderr


def test_session_log_help(cli):
    """session log --help shows usage."""
    result = cli("session", "log", "--help")
    result.assert_success()
    assert "log" in result.stdout.lower() or "session" in result.stdout.lower()


def test_session_search_help(cli):
    """session search --help shows usage."""
    result = cli("session", "search", "--help")
    result.assert_success()
    assert "search" in result.stdout.lower()


def test_session_repair_help(cli):
    """session repair --help shows usage."""
    result = cli("session", "repair", "--help")
    result.assert_success()
    assert "repair" in result.stdout.lower()
