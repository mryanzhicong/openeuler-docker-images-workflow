import json
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest


class DockerRunner:
    def __init__(
        self,
        *,
        fail_build=False,
        failure_text="source compilation failed",
        fail_dgoss=False,
        fail_shared_tests=False,
        container_logs="",
        container_logs_returncode=0,
        container_state="exited 1 kvrocks refused to start",
        container_probe="",
        container_probe_returncode=0,
    ):
        self.fail_build = fail_build
        self.failure_text = failure_text
        self.fail_dgoss = fail_dgoss
        self.fail_shared_tests = fail_shared_tests
        self.container_logs = container_logs
        self.container_logs_returncode = container_logs_returncode
        self.container_state = container_state
        self.container_probe = container_probe
        self.container_probe_returncode = container_probe_returncode
        self.builders = set()
        self.calls = []

    def __call__(self, command, cwd, env, timeout):
        command = list(command)
        self.calls.append(
            {
                "command": command,
                "cwd": cwd,
                "env": dict(env),
                "timeout": timeout,
            }
        )
        if command[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(
                command, self.container_logs_returncode, self.container_logs, ""
            )
        if command[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(
                command, 0, f"{self.container_state}\n", ""
            )
        if (
            self.fail_dgoss
            and command[0] != "docker"
            and command[1:2] == ["run"]
        ):
            return subprocess.CompletedProcess(
                command, 1, "", self.failure_text
            )
        if command[:2] == ["docker", "exec"] and "### processes" in command[-1]:
            return subprocess.CompletedProcess(
                command, self.container_probe_returncode, self.container_probe, ""
            )
        if self.fail_shared_tests and command[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(
                command, 1, "", "shared test assertion failed"
            )
        if command[:3] == ["docker", "buildx", "inspect"]:
            present = command[3] in self.builders
            return subprocess.CompletedProcess(
                command, 0 if present else 1, "", ""
            )
        if command[:3] == ["docker", "buildx", "ls"]:
            output = "\n".join(sorted(self.builders))
            return subprocess.CompletedProcess(
                command, 0, f"{output}\n" if output else "", ""
            )
        if command[:3] == ["docker", "buildx", "create"]:
            self.builders.add(command[command.index("--name") + 1])
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:4] == ["docker", "buildx", "rm", "--force"]:
            self.builders.discard(command[4])
            return subprocess.CompletedProcess(command, 0, "", "")
        if self.fail_build and "build" in command:
            return subprocess.CompletedProcess(
                command, 1, "", self.failure_text
            )
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, "sha256:image-id\n", "")
        if "PING" in command:
            return subprocess.CompletedProcess(command, 0, "PONG\n", "")
        if "GET" in command:
            return subprocess.CompletedProcess(command, 0, "run-123456\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")


def test_infrastructure_failure_evidence_preserves_clone_error(tmp_path):
    from scripts.lib.native_validation import (
        write_infrastructure_failure_evidence,
    )

    report_path = tmp_path / "round" / "aarch64.json"
    junit_path = tmp_path / "round" / "aarch64.junit.xml"
    failure = (
        "error: RPC failed; curl 18 transfer closed with outstanding read "
        "data remaining\nfatal: early EOF"
    )

    report = write_infrastructure_failure_evidence(
        task=_task(),
        architecture="aarch64",
        failed_stage="target_clone",
        failure=failure,
        report_path=report_path,
        junit_path=junit_path,
        attempts=2,
    )

    assert json.loads(report_path.read_text()) == report
    assert report["status"] == "failed"
    assert report["failed_stage"] == "target_clone"
    assert report["failure"] == failure
    assert report["failure_details"] == {
        "attempts": 2,
        "retryable": True,
    }
    assert report["checks"] == {
        "native_build": None,
        "dgoss": None,
        "shared_tests": None,
    }
    suite = ET.parse(junit_path).getroot()
    assert suite.attrib["failures"] == "1"
    assert failure in suite.find("testcase/failure").text


def _task():
    from scripts.lib.task_spec import TaskSpec

    return TaskSpec.from_workflow_dispatch(
        {
            "app": "kvrocks",
            "version": "2.16.0",
            "os_version": "24.03-lts-sp4",
            "domain": "Database",
            "source_url": "https://github.com/apache/kvrocks/tree/v2.16.0",
        }
    )


def _generic_task(*, app, domain="Others"):
    from scripts.lib.task_spec import TaskSpec

    return TaskSpec.from_workflow_dispatch(
        {
            "app": app,
            "version": "1.2.3",
            "os_version": "24.03-lts-sp4",
            "domain": domain,
            "source_url": f"https://github.com/example/{app}/tree/v1.2.3",
        }
    )


def _git_init(workspace):
    """A validated workspace is a checkout at base SHA with the patch applied."""
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T")):
        subprocess.run(
            ["git", "-C", str(workspace), "config", key, value], check=True
        )
    (workspace / ".keep").write_text("base\n")
    subprocess.run(
        ["git", "-C", str(workspace), "add", "--", ".keep"], check=True
    )
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-qm", "base"], check=True
    )
    return workspace


