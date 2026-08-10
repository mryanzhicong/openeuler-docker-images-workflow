#!/usr/bin/env python3
"""Clean the dedicated native Docker runner before or after one validation."""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


class RunnerCleanupError(RuntimeError):
    """Raised when a dedicated native runner cannot be made clean."""


CleanupRunner = Callable[
    [Sequence[str], Path, int], subprocess.CompletedProcess[str]
]

_ARCHITECTURES = {
    "x86_64": {"x86_64", "amd64"},
    "aarch64": {"aarch64", "arm64"},
}
_MANAGED_BUILDER_PREFIXES = ("oe-e2e-", "oe-smoke-")
_OUTPUT_LIMIT = 2000
_POSITIVE_INTEGER_RE = re.compile(r"^[1-9][0-9]*$")


def _default_runner(
    command: Sequence[str], cwd: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _clip(value: object) -> str:
    return str(value or "").strip()[-_OUTPUT_LIMIT:]


def _write_report(path: Path, report: dict[str, object]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def clean_native_runner(
    *,
    workspace: Path,
    job_temp: Path,
    architecture: str,
    phase: str,
    run_id: str,
    run_attempt: str,
    round_number: str,
    runner_name: str,
    report_path: Path,
    machine: str | None = None,
    runner: CleanupRunner = _default_runner,
) -> dict[str, object]:
    """Remove all Docker state from one dedicated native runner.

    These commands are intentionally host-wide. The workflow calls this only on
    machines dedicated to native image validation, with one runner per host.
    """
    workspace = Path(workspace).resolve()
    raw_job_temp = Path(job_temp)
    if not raw_job_temp.is_absolute():
        raise ValueError("job_temp must be an absolute path")
    job_temp = raw_job_temp.resolve()
    if job_temp == Path("/") or job_temp == workspace:
        raise ValueError("job_temp must be a dedicated temporary directory")
    report_path = Path(report_path)
    if architecture not in _ARCHITECTURES:
        raise ValueError("architecture must be x86_64 or aarch64")
    if phase not in {"before", "after"}:
        raise ValueError("phase must be before or after")
    for field, value in (
        ("run_id", run_id),
        ("run_attempt", run_attempt),
        ("round", round_number),
    ):
        if not _POSITIVE_INTEGER_RE.fullmatch(value):
            raise ValueError(f"{field} must be a positive integer")
    runner_name = runner_name.strip()
    if not runner_name:
        raise ValueError("runner_name must not be empty")
    actual_machine = (machine or platform.machine()).strip().lower()
    report: dict[str, object] = {
        "schema_version": 1,
        "status": "running",
        "phase": phase,
        "architecture": architecture,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "round": round_number,
        "runner_name": runner_name,
        "machine": actual_machine,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "commands": [],
        "filesystem_cleanup": [],
    }
    failures: list[str] = []
    records = report["commands"]
    filesystem_records = report["filesystem_cleanup"]
    assert isinstance(records, list)
    assert isinstance(filesystem_records, list)

    if actual_machine not in _ARCHITECTURES[architecture]:
        failures.append(
            f"native architecture mismatch: expected {architecture}, "
            f"got {actual_machine or 'unknown'}"
        )

    def execute(command: Sequence[str], *, timeout: int = 600) -> str:
        encoded = [str(part) for part in command]
        try:
            completed = runner(encoded, workspace, timeout)
            returncode = int(completed.returncode)
            stdout = _clip(completed.stdout)
            stderr = _clip(completed.stderr)
        except (OSError, subprocess.TimeoutExpired) as error:
            returncode = 124 if isinstance(error, subprocess.TimeoutExpired) else 127
            stdout = ""
            stderr = str(error)
        records.append(
            {
                "command": encoded,
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
        )
        if returncode != 0:
            failures.append(
                f"{' '.join(encoded[:3])} failed with exit status {returncode}"
            )
            return ""
        return stdout

    if not failures:
        temporary_names = ["phase1-input", "phase1-target"]
        if phase == "before":
            temporary_names.append("phase1-round")
        for name in temporary_names:
            target = job_temp / name
            try:
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
            except OSError as error:
                failures.append(f"failed to remove temporary path {target}: {error}")
                filesystem_records.append(
                    {"path": str(target), "status": "failed", "error": str(error)}
                )
            else:
                filesystem_records.append(
                    {"path": str(target), "status": "removed"}
                )

        builders = execute(
            ["docker", "buildx", "ls", "--format", "{{.Name}}"],
            timeout=300,
        )
        for builder in sorted(set(builders.splitlines())):
            builder = builder.strip()
            if builder.startswith(_MANAGED_BUILDER_PREFIXES):
                execute(
                    ["docker", "buildx", "rm", "--force", builder],
                    timeout=300,
                )

        container_ids = execute(
            ["docker", "ps", "--all", "--quiet"],
            timeout=300,
        ).split()
        if container_ids:
            execute(
                ["docker", "rm", "--force", "--volumes", *container_ids],
                timeout=600,
            )

        for command in (
            ["docker", "builder", "prune", "--all", "--force"],
            ["docker", "image", "prune", "--all", "--force"],
            ["docker", "volume", "prune", "--force"],
            ["docker", "network", "prune", "--force"],
        ):
            execute(command)

    report["status"] = "failed" if failures else "passed"
    report["failures"] = failures
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write_report(report_path, report)
    if failures:
        raise RunnerCleanupError("; ".join(failures))
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--job-temp", required=True, type=Path)
    parser.add_argument(
        "--architecture",
        required=True,
        choices=tuple(_ARCHITECTURES),
    )
    parser.add_argument("--phase", required=True, choices=("before", "after"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--round", required=True, dest="round_number")
    parser.add_argument("--runner-name", required=True)
    parser.add_argument("--report", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = clean_native_runner(
            workspace=args.workspace,
            job_temp=args.job_temp,
            architecture=args.architecture,
            phase=args.phase,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            round_number=args.round_number,
            runner_name=args.runner_name,
            report_path=args.report,
        )
    except (RunnerCleanupError, ValueError) as error:
        print(f"runner-cleanup: error: {error}")
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
