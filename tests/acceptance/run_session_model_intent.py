"""Explicit -I -S entrypoint: permanent effect denial before application imports."""

import argparse
import collections
import hashlib
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--dependency-root", type=Path, required=True)
    args = parser.parse_args()
    source, dependencies = args.source_root, args.dependency_root
    assert sys.flags.isolated and sys.flags.no_site
    assert source.is_absolute() and dependencies.is_absolute()
    assert dependencies.parent.name == f"python{sys.version_info.major}.{sys.version_info.minor}"
    assert (source / "src/meridian").is_dir() and (dependencies / "pydantic").is_dir()
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix="intent-acceptance-") as temporary:
        root = Path(temporary)
        os.environ.clear()
        for name in (
            "HOME",
            "MERIDIAN_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
            "TMPDIR",
        ):
            path = root / name
            path.mkdir()
            os.environ[name] = str(path)
        os.environ.update(
            PATH="/nonexistent", UV_OFFLINE="1", UV_PYTHON_DOWNLOADS="never", LANG="C.UTF-8"
        )
        os.chdir(root)
        bootstrap: collections.Counter[str] = collections.Counter()
        workload: collections.Counter[str] = collections.Counter()
        phase = bootstrap
        outside_paths = []
        allowed = (
            root,
            source / "src",
            source / "tests",
            dependencies,
            Path(os.__file__).parent,
            Path(os.__file__).parent.parent
            / f"python{sys.version_info.major}{sys.version_info.minor}.zip",
        )

        def audit(event, arguments):
            if event in {
                "subprocess.Popen",
                "os.system",
                "os.fork",
                "os.forkpty",
                "os.posix_spawn",
                "os.exec",
            } or event.startswith(("socket.", "ctypes.dlopen", "ctypes.dlsym")):
                phase[event] += 1
                raise RuntimeError("denied: " + event)
            if event == "open" and isinstance(arguments[0], (str, bytes)):
                path = Path(os.fsdecode(arguments[0])).absolute()
                mode, flags = arguments[1:3]
                writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
                )
                if not path.is_relative_to(root) and (
                    writing or not any(path.is_relative_to(base) for base in allowed[1:])
                ):
                    phase["outside_data_open"] += 1
                    outside_paths.append(str(path))
                    print("DENIED OPEN", str(path), flush=True)
                    raise PermissionError("outside disposable data root: " + str(path))

        sys.addaudithook(audit)
        import socket
        import subprocess

        cases = {
            "subprocess": ("subprocess.Popen", lambda: subprocess.Popen(["/never"])),
            "system": ("os.system", lambda: os.system("never")),
            "execv": ("os.exec", lambda: os.execv("/never", ["never"])),
            "execve": ("os.exec", lambda: os.execve("/never", ["never"], {})),
            "inet": ("socket.__new__", lambda: socket.socket(socket.AF_INET)),
            "unix": ("socket.__new__", lambda: socket.socket(socket.AF_UNIX)),
            "getaddrinfo": ("socket.getaddrinfo", lambda: socket.getaddrinfo("localhost", 1)),
            "gethostbyname": ("socket.gethostbyname", lambda: socket.gethostbyname("localhost")),
            "gethostbyaddr": ("socket.gethostbyaddr", lambda: socket.gethostbyaddr("127.0.0.1")),
            "getnameinfo": ("socket.getnameinfo", lambda: socket.getnameinfo(("127.0.0.1", 1), 0)),
        }
        unsupported = []
        for name in ("fork", "forkpty", "posix_spawn", "posix_spawnp"):
            if hasattr(os, name):
                call = getattr(os, name)
                cases[name] = (
                    "os.posix_spawn" if name.startswith("posix_spawn") else "os." + name,
                    (lambda call=call: call("/never", ["never"], {}))
                    if name.startswith("posix_spawn")
                    else call,
                )
            else:
                unsupported.append(name)
        # Exercise audit categories whose real APIs require an already denied socket/FFI handle.
        cases["ctypes-import"] = ("ctypes.dlopen", lambda: __import__("ctypes"))
        for name in (
            "socket.connect",
            "socket.bind",
            "socket.sendto",
            "socket.sendmsg",
            "ctypes.dlsym",
        ):
            cases[name] = (name, lambda name=name: sys.audit(name))
        expected: collections.Counter[str] = collections.Counter()
        for name, (event, call) in cases.items():
            before = bootstrap.copy()
            try:
                call()
            except RuntimeError:
                pass
            else:
                raise AssertionError("denial self-test failed: " + name)
            expected[event] += 1
            assert bootstrap == before + collections.Counter({event: 1}), name
        assert bootstrap == expected
        print("denial_self_tests", dict(bootstrap), "unsupported", unsupported, flush=True)
        dependency_denials: collections.Counter[str] = collections.Counter()
        phase = dependency_denials
        sys.path[:0] = [str(source / "src"), str(source), str(dependencies)]
        # psutil primes CPU caches on import. Deny those reads permanently;
        # its documented OSError fallback leaves the caches empty. No proc data is read.
        import psutil

        assert psutil is not None
        assert set(dependency_denials) <= {"outside_data_open"}
        assert set(outside_paths) <= {"/proc/stat"}
        print("dependency_import_denials", dict(dependency_denials), flush=True)
        phase = workload
        # No site initialization, .pth execution, pytest collection or plugins.
        from tests.acceptance.session_model_intent_cases import run

        revision = hashlib.sha256(
            (source / "src/meridian/lib/state/session_authority.py").read_bytes()
            + (source / "src/meridian/lib/state/session_store.py").read_bytes()
        ).hexdigest()
        print(
            "bootstrap",
            sys.version,
            sys.platform,
            "source",
            source,
            "source_sha256",
            revision,
            flush=True,
        )
        try:
            run(root)
        finally:
            print("workload_attempts", dict(workload), flush=True)
            assert not workload
        print("isolated reducer acceptance passed", flush=True)


if __name__ == "__main__":
    main()