def _workspace(tmp_path):
    workspace = tmp_path / "target"
    image = workspace / "Database" / "kvrocks" / "2.16.0" / "24.03-lts-sp4"
    tests = workspace / "Database" / "kvrocks" / "tests"
    image.mkdir(parents=True)
    tests.mkdir(parents=True)
    (image / "Dockerfile").write_text("FROM scratch\n")
    (tests / "goss.yaml").write_text("{}\n")
    (tests / "goss_wait.yaml").write_text(
        "process:\n  sleep:\n    running: true\n"
    )
    (tests / "test.sh").write_text("#!/bin/bash\n")
    (tests / "test.sh").chmod(0o755)
    return _git_init(workspace)


def _generic_workspace(tmp_path, task, *, service):
    workspace = tmp_path / "target"
    image = (
        workspace
        / task.domain
        / task.app
        / task.version
        / task.os_version
    )
    tests = workspace / task.domain / task.app / "tests"
    image.mkdir(parents=True)
    tests.mkdir(parents=True)
    (image / "Dockerfile").write_text("FROM scratch\n")
    (tests / "goss.yaml").write_text("{}\n")
    if service:
        (tests / "goss_wait.yaml").write_text(
            "process:\n  sleep:\n    running: true\n"
        )
    (tests / "test.sh").write_text("#!/bin/sh\nexit 0\n")
    (tests / "test.sh").chmod(0o755)
    return _git_init(workspace)


def _tools(tmp_path):
    dgoss = tmp_path / "dgoss"
    goss = tmp_path / "goss"
    dgoss.write_text("#!/bin/sh\n")
    goss.write_text("#!/bin/sh\n")
    dgoss.chmod(0o755)
    goss.chmod(0o755)
    return dgoss, goss


