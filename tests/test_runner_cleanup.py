import json
import subprocess

import pytest


def test_clean_native_runner_removes_managed_docker_state(tmp_path):
    from scripts.runner_cleanup import clean_native_runner

    commands = []

    def runner(command, cwd, timeout):
        commands.append(list(command))
        stdout = ""
        if command[:3] == ["docker", "buildx", "ls"]:
            stdout = "default\noe-e2e-12-x86-64-builder\n"
        if command[:4] == ["docker", "ps", "--all", "--quiet"]:
            stdout = "container-1\ncontainer-2\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    report_path = tmp_path / "cleanup.json"
    job_temp = tmp_path / "runner-temp"
    for name in ("phase1-input", "phase1-target", "phase1-round"):
        directory = job_temp / name
        directory.mkdir(parents=True)
        (directory / "stale").write_text("stale")
    report = clean_native_runner(
        workspace=tmp_path,
        job_temp=job_temp,
        architecture="x86_64",
        phase="before",
        run_id="123456",
        run_attempt="1",
        round_number="2",
        runner_name="x86-runner-1",
        report_path=report_path,
        machine="amd64",
        runner=runner,
    )

    assert report["status"] == "passed"
    assert report["run_id"] == "123456"
    assert report["run_attempt"] == "1"
    assert report["round"] == "2"
    assert report["runner_name"] == "x86-runner-1"
    assert all(
        not (job_temp / name).exists()
        for name in ("phase1-input", "phase1-target", "phase1-round")
    )
    assert [
        "docker",
        "buildx",
        "rm",
        "--force",
        "oe-e2e-12-x86-64-builder",
    ] in commands
    assert ["docker", "buildx", "rm", "--force", "default"] not in commands
    assert [
        "docker",
        "rm",
        "--force",
        "--volumes",
        "container-1",
        "container-2",
    ] in commands
    assert ["docker", "builder", "prune", "--all", "--force"] in commands
    assert ["docker", "image", "prune", "--all", "--force"] in commands
    assert ["docker", "volume", "prune", "--force"] in commands
    assert ["docker", "network", "prune", "--force"] in commands
    assert json.loads(report_path.read_text())["status"] == "passed"


def test_clean_native_runner_records_every_cleanup_failure(tmp_path):
    from scripts.runner_cleanup import RunnerCleanupError, clean_native_runner

    def runner(command, cwd, timeout):
        failing = (["builder", "prune"], ["image", "prune"])
        returncode = 1 if command[1:3] in failing else 0
        return subprocess.CompletedProcess(command, returncode, "", "failed")

    report_path = tmp_path / "cleanup.json"
    with pytest.raises(RunnerCleanupError, match="builder prune"):
        clean_native_runner(
            workspace=tmp_path,
            job_temp=tmp_path / "runner-temp",
            architecture="aarch64",
            phase="after",
            run_id="123456",
            run_attempt="2",
            round_number="3",
            runner_name="arm-runner-1",
            report_path=report_path,
            machine="arm64",
            runner=runner,
        )

    report = json.loads(report_path.read_text())
    assert report["status"] == "failed"
    assert len(report["failures"]) == 2


def test_clean_native_runner_fails_closed_on_wrong_architecture(tmp_path):
    from scripts.runner_cleanup import RunnerCleanupError, clean_native_runner

    commands = []
    report_path = tmp_path / "cleanup.json"
    with pytest.raises(RunnerCleanupError, match="architecture mismatch"):
        clean_native_runner(
            workspace=tmp_path,
            job_temp=tmp_path / "runner-temp",
            architecture="aarch64",
            phase="before",
            run_id="123456",
            run_attempt="1",
            round_number="1",
            runner_name="arm-runner-2",
            report_path=report_path,
            machine="x86_64",
            runner=lambda command, cwd, timeout: commands.append(command),
        )

    assert commands == []
    assert json.loads(report_path.read_text())["status"] == "failed"


@pytest.mark.parametrize(
    ("field", "value"),
    (("run_id", "0"), ("run_attempt", "bad"), ("round_number", "")),
)
def test_clean_native_runner_rejects_invalid_execution_identity(
    tmp_path, field, value
):
    from scripts.runner_cleanup import clean_native_runner

    arguments = {
        "run_id": "123456",
        "run_attempt": "1",
        "round_number": "1",
    }
    arguments[field] = value
    with pytest.raises(ValueError, match="positive integer"):
        clean_native_runner(
            workspace=tmp_path,
            job_temp=tmp_path / "runner-temp",
            architecture="x86_64",
            phase="before",
            runner_name="x86-runner-1",
            report_path=tmp_path / "cleanup.json",
            machine="x86_64",
            runner=lambda command, cwd, timeout: None,
            **arguments,
        )


def test_clean_native_runner_rejects_empty_runner_name(tmp_path):
    from scripts.runner_cleanup import clean_native_runner

    with pytest.raises(ValueError, match="runner_name"):
        clean_native_runner(
            workspace=tmp_path,
            job_temp=tmp_path / "runner-temp",
            architecture="x86_64",
            phase="before",
            run_id="123456",
            run_attempt="1",
            round_number="1",
            runner_name=" ",
            report_path=tmp_path / "cleanup.json",
            machine="x86_64",
        )


def test_after_cleanup_preserves_evidence_directory(tmp_path):
    from scripts.runner_cleanup import clean_native_runner

    job_temp = tmp_path / "runner-temp"
    evidence = job_temp / "phase1-round"
    evidence.mkdir(parents=True)
    (evidence / "x86_64.json").write_text("{}")
    for name in ("phase1-input", "phase1-target"):
        (job_temp / name).mkdir()

    clean_native_runner(
        workspace=tmp_path,
        job_temp=job_temp,
        architecture="x86_64",
        phase="after",
        run_id="123456",
        run_attempt="1",
        round_number="1",
        runner_name="x86-runner-1",
        report_path=evidence / "cleanup-after-x86_64.json",
        machine="x86_64",
        runner=lambda command, cwd, timeout: subprocess.CompletedProcess(
            command, 0, "", ""
        ),
    )

    assert evidence.is_dir()
    assert not (job_temp / "phase1-input").exists()
    assert not (job_temp / "phase1-target").exists()
