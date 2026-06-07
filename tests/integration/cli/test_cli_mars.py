import importlib
import subprocess
from io import StringIO

import pytest

cli_main = importlib.import_module("meridian.cli.main")
mars_passthrough = importlib.import_module("meridian.cli.mars_passthrough")


@pytest.mark.parametrize(
    ("is_sync", "output_format", "result", "expected_stdout", "expected_stderr"),
    [
        (
            False,
            "json",
            mars_passthrough.MarsPassthroughResult(
                request=mars_passthrough.MarsPassthroughRequest(
                    command=("/usr/bin/mars", "--json", "list"),
                    mars_args=("--json", "list"),
                    wants_json=True,
                    root_override=None,
                ),
                returncode=7,
                stdout_text='{"packages": []}\n',
                stderr_text="warning\n",
            ),
            '{"packages": []}\n',
            "warning\n",
        ),
        (
            True,
            None,
            mars_passthrough.MarsPassthroughResult(
                request=mars_passthrough.MarsPassthroughRequest(
                    command=("/usr/bin/mars", "sync"),
                    mars_args=("sync",),
                    wants_json=False,
                    root_override=None,
                ),
                returncode=1,
            ),
            "",
            "",
        ),
    ],
    ids=["non-sync-streaming", "sync-streaming"],
)
def test_run_mars_passthrough_streaming(
    is_sync: bool,
    output_format: str | None,
    result: mars_passthrough.MarsPassthroughResult,
    expected_stdout: str,
    expected_stderr: str,
) -> None:
    request = result.request
    stdout = StringIO()
    stderr = StringIO()

    with pytest.raises(SystemExit) as exc_info:
        mars_passthrough.run_mars_passthrough(
            ["sync"] if is_sync else ["list"],
            output_format=output_format,
            resolve_executable=lambda: "/usr/bin/mars",
            parse_request=lambda *_args, **_kwargs: request,
            execute_request=lambda _request: result,
            stdout=stdout,
            stderr=stderr,
        )

    assert exc_info.value.code == result.returncode
    assert stdout.getvalue() == expected_stdout
    assert stderr.getvalue() == expected_stderr


@pytest.mark.parametrize(
    "mars_args",
    [
        ("models", "list"),
        ("sync",),
        ("upgrade",),
        ("link",),
        ("init", "--link", ".claude"),
        ("init",),
    ],
    ids=["models", "sync", "upgrade", "link", "init-link", "init"],
)
def test_execute_mars_passthrough_sets_managed_env_for_all_commands(
    mars_args: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_MANAGED", raising=False)
    request = mars_passthrough.MarsPassthroughRequest(
        command=("/usr/bin/mars", *mars_args),
        mars_args=mars_args,
        wants_json=False,
        root_override=None,
    )
    observed: dict[str, object] = {}

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["cmd"] = cmd
        observed["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    result = mars_passthrough.execute_mars_passthrough(request, run=_fake_run)

    assert result.returncode == 0
    assert observed["cmd"] == ["/usr/bin/mars", *mars_args]
    env = observed["env"]
    assert isinstance(env, dict)
    assert env["MERIDIAN_MANAGED"] == "1"


def test_execute_mars_passthrough_preserves_outer_meridian_managed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_MANAGED", "0")
    request = mars_passthrough.MarsPassthroughRequest(
        command=("/usr/bin/mars", "models", "list"),
        mars_args=("models", "list"),
        wants_json=False,
        root_override=None,
    )
    observed: dict[str, object] = {}

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    mars_passthrough.execute_mars_passthrough(request, run=_fake_run)

    env = observed["env"]
    assert isinstance(env, dict)
    assert env["MERIDIAN_MANAGED"] == "0"


def test_main_mars_defaults_to_text_in_agent_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, object] = {}

    def _fake_run_mars_passthrough(
        args: tuple[str, ...] | list[str],
        *,
        output_format: str | None = None,
        **_kwargs: object,
    ) -> None:
        captured["args"] = tuple(args)
        captured["output_format"] = output_format
        raise SystemExit(0)

    monkeypatch.setattr(mars_passthrough, "run_mars_passthrough", _fake_run_mars_passthrough)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["mars", "list"])

    assert exc_info.value.code == 0
    assert captured["args"] == ("list",)
    assert captured["output_format"] == "text"