def test_native_validation_uses_dedicated_builder_and_generated_runtime_checks(
    tmp_path,
    capsys,
):
    from scripts.lib.native_validation import validate_native_image

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    report_path = tmp_path / "reports" / "x86_64.json"
    junit_path = tmp_path / "reports" / "x86_64.junit.xml"
    runner = DockerRunner()

    report = validate_native_image(
        workspace=workspace,
        task=_task(),
        architecture="x86_64",
        run_id="123456",
        dgoss=dgoss,
        goss=goss,
        report_path=report_path,
        junit_path=junit_path,
        runner=runner,
        sleep=lambda _: None,
    )

    assert report["status"] == "passed"
    assert report["architecture"] == "x86_64"
    assert report["platform"] == "linux/amd64"
    assert report["builder"] == "oe-e2e-123456-x86-64-builder"
    assert report["build_cache"] == "disabled"
    assert report["image_id"] == "sha256:image-id"
    assert set(report["environment"]) == {
        "test_time",
        "Model",
        "architecture",
        "kernel",
        "os",
        "cpu_model",
        "cpu_cores",
        "software_name",
        "software_version",
        "python_version",
        "numpy_version",
    }
    assert report["environment"]["architecture"] == "x86_64"
    assert report["environment"]["software_version"] == "2.16.0"
    assert json.loads(report_path.read_text()) == report
    suite = ET.parse(junit_path).getroot()
    assert suite.attrib["failures"] == "0"

    commands = [call["command"] for call in runner.calls]
    flattened = "\n".join(" ".join(command) for command in commands)
    assert "docker buildx create" in flattened
    assert "--driver docker-container" in flattened
    assert "docker buildx build" in flattened
    assert "--no-cache" in flattened
    assert "--platform linux/amd64" in flattened
    dgoss_call = runner.calls[
        [command[0] for command in commands].index(str(dgoss))
    ]
    assert str(dgoss) in dgoss_call["command"][0]
    assert dgoss_call["env"]["GOSS_FILES_PATH"] == str(
        workspace / "Database" / "kvrocks" / "tests"
    )
    assert dgoss_call["env"]["GOSS_FILE"] == "goss.yaml"
    assert dgoss_call["env"]["GOSS_WAIT_OPTS"] == "-r 30s -s 1s"
    assert "GOSS_WAIT_FILE" not in dgoss_call["env"]
    assert dgoss_call["command"][-3:] == [
        "--env",
        "EXPECTED_VERSION=2.16.0",
        "oe-autopilot/kvrocks:2.16.0-123456-x86-64",
    ]
    assert "docker image rm" in flattened
    assert "system prune" not in flattened
    assert "setup-qemu" not in flattened
    assert "docker buildx rm" in flattened
    assert "--use" not in flattened
    output = capsys.readouterr().out
    markers = [
        "[flow][native:x86_64] START validation",
        "[flow][native:x86_64] START build",
        "[flow][native:x86_64] PASS build",
        "[flow][native:x86_64] START dgoss",
        "[flow][native:x86_64] PASS dgoss",
        "[flow][native:x86_64] PASS validation",
    ]
    positions = [output.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_native_service_uses_test_assets_without_application_hardcoding(
    tmp_path,
):
    from scripts.lib.native_validation import validate_native_image

    task = _generic_task(app="echo-server")
    workspace = _generic_workspace(tmp_path, task, service=True)
    dgoss, goss = _tools(tmp_path)
    runner = DockerRunner()

    report = validate_native_image(
        workspace=workspace,
        task=task,
        architecture="x86_64",
        run_id="123456",
        dgoss=dgoss,
        goss=goss,
        report_path=tmp_path / "reports/x86_64.json",
        junit_path=tmp_path / "reports/x86_64.junit.xml",
        runner=runner,
        sleep=lambda _: None,
    )

    assert report["checks"] == {
        "native_build": True,
        "dgoss": True,
        "shared_tests": True,
    }
    commands = "\n".join(
        " ".join(call["command"]) for call in runner.calls
    ).lower()
    for application_literal in (
        "kvrocks",
        "redis-cli",
        "6666",
        "/var/lib/kvrocks",
    ):
        assert application_literal not in commands
    assert "docker run --detach" in commands
    assert "docker exec" in commands


def test_native_cli_runs_shared_tests_without_a_detached_service(tmp_path):
    from scripts.lib.native_validation import validate_native_image

    task = _generic_task(app="batch-tool", domain="HPC")
    workspace = _generic_workspace(tmp_path, task, service=False)
    dgoss, goss = _tools(tmp_path)
    runner = DockerRunner()

    report = validate_native_image(
        workspace=workspace,
        task=task,
        architecture="aarch64",
        run_id="123456",
        dgoss=dgoss,
        goss=goss,
        report_path=tmp_path / "reports/aarch64.json",
        junit_path=tmp_path / "reports/aarch64.junit.xml",
        runner=runner,
        sleep=lambda _: None,
    )

    assert report["status"] == "passed"
    assert report["checks"]["shared_tests"] is True
    commands = [call["command"] for call in runner.calls]
    assert not any(
        command[:3] == ["docker", "run", "--detach"]
        for command in commands
    )
    assert not any(
        command[:2] == ["docker", "exec"] for command in commands
    )
    cli_run = next(
        command
        for command in commands
        if command[:2] == ["docker", "run"]
        and "--volume" in command
    )
    assert cli_run[cli_run.index("--entrypoint") + 1] == "/bin/sh"
    assert cli_run[cli_run.index("--label") + 1] == (
        "oe.autopilot.run=123456"
    )
    # An explicit command follows the image, so Docker cannot append the
    # image's original CMD as positional arguments to test.sh.
    assert cli_run[-2:] == ["-c", "exec /opt/oe-tests/test.sh"]


def test_invalid_goss_contract_skips_only_dgoss(
    tmp_path,
):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    task = _generic_task(app="broken-tests")
    workspace = _generic_workspace(tmp_path, task, service=True)
    tests = workspace / task.domain / task.app / "tests"
    (tests / "goss.yaml").write_text("command: [\n")
    dgoss, goss = _tools(tmp_path)
    runner = DockerRunner()
    report_path = tmp_path / "reports/x86_64.json"

    with pytest.raises(NativeValidationError, match="test contract"):
        validate_native_image(
            workspace=workspace,
            task=task,
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=tmp_path / "reports/x86_64.junit.xml",
            runner=runner,
            sleep=lambda _: None,
        )

    report = json.loads(report_path.read_text())
    assert report["failed_stage"] == "test_contract"
    assert report["checks"] == {
        "native_build": True,
        "dgoss": False,
        "shared_tests": True,
    }
    assert report["failures"][0]["check"] == "dgoss"
    commands = "\n".join(
        " ".join(call["command"]) for call in runner.calls
    )
    assert "docker buildx build" in commands
    assert str(dgoss) not in commands
    assert "docker exec" in commands


def test_native_validation_failure_still_cleans_exact_resources(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    report_path = tmp_path / "reports" / "aarch64.json"
    junit_path = tmp_path / "reports" / "aarch64.junit.xml"
    runner = DockerRunner(fail_build=True)

    with pytest.raises(NativeValidationError, match="source compilation failed"):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="aarch64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=junit_path,
            runner=runner,
            sleep=lambda _: None,
        )

    report = json.loads(report_path.read_text())
    assert report["status"] == "failed"
    assert report["architecture"] == "aarch64"
    suite = ET.parse(junit_path).getroot()
    assert suite.attrib["failures"] == "1"
    flattened = "\n".join(
        " ".join(call["command"]) for call in runner.calls
    )
    assert "docker image rm" in flattened
    assert "system prune" not in flattened
    assert "docker buildx rm" in flattened


def test_native_validation_failure_keeps_both_ends_of_a_long_log(tmp_path):
    """The Fixer prompt asks for the earliest error, so a tail is not enough.

    Keeping only the tail hid exactly what the Fixer is told to look for, and
    a bare string hid the exit code that separates a missing command from a
    real compile failure.
    """
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    report_path = tmp_path / "reports" / "x86_64.json"
    first_error = "CMake Error: could not find libstdc++.a"
    root_cause = "groupadd: GID '999' already exists"
    runner = DockerRunner(
        fail_build=True,
        failure_text=(
            first_error + "\n" + ("package progress\n" * 500) + root_cause
        ),
    )

    with pytest.raises(NativeValidationError, match="GID '999'"):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=tmp_path / "reports" / "x86_64.junit.xml",
            runner=runner,
            sleep=lambda _: None,
        )

    report = json.loads(report_path.read_text())
    failure = report["failure"]
    assert first_error in failure
    assert root_cause in failure
    details = report["failure_details"]
    assert first_error in details["stdout_head"]
    assert root_cause in details["stdout_tail"]
    assert details["returncode"] == 1
    assert "docker buildx build" in " ".join(details["command"])


