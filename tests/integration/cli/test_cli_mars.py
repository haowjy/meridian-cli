import importlib
import subprocess
from io import StringIO

import pytest

cli_main = importlib.import_module("meridian.cli.main")
mars_passthrough = importlib.import_module("meridian.cli.mars_passthrough")


@pytest.mark.parametrize(
    ("is_sync", "output_format", "result", "expected_stdout", "expected_stderr", "expect_augment"),
    [
        (
            False,
            "json",
            mars_passthrough.MarsPassthroughResult(
                request=mars_passthrough.MarsPassthroughRequest(
                    command=("/usr/bin/mars", "--json", "list"),
                    mars_args=("--json", "list"),
                    is_sync=False,
                    wants_json=True,
                    root_override=None,
                ),
                returncode=7,
                stdout_text='{"packages": []}\n',
                stderr_text="warning\n",
            ),
            '{"packages": []}\n',
            "warning\n",
            0,
        ),
        (
            True,
            None,
            mars_passthrough.MarsPassthroughResult(
                request=mars_passthrough.MarsPassthroughRequest(
                    command=("/usr/bin/mars", "sync"),
                    mars_args=("sync",),
                    is_sync=True,
                    wants_json=False,
                    root_override=None,
                ),
                returncode=1,
            ),
            "",
            "",
            1,
        ),
    ],
    ids=["non-sync-streaming", "sync-augment"],
)
def test_run_mars_passthrough_streaming_and_sync_augment(
    is_sync: bool,
    output_format: str | None,
    result: mars_passthrough.MarsPassthroughResult,
    expected_stdout: str,
    expected_stderr: str,
    expect_augment: int,
) -> None:
    request = result.request
    stdout = StringIO()
    stderr = StringIO()
    observed: list[mars_passthrough.MarsPassthroughResult] = []

    with pytest.raises(SystemExit) as exc_info:
        mars_passthrough.run_mars_passthrough(
            ["sync"] if is_sync else ["list"],
            output_format=output_format,
            resolve_executable=lambda: "/usr/bin/mars",
            parse_request=lambda *_args, **_kwargs: request,
            execute_request=lambda _request: result,
            augment_result=lambda passthrough_result: observed.append(passthrough_result),
            stdout=stdout,
            stderr=stderr,
        )

    assert exc_info.value.code == result.returncode
    assert stdout.getvalue() == expected_stdout
    assert stderr.getvalue() == expected_stderr
    assert len(observed) == expect_augment


@pytest.mark.parametrize(
    "is_sync",
    [False, True],
    ids=["non-sync", "sync"],
)
def test_execute_mars_passthrough_sets_managed_env_only_for_sync(is_sync: bool) -> None:
    mars_args = ("sync",) if is_sync else ("models", "list")
    request = mars_passthrough.MarsPassthroughRequest(
        command=("/usr/bin/mars", *mars_args),
        mars_args=mars_args,
        is_sync=is_sync,
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
    if is_sync:
        assert isinstance(env, dict)
        assert env["MERIDIAN_MANAGED"] == "1"
    else:
        assert env is None


@pytest.mark.parametrize(
    "argv",
    [
        ["mars", "list"],
        ["--format", "text", "mars", "list"],
    ],
    ids=["agent-mode-default-text", "agent-mode-explicit-text"],
)
def test_main_mars_list_agent_mode_uses_text_output(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        cli_main.main(argv)

    assert exc_info.value.code == 0
    assert captured["args"] == ("list",)
    assert captured["output_format"] == "text"
