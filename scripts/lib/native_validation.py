"""Application-neutral native build, runtime tests, evidence, and cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import platform as runtime_platform
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Callable, Mapping, Sequence

from scripts.lib.progress import log, run_streaming
from scripts.lib.task_spec import TaskSpec
from scripts.lib.target_contract import validate_test_contract


class NativeValidationError(RuntimeError):
    """Raised when native image validation fails.

    Carries the structured failure so the caller can hand the Fixer a command,
    an exit code and both ends of the log instead of one opaque string.
    """

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.details: dict[str, object] = dict(details or {})


CommandRunner = Callable[
    [Sequence[str], Path, Mapping[str, str], int],
    subprocess.CompletedProcess,
]
FormatValidator = Callable[..., Mapping[str, object]]

_PLATFORMS = {
    "x86_64": "linux/amd64",
    "aarch64": "linux/arm64",
}
_RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
# The Fixer prompt asks for the earliest error because that is usually the root
# cause, so keeping only the tail hid exactly what it was told to look for.
_LOG_HEAD_CHARS = 2000
_LOG_TAIL_CHARS = 4000
_CONTAINER_LOG_LINES = "200"
# An entrypoint that reports "export properties error" and exits has told the
# Fixer that something failed, not what. The reason is in a file the container
# wrote and docker logs never saw, so probe for those files by shape rather
# than by name: run 31106121623 spent 22 minutes rebuilding Kylin locally to
# read a shell.stderr that was sitting inside the container the whole time.
_PROBE_ROOTS = ("/opt", "/home", "/srv", "/app", "/usr/local", "/var/log")
_PROBE_NAMES = ("*.log", "*.out", "*.err", "*.stderr")
_PROBE_MAX_FILES = "20"
_PROBE_TAIL_LINES = "200"
# A Bigdata image carries tens of thousands of jars under these roots, so the
# walk is bounded three ways: it stays on one filesystem, it stops before the
# depth application logs are ever nested at, and it only considers files this
# run could have written. head then closes the pipe, which ends find early once
# the quota is met.
_PROBE_MAX_DEPTH = "6"
_PROBE_MAX_AGE_MINUTES = "180"
_E2E_CHECKS = (
    "native_build",
    "dgoss",
    "shared_tests",
)
_SMOKE_CHECKS = _E2E_CHECKS


def _default_runner(
    command: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
) -> subprocess.CompletedProcess:
    process_env = os.environ.copy()
    process_env.update(env)
    return run_streaming(
        command,
        cwd=cwd,
        env=process_env,
        timeout=timeout,
    )


def _run_optional_format_check(
    *,
    format_validator: FormatValidator | None,
    workspace: Path,
    architecture: str,
    report_path: Path,
) -> dict[str, object] | None:
    if format_validator is None:
        return None
    try:
        result = dict(
            format_validator(
                workspace=workspace,
                architecture=architecture,
                temp_root=report_path.parent / "upstream-format",
            )
        )
    except Exception as error:
        return {
            "status": "failed",
            "kind": "infra",
            "stage": "integration",
            "runner_architecture": architecture,
            "failure": str(error) or error.__class__.__name__,
        }
    if result.get("status") not in {"passed", "failed"}:
        return {
            **result,
            "status": "failed",
            "kind": "infra",
            "stage": "integration",
            "runner_architecture": architecture,
            "failure": "format validator returned an invalid status",
        }
    return result


def _format_failure(result: Mapping[str, object] | None) -> str | None:
    if result is None or result.get("status") == "passed":
        return None
    return str(
        result.get("failure")
        or result.get("output")
        or "upstream format check failed"
    )


def _format_failure_details(result: Mapping[str, object]) -> dict[str, object]:
    return {
        key: result[key]
        for key in ("kind", "stage", "commit_sha")
        if key in result
    }


def _merged_output(result: subprocess.CompletedProcess) -> str:
    # run_streaming already folds stderr into stdout; injected runners may
    # still populate them separately, so read both rather than guessing.
    return "\n".join(
        part.strip()
        for part in (str(result.stdout or ""), str(result.stderr or ""))
        if part.strip()
    )


def _raw_output(result: subprocess.CompletedProcess) -> str:
    stdout = str(result.stdout or "")
    stderr = str(result.stderr or "")
    if stdout and stderr and not stdout.endswith("\n"):
        return f"{stdout}\n{stderr}"
    return stdout + stderr


def _clip(text: str) -> tuple[str, str]:
    """Both ends of a failure log: the earliest error and the final error."""
    if len(text) <= _LOG_HEAD_CHARS + _LOG_TAIL_CHARS:
        return text, ""
    return text[:_LOG_HEAD_CHARS], text[-_LOG_TAIL_CHARS:]


def _container_log_summary(text: str) -> str:
    tail = "\n".join(text.splitlines()[-int(_CONTAINER_LOG_LINES) :])
    head, end = _clip(tail)
    return head if not end else f"{head}\n...\n{end}"


def _write_full_evidence(
    *,
    artifact_root: Path,
    diagnostics_dir: Path,
    name: str,
    suffix: str,
    content: str,
) -> dict[str, object]:
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    path = diagnostics_dir / f"{name}.{suffix}"
    path.write_bytes(content.encode())
    return _file_metadata(path, artifact_root=artifact_root)


def _file_metadata(path: Path, *, artifact_root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(artifact_root).as_posix(),
        "size_bytes": path.stat().st_size,
    }


def _capture_status(
    metadata: dict[str, object],
    *,
    returncode: int,
) -> dict[str, object]:
    metadata["capture_status"] = {0: "complete", 124: "timeout"}.get(
        returncode,
        "failed",
    )
    return metadata


def _file_log_summary(path: Path) -> str:
    lines: deque[str] = deque(maxlen=int(_CONTAINER_LOG_LINES))
    with path.open(errors="replace") as stream:
        for line in stream:
            line = line.rstrip("\r\n")
            if len(line) > _LOG_HEAD_CHARS + _LOG_TAIL_CHARS:
                line = line[:_LOG_HEAD_CHARS] + line[-_LOG_TAIL_CHARS:]
            lines.append(line)
    return _container_log_summary("\n".join(lines))


def _stream_command_evidence(
    *,
    command: Sequence[str],
    cwd: Path,
    artifact_root: Path,
    path: Path,
    timeout: int,
) -> tuple[dict[str, object], str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("wb") as output:
            result = subprocess.run(
                list(command),
                cwd=cwd,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        returncode = 124
    metadata = _capture_status(
        _file_metadata(path, artifact_root=artifact_root),
        returncode=returncode,
    )
    return metadata, _file_log_summary(path)


def _run(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess:
    result = runner(command, cwd, env or {}, timeout)
    if check and result.returncode != 0:
        output = _merged_output(result) or "command failed"
        head, tail = _clip(output)
        omitted = len(output) - len(head) - len(tail)
        message = head if not tail else f"{head}\n...[{omitted} chars omitted]...\n{tail}"
        raise NativeValidationError(
            message,
            details={
                "command": list(command),
                "returncode": result.returncode,
                "stdout_head": head,
                "stdout_tail": tail,
            },
        )
    return result


def _probe_script() -> str:
    """Shell that reports what is running and dumps the logs docker never saw.

    Deliberately POSIX sh and best-effort throughout: a probe that fails on an
    unusual image must still return the part it managed to collect.
    """
    names = " -o ".join(f"-name '{pattern}'" for pattern in _PROBE_NAMES)
    roots = " ".join(_PROBE_ROOTS)
    return (
        "echo '### processes'\n"
        "ps -ef 2>/dev/null || ps aux 2>/dev/null || echo '(ps unavailable)'\n"
        f"for root in {roots}; do\n"
        '  [ -d "$root" ] || continue\n'
        f'  find "$root" -xdev -maxdepth {_PROBE_MAX_DEPTH} -type f'
        f" \\( {names} \\)"
        f" -mmin -{_PROBE_MAX_AGE_MINUTES} 2>/dev/null\n"
        "done"
        f" | head -n {_PROBE_MAX_FILES}"
        " | while IFS= read -r file; do\n"
        '  echo "### $file"\n'
        f'  tail -n {_PROBE_TAIL_LINES} "$file" 2>/dev/null\n'
        "done\n"
    )


def _container_evidence(
    runner: CommandRunner,
    *,
    workspace: Path,
    containers: Sequence[str],
    artifact_root: Path,
) -> dict[str, object]:
    """Read what the containers themselves reported, before cleanup removes them.

    When an image builds but the application dies on startup, the container log
    is the only place the reason exists; the harness used to force-remove the
    container before anything read it.
    """
    evidence: dict[str, object] = {}
    for name in containers:
        inspected = _run(
            runner,
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}} {{.State.ExitCode}} {{.State.Error}}",
                name,
            ],
            cwd=workspace,
            timeout=60,
            check=False,
        )
        if inspected.returncode != 0:
            continue
        log_command = ["docker", "logs", "--timestamps", name]
        if runner is _default_runner:
            log_metadata, log_summary = _stream_command_evidence(
                command=log_command,
                cwd=workspace,
                artifact_root=artifact_root,
                path=artifact_root / "diagnostics" / f"{name}.docker.log",
                timeout=60,
            )
        else:
            logs = _run(
                runner,
                log_command,
                cwd=workspace,
                timeout=60,
                check=False,
            )
            log_metadata = _capture_status(
                _write_full_evidence(
                    artifact_root=artifact_root,
                    diagnostics_dir=artifact_root / "diagnostics",
                    name=name,
                    suffix="docker.log",
                    content=_raw_output(logs),
                ),
                returncode=logs.returncode,
            )
            log_summary = _container_log_summary(_raw_output(logs))
        probe_command = ["docker", "exec", name, "sh", "-c", _probe_script()]
        if runner is _default_runner:
            probe_metadata, probe_summary = _stream_command_evidence(
                command=probe_command,
                cwd=workspace,
                artifact_root=artifact_root,
                path=artifact_root / "diagnostics" / f"{name}.probe.log",
                timeout=60,
            )
        else:
            probed = _run(
                runner,
                probe_command,
                cwd=workspace,
                timeout=60,
                check=False,
            )
            probe_metadata = _capture_status(
                _write_full_evidence(
                    artifact_root=artifact_root,
                    diagnostics_dir=artifact_root / "diagnostics",
                    name=name,
                    suffix="probe.log",
                    content=_raw_output(probed),
                ),
                returncode=probed.returncode,
            )
            probe_summary = _container_log_summary(_raw_output(probed))
        evidence[name] = {
            "state": _merged_output(inspected),
            "logs": log_summary,
            "full_logs": log_metadata,
            "probe": probe_summary,
            "full_probe": probe_metadata,
        }
    return evidence


def _write_evidence(
    *,
    report_path: Path,
    junit_path: Path,
    report: dict[str, object],
    failure: str | None,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    suite = ET.Element(
        "testsuite",
        {
            "name": (
                f"{report['environment']['software_name']}-"
                f"{report['architecture']}"
            ),
            "tests": "1",
            "failures": "1" if failure else "0",
            "errors": "0",
            "time": str(report["duration_seconds"]),
        },
    )
    case = ET.SubElement(
        suite,
        "testcase",
        {
            "classname": "native-image-validation",
            "name": "build-runtime-tests",
            "time": str(report["duration_seconds"]),
        },
    )
    if failure:
        node = ET.SubElement(case, "failure", {"message": failure[:500]})
        node.text = failure[:4000]
    ET.ElementTree(suite).write(
        junit_path,
        encoding="utf-8",
        xml_declaration=True,
    )


def write_infrastructure_failure_evidence(
    *,
    task: TaskSpec,
    architecture: str,
    failed_stage: str,
    failure: str,
    report_path: Path,
    junit_path: Path,
    attempts: int,
) -> dict[str, object]:
    """Record a pre-validation infrastructure failure for round evaluation."""
    if architecture not in _PLATFORMS:
        raise NativeValidationError(
            "architecture must be the native runner name x86_64 or aarch64"
        )
    report: dict[str, object] = {
        "status": "failed",
        "task_id": task.task_id,
        "architecture": architecture,
        "platform": _PLATFORMS[architecture],
        "image_id": "",
        "validated_patch_sha256": "",
        "duration_seconds": 0.0,
        "environment": _environment_evidence(task, architecture),
        "checks": {name: None for name in _E2E_CHECKS},
        "failure": failure,
        "failed_stage": failed_stage,
        "failure_details": {
            "attempts": attempts,
            "retryable": True,
        },
    }
    _write_evidence(
        report_path=Path(report_path),
        junit_path=Path(junit_path),
        report=report,
        failure=failure,
    )
    return report


def validated_patch_digest(workspace: Path) -> str:
    """Digest the candidate content this workspace actually holds.

    The workspace is checked out at the immutable base SHA with the candidate
    patch applied and never committed, so diffing HEAD yields exactly the
    candidate. Recording it lets a later stage prove both architectures
    validated the same content instead of trusting job order.
    """
    workspace = Path(workspace)
    if not (workspace / ".git").is_dir():
        raise NativeValidationError(
            "target workspace must be a Git checkout to digest its candidate"
        )
    for arguments in (
        ["add", "--intent-to-add", "--", "."],
        ["diff", "--binary", "--full-index", "--no-ext-diff", "HEAD", "--"],
    ):
        completed = subprocess.run(
            ["git", "-C", str(workspace), *arguments],
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            raise NativeValidationError(
                (completed.stderr or b"").decode(errors="replace").strip()
                or "candidate digest failed"
            )
    return hashlib.sha256(completed.stdout).hexdigest()


def _create_builder(
    runner: CommandRunner,
    builder: str,
    *,
    cwd: Path,
) -> None:
    """Create a disposable builder for exactly one native validation."""
    _run(
        runner,
        [
            "docker",
            "buildx",
            "create",
            "--name",
            builder,
            "--driver",
            "docker-container",
        ],
        cwd=cwd,
    )


def _validate_tool(path: Path, name: str) -> Path:
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise NativeValidationError(f"{name} must be an absolute executable file")
    return path


def _os_name() -> str:
    try:
        values = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value.strip().strip('"')
        return values.get("PRETTY_NAME") or values.get("NAME") or "unknown"
    except OSError:
        return "unknown"


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() in {
                "model name",
                "model",
                "hardware",
                "processor",
            }:
                candidate = value.strip()
                if candidate:
                    return candidate
    except OSError:
        pass
    return runtime_platform.processor() or "unknown"


def _environment_evidence(task: TaskSpec, architecture: str) -> dict[str, object]:
    try:
        numpy_version = metadata.version("numpy")
    except metadata.PackageNotFoundError:
        numpy_version = "not-installed"
    return {
        "test_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Model": os.environ.get("RUNNER_NAME", "self-hosted-native-runner"),
        "architecture": architecture,
        "kernel": runtime_platform.release(),
        "os": _os_name(),
        "cpu_model": _cpu_model(),
        "cpu_cores": os.cpu_count() or 0,
        "software_name": task.app,
        "software_version": task.version,
        "python_version": runtime_platform.python_version(),
        "numpy_version": numpy_version,
    }


def _run_dgoss(
    runner: CommandRunner,
    *,
    workspace: Path,
    image: str,
    tests_root: Path,
    version: str,
    dgoss: Path,
    goss: Path,
    container: str,
    service_mode: bool,
) -> None:
    command = [str(dgoss), "run", "--name", container]
    if not service_mode:
        command.extend(("--entrypoint", "/bin/sh"))
    command.extend(("--env", f"EXPECTED_VERSION={version}", image))
    if not service_mode:
        command.extend(("-c", "sleep 300"))
    environment = {
        "GOSS_PATH": str(goss),
        "GOSS_FILES_PATH": str(tests_root),
        "GOSS_FILE": "goss.yaml",
        "EXPECTED_VERSION": version,
    }
    if service_mode:
        environment["GOSS_WAIT_OPTS"] = "-r 30s -s 1s"
    _run(
        runner,
        command,
        cwd=workspace,
        env=environment,
        timeout=300,
    )


def _run_shared_tests(
    runner: CommandRunner,
    *,
    workspace: Path,
    image: str,
    tests_root: Path,
    version: str,
    container: str,
    service_mode: bool,
    run_id: str,
) -> None:
    command = [
        "docker",
        "run",
        "--name",
        container,
        "--label",
        f"oe.autopilot.run={run_id}",
        "--volume",
        f"{tests_root}:/opt/oe-tests:ro",
    ]
    if service_mode:
        command.insert(2, "--detach")
        command.append(image)
        _run(runner, command, cwd=workspace)
        _run(
            runner,
            [
                "docker",
                "exec",
                "--env",
                f"EXPECTED_VERSION={version}",
                container,
                "/opt/oe-tests/test.sh",
            ],
            cwd=workspace,
            timeout=600,
        )
        return
    command.extend(
        (
            "--env",
            f"EXPECTED_VERSION={version}",
            "--entrypoint",
            "/bin/sh",
            image,
            "-c",
            "exec /opt/oe-tests/test.sh",
        )
    )
    _run(runner, command, cwd=workspace, timeout=300)


def validate_native_image(
    *,
    workspace: Path,
    task: TaskSpec,
    architecture: str,
    run_id: str,
    dgoss: Path,
    goss: Path,
    report_path: Path,
    junit_path: Path,
    runner: CommandRunner = _default_runner,
    sleep: Callable[[float], None] = time.sleep,
    format_validator: FormatValidator | None = None,
) -> dict[str, object]:
    if architecture not in _PLATFORMS:
        raise NativeValidationError(
            "architecture must be the native runner name x86_64 or aarch64"
        )
    if not _RUN_ID_RE.fullmatch(run_id):
        raise NativeValidationError("run_id must be a positive integer")
    workspace = Path(workspace)
    if not workspace.is_dir():
        raise NativeValidationError("target workspace does not exist")
    report_path = Path(report_path)
    junit_path = Path(junit_path)
    start = time.monotonic()
    format_check = _run_optional_format_check(
        format_validator=format_validator,
        workspace=workspace,
        architecture=architecture,
        report_path=report_path,
    )
    dgoss = _validate_tool(dgoss, "dgoss")
    goss = _validate_tool(goss, "goss")

    app_root = workspace / task.domain / task.app
    image_root = app_root / task.version / task.os_version
    tests_root = app_root / "tests"
    dockerfile = image_root / "Dockerfile"
    if not dockerfile.is_file():
        raise NativeValidationError(
            f"native validation input is missing: {dockerfile}"
        )
    test_contract = validate_test_contract(repo=workspace, task=task)
    service_mode = (tests_root / "goss_wait.yaml").is_file()

    platform = _PLATFORMS[architecture]
    slug = architecture.replace("_", "-")
    prefix = f"oe-e2e-{run_id}-{slug}"
    builder = f"{prefix}-builder"
    dgoss_container = f"{prefix}-dgoss"
    container = f"{prefix}-runtime"
    image = f"oe-autopilot/{task.app}:{task.version}-{run_id}-{slug}"
    validated_patch_sha256 = validated_patch_digest(workspace)
    image_id = ""
    failures: list[dict[str, object]] = []
    container_evidence: dict[str, object] = {}
    # None means the check was never reached. Sharing one boolean across all
    # checks made a dgoss failure look like a build failure to the Fixer.
    checks: dict[str, bool | None] = {name: None for name in _E2E_CHECKS}
    stage = f"native:{architecture}"
    log(stage, "START validation")

    def record_failure(
        check: str,
        error: NativeValidationError,
        *,
        failed_stage: str | None = None,
    ) -> None:
        checks[check] = False
        failures.append(
            {
                "stage": failed_stage or check,
                "check": check,
                "failure": str(error),
                "failure_details": dict(error.details),
            }
        )
        log(stage, f"FAIL {check}: {error}")

    def run_check(check: str, action: Callable[[], None]) -> None:
        log(stage, f"START {check}")
        try:
            action()
        except NativeValidationError as error:
            record_failure(check, error)
        else:
            checks[check] = True
            log(stage, f"PASS {check}")

    def contract_error(check: str) -> NativeValidationError:
        findings = [
            finding
            for finding in test_contract["findings"]
            if finding.get("check") == check
        ]
        return NativeValidationError(
            "native test contract is not executable: "
            + "; ".join(str(finding["message"]) for finding in findings),
            details={"findings": findings},
        )

    try:
        try:
            log(stage, "START build")
            _create_builder(runner, builder, cwd=workspace)
            _run(
                runner,
                [
                    "docker",
                    "buildx",
                    "build",
                    "--builder",
                    builder,
                    "--no-cache",
                    "--load",
                    "--progress",
                    "plain",
                    "--platform",
                    platform,
                    "--tag",
                    image,
                    "--file",
                    str(dockerfile),
                    str(image_root),
                ],
                cwd=workspace,
                timeout=7200,
            )
            inspected = _run(
                runner,
                ["docker", "image", "inspect", "--format", "{{.Id}}", image],
                cwd=workspace,
            )
            image_id = str(inspected.stdout or "").strip()
        except NativeValidationError as error:
            record_failure("native_build", error)
        else:
            checks["native_build"] = True
            log(stage, "PASS build")
            if test_contract["goss_allowed"] is True:
                run_check(
                    "dgoss",
                    lambda: _run_dgoss(
                        runner,
                        workspace=workspace,
                        image=image,
                        tests_root=tests_root,
                        version=task.version,
                        dgoss=dgoss,
                        goss=goss,
                        container=dgoss_container,
                        service_mode=service_mode,
                    ),
                )
            else:
                record_failure(
                    "dgoss",
                    contract_error("dgoss"),
                    failed_stage="test_contract",
                )
            if test_contract["shared_tests_allowed"] is True:
                run_check(
                    "shared_tests",
                    lambda: _run_shared_tests(
                        runner,
                        workspace=workspace,
                        image=image,
                        tests_root=tests_root,
                        version=task.version,
                        container=container,
                        service_mode=service_mode,
                        run_id=run_id,
                    ),
                )
            else:
                record_failure(
                    "shared_tests",
                    contract_error("shared_tests"),
                    failed_stage="test_contract",
                )
        if failures:
            # Must run before the finally block force-removes the containers.
            try:
                container_evidence = _container_evidence(
                    runner,
                    workspace=workspace,
                    containers=(dgoss_container, container),
                    artifact_root=report_path.parent,
                )
            except Exception as error:
                capture_error = str(error) or error.__class__.__name__
                container_evidence = {"capture_error": capture_error}
                log(stage, f"WARN container evidence: {capture_error}")
    finally:
        cleanup_commands = (
            ["docker", "rm", "--force", "--volumes", dgoss_container],
            ["docker", "rm", "--force", "--volumes", container],
            ["docker", "image", "rm", "--force", image],
            ["docker", "buildx", "rm", "--force", builder],
        )
        for command in cleanup_commands:
            _run(
                runner,
                command,
                cwd=workspace,
                timeout=300,
                check=False,
            )

    format_failure = _format_failure(format_check)
    first_failure = failures[0] if failures else None
    failure = str(first_failure["failure"]) if first_failure else None
    overall_failure = failure or format_failure
    report: dict[str, object] = {
        "status": "failed" if overall_failure else "passed",
        "task_id": task.task_id,
        "architecture": architecture,
        "platform": platform,
        "builder": builder,
        "build_cache": "disabled",
        "image_id": image_id,
        "validated_patch_sha256": validated_patch_sha256,
        "duration_seconds": round(time.monotonic() - start, 3),
        "environment": _environment_evidence(task, architecture),
        "checks": checks,
    }
    if format_check is not None:
        report["format_check"] = format_check
    if first_failure:
        report["failure"] = failure
        report["failed_stage"] = first_failure["stage"]
        report["failure_details"] = first_failure["failure_details"]
        report["failures"] = failures
        if container_evidence:
            report["container_evidence"] = container_evidence
    elif format_failure:
        report["failure"] = format_failure
        report["failed_stage"] = "upstream_format"
        report["failure_details"] = _format_failure_details(format_check or {})
    _write_evidence(
        report_path=report_path,
        junit_path=junit_path,
        report=report,
        failure=overall_failure,
    )
    if overall_failure:
        failure_summary = "\n".join(
            f"{item['check']}: {item['failure']}" for item in failures
        ) or str(overall_failure)
        log(stage, f"FAIL validation: {failure_summary}")
        # Keep the structure a direct caller needs; only the report file had it.
        details = (
            dict(first_failure["failure_details"])
            if first_failure
            else _format_failure_details(format_check or {})
        )
        raise NativeValidationError(failure_summary, details=details)
    log(stage, "PASS validation")
    return report


def validate_native_smoke(
    *,
    workspace: Path,
    task: TaskSpec,
    architecture: str,
    run_id: str,
    dgoss: Path,
    goss: Path,
    report_path: Path,
    junit_path: Path,
    repair_report_dir: Path,
    runner: CommandRunner = _default_runner,
    format_validator: FormatValidator | None = None,
) -> dict[str, object]:
    if architecture not in _PLATFORMS:
        raise NativeValidationError(
            "architecture must be the native runner name x86_64 or aarch64"
        )
    if not _RUN_ID_RE.fullmatch(run_id):
        raise NativeValidationError("run_id must be a positive integer")
    workspace = Path(workspace)
    if not workspace.is_dir():
        raise NativeValidationError("target workspace does not exist")
    dgoss = _validate_tool(dgoss, "dgoss")
    goss = _validate_tool(goss, "goss")
    report_path = Path(report_path)
    junit_path = Path(junit_path)
    repair_report_dir = Path(repair_report_dir)
    start = time.monotonic()
    format_check = _run_optional_format_check(
        format_validator=format_validator,
        workspace=workspace,
        architecture=architecture,
        report_path=report_path,
    )

    context = report_path.parent / "pipeline-smoke-context"
    contexts: dict[str, Path] = {}
    for mode in ("service", "cli"):
        mode_root = context / mode
        mode_root.mkdir(parents=True, exist_ok=True)
        marker = f"/pipeline-smoke-{mode}"
        command = (
            'CMD ["sleep", "300"]\n'
            if mode == "service"
            else 'CMD ["unexpected-image-cmd"]\n'
        )
        (mode_root / "Dockerfile").write_text(
            f"FROM openeuler/openeuler:{task.os_version}\n"
            f"RUN printf 'pipeline-smoke-{mode}\\n' > {marker}\n"
            + command
        )
        (mode_root / "goss.yaml").write_text(
            "file:\n"
            f"  {marker}:\n"
            "    exists: true\n"
        )
        if mode == "service":
            (mode_root / "goss_wait.yaml").write_text(
                "process:\n  sleep:\n    running: true\n"
            )
        test_sh = mode_root / "test.sh"
        test_sh.write_text(
            "#!/bin/sh\nset -eu\n"
            "test \"$#\" -eq 0\n"
            f"test -f {marker}\n"
        )
        test_sh.chmod(0o755)
        contexts[mode] = mode_root

    platform = _PLATFORMS[architecture]
    slug = architecture.replace("_", "-")
    prefix = f"oe-smoke-{run_id}-{slug}"
    builder = f"{prefix}-builder"
    containers = [
        f"{prefix}-{mode}-{kind}"
        for mode in contexts
        for kind in ("dgoss", "runtime")
    ]
    images = {
        mode: f"oe-autopilot/pipeline-smoke-{mode}:{run_id}-{slug}"
        for mode in contexts
    }
    stage = f"smoke:{architecture}"
    validated_patch_sha256 = validated_patch_digest(workspace)
    image_id = ""
    failure: str | None = None
    failure_details: dict[str, object] = {}
    checks: dict[str, bool | None] = {name: None for name in _SMOKE_CHECKS}
    current_check = ""
    log(stage, "START native plumbing")
    try:
        _create_builder(runner, builder, cwd=workspace)
        current_check = "native_build"
        for mode, mode_root in contexts.items():
            _run(
                runner,
                [
                    "docker",
                    "buildx",
                    "build",
                    "--builder",
                    builder,
                    "--no-cache",
                    "--load",
                    "--progress",
                    "plain",
                    "--platform",
                    platform,
                    "--tag",
                    images[mode],
                    str(mode_root),
                ],
                cwd=workspace,
                timeout=1800,
            )
        checks["native_build"] = True
        current_check = "dgoss"
        for mode, mode_root in contexts.items():
            _run_dgoss(
                runner,
                workspace=workspace,
                image=images[mode],
                tests_root=mode_root,
                version=task.version,
                dgoss=dgoss,
                goss=goss,
                container=f"{prefix}-{mode}-dgoss",
                service_mode=mode == "service",
            )
        checks["dgoss"] = True
        current_check = "shared_tests"
        for mode, mode_root in contexts.items():
            _run_shared_tests(
                runner,
                workspace=workspace,
                image=images[mode],
                tests_root=mode_root,
                version=task.version,
                container=f"{prefix}-{mode}-runtime",
                service_mode=mode == "service",
                run_id=run_id,
            )
        checks["shared_tests"] = True
        inspected = _run(
            runner,
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                images["service"],
            ],
            cwd=workspace,
        )
        image_id = str(inspected.stdout or "").strip()
    except NativeValidationError as error:
        failure = str(error)
        failure_details = dict(error.details)
        if current_check:
            checks[current_check] = False
    finally:
        cleanup = [
            ["docker", "rm", "--force", "--volumes", name]
            for name in containers
        ] + [
            ["docker", "image", "rm", "--force", image]
            for image in images.values()
        ] + [["docker", "buildx", "rm", "--force", builder]]
        for command in cleanup:
            _run(
                runner,
                command,
                cwd=workspace,
                timeout=300,
                check=False,
            )

    format_failure = _format_failure(format_check)
    overall_failure = failure or format_failure
    report: dict[str, object] = {
        "status": "failed" if overall_failure else "passed",
        "task_id": task.task_id,
        "architecture": architecture,
        "platform": platform,
        "builder": builder,
        "build_cache": "disabled",
        "image_id": image_id,
        "validated_patch_sha256": validated_patch_sha256,
        "duration_seconds": round(time.monotonic() - start, 3),
        "environment": _environment_evidence(task, architecture),
        "checks": checks,
    }
    if format_check is not None:
        report["format_check"] = format_check
    if failure:
        report["failure"] = failure
        report["failed_stage"] = current_check
        report["failure_details"] = failure_details
    elif format_failure:
        report["failure"] = format_failure
        report["failed_stage"] = "upstream_format"
        report["failure_details"] = _format_failure_details(format_check or {})
    _write_evidence(
        report_path=report_path,
        junit_path=junit_path,
        report=report,
        failure=overall_failure,
    )
    repair_report_dir.mkdir(parents=True, exist_ok=True)
    (repair_report_dir / f"native-repair-{architecture}.json").write_text(
        json.dumps(
            {
                "architecture": architecture,
                "mode": "pipeline_smoke",
                "repair_attempts": 0,
                "status": report["status"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if overall_failure:
        log(stage, f"FAIL native plumbing: {overall_failure}")
        details = (
            failure_details
            if failure
            else _format_failure_details(format_check or {})
        )
        raise NativeValidationError(overall_failure, details=details)
    log(stage, "PASS native plumbing")
    return report