def test_dgoss_failure_does_not_skip_shared_tests(
    tmp_path,
):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    report_path = tmp_path / "reports" / "x86_64.json"
    runner = DockerRunner(fail_dgoss=True, failure_text="goss: invalid Attribute")

    with pytest.raises(NativeValidationError, match="invalid Attribute"):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=tmp_path / "reports" / "x86_64.junit.xml",
            runner=runner,
            sleep=lambda _: None,
        )

    report = json.loads(report_path.read_text())
    assert report["failed_stage"] == "dgoss"
    assert report["checks"] == {
        "native_build": True,
        "dgoss": False,
        "shared_tests": True,
    }
    assert [failure["stage"] for failure in report["failures"]] == ["dgoss"]
    assert any(
        call["command"][:2] == ["docker", "exec"] for call in runner.calls
    )


def test_native_validation_aggregates_dgoss_and_shared_test_failures(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    report_path = tmp_path / "reports" / "x86_64.json"
    runner = DockerRunner(
        fail_dgoss=True,
        fail_shared_tests=True,
        failure_text="goss assertion failed",
    )

    with pytest.raises(NativeValidationError, match="goss assertion failed"):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=tmp_path / "reports" / "x86_64.junit.xml",
            runner=runner,
            sleep=lambda _: None,
        )

    report = json.loads(report_path.read_text())
    assert report["checks"] == {
        "native_build": True,
        "dgoss": False,
        "shared_tests": False,
    }
    assert [failure["stage"] for failure in report["failures"]] == [
        "dgoss",
        "shared_tests",
    ]
    assert "goss assertion failed" in report["failures"][0]["failure"]
    assert "shared test assertion failed" in report["failures"][1]["failure"]


