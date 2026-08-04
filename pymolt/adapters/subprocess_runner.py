import subprocess


def run_command(
    args: list[str],
    check: bool = True,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess command in one isolated system-touching location.

    ``env`` and ``timeout`` are additive (existing callers are unaffected). ``env`` lets
    verify/ inject ``PYMOLT_TRACE_*`` when launching a suite or test client under the
    Mode-B watcher; ``timeout`` guards a hung traced run.
    """
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=check,
        cwd=cwd,
        env=env,
        timeout=timeout,
    )