def test_invalid_shared_test_contract_does_not_skip_goss(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    (workspace / "Database" / "kvrocks" / "tests" / "test.sh").write_text(
        "#!/bin/bash\nif true; then\n"
    )
    dgoss, goss = _tools(tmp_path)
    report_path = tmp_path / "reports" / "x86_64.json"
    runner = DockerRunner()

    with pytest.raises(NativeValidationError, match="test.sh is not valid Bash"):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=tmp_path / "reports" / "x86_64.junit.xml",
            runner=runner,
            sleep=lambda _: None,
        )

    report = json.loads(report_path.read_text())
    assert report["checks"] == {
        "native_build": True,
        "dgoss": True,
        "shared_tests": False,
    }
    assert report["failures"][0]["check"] == "shared_tests"
    assert report["failures"][0]["stage"] == "test_contract"
    assert any(call["command"][0] == str(dgoss) for call in runner.calls)


def test_format_failure_is_recorded_but_native_validation_still_runs(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    runner = DockerRunner()
    report_path = tmp_path / "reports" / "aarch64.json"

    def failing_format_check(**_):
        return {
            "status": "failed",
            "kind": "candidate",
            "stage": "execute",
            "repository": "https://gitcode.com/openeuler/eulerpublisher.git",
            "commit_sha": "a" * 40,
            "runner_architecture": "aarch64",
            "compatibility_override": True,
            "fail_count": 1,
            "output": "image-info.yml is missing environment",
            "failure": "upstream format check reported 1 failure",
        }

    with pytest.raises(NativeValidationError, match="format check"):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="aarch64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=tmp_path / "reports" / "aarch64.junit.xml",
            runner=runner,
            sleep=lambda _: None,
            format_validator=failing_format_check,
        )

    report = json.loads(report_path.read_text())
    assert report["status"] == "failed"
    assert report["failed_stage"] == "upstream_format"
    assert report["format_check"]["kind"] == "candidate"
    assert report["format_check"]["commit_sha"] == "a" * 40
    assert report["checks"] == {
        "native_build": True,
        "dgoss": True,
        "shared_tests": True,
    }
    commands = "\n".join(" ".join(call["command"]) for call in runner.calls)
    assert "docker buildx build" in commands
    assert "--no-cache" in commands
    assert str(dgoss) in commands
    assert "docker exec" in commands


def test_native_validation_failure_captures_container_state_before_cleanup(
    tmp_path,
):
    """When the app dies on startup its own log is the only root-cause source."""
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    report_path = tmp_path / "reports" / "x86_64.json"
    runner = DockerRunner(
        fail_dgoss=True,
        failure_text="dgoss failed",
        container_logs="FATAL: cannot bind 0.0.0.0:6666\n",
    )

    with pytest.raises(NativeValidationError):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=tmp_path / "reports" / "x86_64.junit.xml",
            runner=runner,
            sleep=lambda _: None,
        )

    evidence = json.loads(report_path.read_text())["container_evidence"]
    collected = "\n".join(
        f"{entry['state']}\n{entry['logs']}" for entry in evidence.values()
    )
    assert "cannot bind 0.0.0.0:6666" in collected
    assert "kvrocks refused to start" in collected
    ordered = [" ".join(call["command"]) for call in runner.calls]
    logs_at = next(i for i, c in enumerate(ordered) if c.startswith("docker logs"))
    removed_at = next(
        i for i, c in enumerate(ordered) if c.startswith("docker rm --force")
    )
    assert logs_at < removed_at


def test_native_validation_failure_probes_inside_the_container(tmp_path):
    """docker logs can carry the symptom while the cause stays in the image.

    Run 31106121623 got two lines -- "export properties error" and a missing
    kylin.out -- and the Fixer then spent 22 minutes rebuilding Kylin locally
    to read a shell.stderr the container already held.
    """
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    report_path = tmp_path / "reports" / "x86_64.json"
    runner = DockerRunner(
        fail_dgoss=True,
        failure_text="dgoss failed",
        container_logs="export properties error\n",
        container_probe=(
            "### processes\n"
            "UID  PID  CMD\n"
            "### /home/kylin/apache-kylin-5.0.3-bin/logs/shell.stderr\n"
            "java.lang.ClassNotFoundException: org.apache.commons.io.FileUtils\n"
        ),
    )

    with pytest.raises(NativeValidationError):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=tmp_path / "reports" / "x86_64.junit.xml",
            runner=runner,
            sleep=lambda _: None,
        )

    evidence = json.loads(report_path.read_text())["container_evidence"]
    probes = "\n".join(str(entry["probe"]) for entry in evidence.values())
    assert "shell.stderr" in probes
    assert "ClassNotFoundException" in probes

    runtime = next(name for name in evidence if name.endswith("runtime"))
    saved = report_path.parent / evidence[runtime]["full_probe"]["path"]
    assert saved.name == f"{runtime}.probe.log"
    assert "ClassNotFoundException" in saved.read_text()
    assert evidence[runtime]["full_probe"]["capture_status"] == "complete"

    probe_at = next(
        index
        for index, call in enumerate(runner.calls)
        if call["command"][:2] == ["docker", "exec"]
        and "### processes" in call["command"][-1]
    )
    removed_at = next(
        index
        for index, call in enumerate(runner.calls)
        if " ".join(call["command"]).startswith("docker rm --force")
    )
    assert probe_at < removed_at


def test_container_probe_searches_by_shape_not_by_application(tmp_path):
    """The probe cannot know an image's log layout, so it must not assume one."""
    from scripts.lib.native_validation import _probe_script

    script = _probe_script()

    for root in ("/opt", "/home", "/var/log"):
        assert root in script
    for pattern in ("*.log", "*.stderr"):
        assert pattern in script
    assert "kylin" not in script.lower()
    # Best-effort throughout: an unusual image still returns what it could.
    assert "2>/dev/null" in script


def test_container_probe_bounds_its_walk_on_a_bigdata_image():
    """A Spark tree holds tens of thousands of files the probe must not walk."""
    from scripts.lib.native_validation import _probe_script

    script = _probe_script()

    assert "-xdev" in script
    assert "-maxdepth 6" in script
    assert "-mmin -180" in script
    # head closes the pipe, which ends find once the quota is met.
    assert "head -n 20" in script


def test_native_validation_failure_saves_complete_container_evidence_artifacts(
    tmp_path,
):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    report_path = tmp_path / "reports" / "x86_64.json"
    container_logs = "".join(f"line-{index:03d}\n" for index in range(250))
    runner = DockerRunner(
        fail_dgoss=True,
        failure_text="dgoss failed",
        container_logs=container_logs,
    )

    with pytest.raises(NativeValidationError):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=tmp_path / "reports" / "x86_64.junit.xml",
            runner=runner,
            sleep=lambda _: None,
        )

    report = json.loads(report_path.read_text())
    runtime = "oe-e2e-123456-x86-64-runtime"
    evidence = report["container_evidence"][runtime]
    log_metadata = evidence["full_logs"]
    log_path = report_path.parent / log_metadata["path"]
    assert log_path.read_text() == container_logs
    assert log_metadata["size_bytes"] == len(container_logs.encode())
    assert log_metadata["capture_status"] == "complete"
    assert set(log_metadata) == {"path", "size_bytes", "capture_status"}
    assert "line-000" not in evidence["logs"]
    assert "line-249" in evidence["logs"]

    assert "full_inspect" not in evidence
    docker_log_commands = [
        call["command"]
        for call in runner.calls
        if call["command"][:2] == ["docker", "logs"]
    ]
    assert docker_log_commands
    assert all("--tail" not in command for command in docker_log_commands)


def test_native_validation_marks_timed_out_container_logs_as_incomplete(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    report_path = tmp_path / "reports" / "x86_64.json"
    runner = DockerRunner(
        fail_dgoss=True,
        container_logs="partial log\n",
        container_logs_returncode=124,
    )

    with pytest.raises(NativeValidationError):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=tmp_path / "reports" / "x86_64.junit.xml",
            runner=runner,
            sleep=lambda _: None,
        )

    runtime = "oe-e2e-123456-x86-64-runtime"
    metadata = json.loads(report_path.read_text())["container_evidence"][runtime][
        "full_logs"
    ]
    assert metadata["capture_status"] == "timeout"
    assert set(metadata) == {"path", "size_bytes", "capture_status"}


def test_container_evidence_failure_does_not_replace_native_report(
    tmp_path,
    monkeypatch,
):
    from scripts.lib import native_validation

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    report_path = tmp_path / "reports" / "x86_64.json"
    runner = DockerRunner(fail_dgoss=True, container_logs="application failed\n")

    def fail_evidence_write(**kwargs):
        raise OSError("diagnostics unavailable")

    monkeypatch.setattr(
        native_validation,
        "_write_full_evidence",
        fail_evidence_write,
    )

    with pytest.raises(native_validation.NativeValidationError):
        native_validation.validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=tmp_path / "reports" / "x86_64.junit.xml",
            runner=runner,
            sleep=lambda _: None,
        )

    report = json.loads(report_path.read_text())
    assert report["status"] == "failed"
    assert report["failed_stage"] == "dgoss"
    assert report["container_evidence"]["capture_error"] == (
        "diagnostics unavailable"
    )


def test_full_evidence_metadata_is_minimal(tmp_path):
    from scripts.lib.native_validation import _write_full_evidence

    metadata = _write_full_evidence(
        artifact_root=tmp_path,
        diagnostics_dir=tmp_path / "diagnostics",
        name="runtime",
        suffix="docker.log",
        content="complete log\n",
    )

    assert metadata == {
        "path": "diagnostics/runtime.docker.log",
        "size_bytes": len(b"complete log\n"),
    }


def test_streamed_command_evidence_writes_output_directly_to_file(tmp_path):
    from scripts.lib import native_validation

    capture = getattr(native_validation, "_stream_command_evidence", None)
    assert callable(capture)
    path = tmp_path / "diagnostics" / "runtime.docker.log"
    command = [
        sys.executable,
        "-c",
        "for index in range(10000): print(f'line-{index:05d}')",
    ]

    metadata, summary = capture(
        command=command,
        cwd=tmp_path,
        artifact_root=tmp_path,
        path=path,
        timeout=30,
    )

    payload = path.read_bytes()
    assert payload.startswith(b"line-00000\n")
    assert payload.endswith(b"line-09999\n")
    assert metadata["size_bytes"] == len(payload)
    assert metadata["capture_status"] == "complete"
    assert set(metadata) == {"path", "size_bytes", "capture_status"}
    assert "line-00000" not in summary
    assert "line-09999" in summary


def test_native_pipeline_smoke_builds_and_runs_dgoss_without_ai(
    tmp_path,
):
    from scripts.lib.native_validation import validate_native_smoke

    workspace = _git_init(tmp_path / "target")
    dgoss, goss = _tools(tmp_path)
    runner = DockerRunner()
    report_path = tmp_path / "reports" / "x86_64.json"
    junit_path = tmp_path / "reports" / "x86_64.junit.xml"
    repair_dir = tmp_path / "reports" / "agents"

    report = validate_native_smoke(
        workspace=workspace,
        task=_task(),
        architecture="x86_64",
        run_id="123456",
        dgoss=dgoss,
        goss=goss,
        report_path=report_path,
        junit_path=junit_path,
        repair_report_dir=repair_dir,
        runner=runner,
    )

    assert report["status"] == "passed"
    assert report["builder"] == "oe-smoke-123456-x86-64-builder"
    assert report["build_cache"] == "disabled"
    assert report["checks"] == {
        "native_build": True,
        "dgoss": True,
        "shared_tests": True,
    }
    commands = "\n".join(
        " ".join(call["command"]) for call in runner.calls
    )
    assert "docker buildx build" in commands
    assert str(dgoss) in commands
    assert "docker image inspect" in commands
    assert "docker image rm" in commands
    assert "docker buildx rm" in commands
    assert "docker exec" in commands
    dgoss_calls = [
        call for call in runner.calls if call["command"][0] == str(dgoss)
    ]
    assert len(dgoss_calls) == 2
    assert {
        call["env"]["GOSS_FILES_PATH"].rsplit("/", 1)[-1]
        for call in dgoss_calls
    } == {"service", "cli"}
    assert all(call["env"]["GOSS_FILE"] == "goss.yaml" for call in dgoss_calls)
    cli_shared_test = next(
        call["command"]
        for call in runner.calls
        if call["command"][:2] == ["docker", "run"]
        and "exec /opt/oe-tests/test.sh" in call["command"]
    )
    assert cli_shared_test[-2:] == ["-c", "exec /opt/oe-tests/test.sh"]
    assert (
        repair_dir / "native-repair-x86_64.json"
    ).is_file()


def test_smoke_format_failure_does_not_skip_native_plumbing(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_smoke,
    )

    workspace = _git_init(tmp_path / "target")
    dgoss, goss = _tools(tmp_path)
    runner = DockerRunner()
    report_path = tmp_path / "reports" / "x86_64.json"

    def failing_format_check(**_):
        return {
            "status": "failed",
            "kind": "candidate",
            "stage": "execute",
            "commit_sha": "a" * 40,
            "failure": "upstream format check reported 1 failure",
        }

    with pytest.raises(NativeValidationError, match="format check"):
        validate_native_smoke(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=tmp_path / "reports" / "x86_64.junit.xml",
            repair_report_dir=tmp_path / "reports" / "agents",
            runner=runner,
            format_validator=failing_format_check,
        )

    report = json.loads(report_path.read_text())
    assert report["failed_stage"] == "upstream_format"
    assert report["format_check"]["commit_sha"] == "a" * 40
    assert report["checks"] == {
        "native_build": True,
        "dgoss": True,
        "shared_tests": True,
    }


def test_repeated_validation_creates_and_removes_a_fresh_builder(
    tmp_path,
):
    from scripts.lib.native_validation import validate_native_image

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    runner = DockerRunner()

    for attempt in range(2):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=tmp_path / f"reports/{attempt}/x86_64.json",
            junit_path=tmp_path / f"reports/{attempt}/x86_64.junit.xml",
            runner=runner,
            sleep=lambda _: None,
        )

    commands = [call["command"] for call in runner.calls]
    creates = [command for command in commands if command[:3] == [
        "docker", "buildx", "create",
    ]]
    inspects = [command for command in commands if command[:3] == [
        "docker", "buildx", "inspect",
    ]]
    removes = [command for command in commands if command[:4] == [
        "docker", "buildx", "rm", "--force",
    ]]
    builds = [command for command in commands if command[:3] == [
        "docker", "buildx", "build",
    ]]
    assert inspects == []
    assert len(creates) == 2
    assert len(removes) == 2
    assert all("--no-cache" in command for command in builds)
    assert creates[0][creates[0].index("--name") + 1] == (
        "oe-e2e-123456-x86-64-builder"
    )


def test_report_records_the_candidate_content_that_was_validated(tmp_path):
    from scripts.lib.native_validation import validate_native_image

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)

    def _validate(attempt):
        return validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=tmp_path / f"reports/{attempt}/x86_64.json",
            junit_path=tmp_path / f"reports/{attempt}/x86_64.junit.xml",
            runner=DockerRunner(),
            sleep=lambda _: None,
        )

    before = _validate(0)["validated_patch_sha256"]
    dockerfile = (
        workspace / "Database" / "kvrocks" / "2.16.0" / "24.03-lts-sp4"
        / "Dockerfile"
    )
    dockerfile.write_text("FROM scratch\nRUN true\n")
    after = _validate(1)["validated_patch_sha256"]

    assert len(before) == 64
    # A Fixer edit between rounds must be visible in the recorded digest,
    # otherwise the digest cannot prove what each architecture validated.
    assert before != after


@pytest.mark.parametrize("architecture", ["amd64", "arm64", "../x86_64"])
def test_native_validation_rejects_non_runner_architecture_names(
    tmp_path, architecture
):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    runner = DockerRunner()

    with pytest.raises(NativeValidationError, match="architecture"):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture=architecture,
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=tmp_path / "report.json",
            junit_path=tmp_path / "report.xml",
            runner=runner,
        )

    assert runner.calls == []
