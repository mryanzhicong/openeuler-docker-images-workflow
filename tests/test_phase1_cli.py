import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "harness" / "flow.py"
GATE_CLI = ROOT / "scripts" / "harness" / "gate_diff.py"
ARTIFACT_CLI = ROOT / "scripts" / "utils" / "artifacts.py"
BASE_SHA = "1d49c0858d8d8152acb1bd3caf5cd862b091160f"


def _run(*args):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _run_script(script, *args):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _write_candidate_payload(root):
    (root / "reports" / "agents").mkdir(parents=True)
    (root / "changes.patch").write_text("diff --git a/a b/a\n")
    report = {
        "status": "passed",
        "checks": {
            "native_build": True,
            "dgoss": True,
            "shared_tests": True,
        },
    }
    (root / "reports" / "x86_64.json").write_text(json.dumps(report) + "\n")
    (root / "reports" / "aarch64.json").write_text(json.dumps(report) + "\n")
    for architecture in ("x86_64", "aarch64"):
        (root / "reports" / f"{architecture}.junit.xml").write_text(
            '<testsuite tests="1" failures="0" errors="0"/>'
        )
    gate = '{"status":"passed","delivery_allowed":true}\n'
    (root / "reports" / "gates.json").write_text(gate)
    (root / "reports" / "generation-gates.json").write_text(gate)
    (root / "reports" / "hadolint.txt").write_text("")


def _phase1_decide_args(tmp_path, reports, *, round_number=1):
    workspace = tmp_path / "target"
    report_dir = tmp_path / "decision" / "agents"
    task_spec = tmp_path / "task-spec.json"
    x86_report = tmp_path / "x86_64.json"
    arm_report = tmp_path / "aarch64.json"
    workspace.mkdir(exist_ok=True)
    task_spec.write_text(
        json.dumps(
            {
                "app": "kvrocks",
                "version": "2.16.0",
                "os_version": "24.03-lts-sp4",
                "domain": "Database",
                "source_url": "https://github.com/apache/kvrocks/tree/v2.16.0",
                "scenario": "new-image",
            }
        )
    )
    x86_report.write_text(json.dumps(reports["x86_64"]))
    arm_report.write_text(json.dumps(reports["aarch64"]))
    return report_dir, (
        "phase1-decide",
        "--workspace",
        str(workspace),
        "--task-spec",
        str(task_spec),
        "--base-sha",
        BASE_SHA,
        "--round",
        str(round_number),
        "--max-rounds",
        "3",
        "--x86-report",
        str(x86_report),
        "--arm-report",
        str(arm_report),
        "--report-dir",
        str(report_dir),
        "--opencode",
        str(tmp_path / "opencode"),
    )


def _passed_native_report(*, commit_sha=""):
    report = {
        "status": "passed",
        "checks": {
            "native_build": True,
            "dgoss": True,
            "shared_tests": True,
        },
        "validated_patch_sha256": "a" * 64,
    }
    if commit_sha:
        report["format_check"] = {
            "status": "passed",
            "kind": "candidate",
            "commit_sha": commit_sha,
        }
    return report


def _infra_native_report():
    return {
        "status": "failed",
        "checks": {
            "native_build": False,
            "dgoss": None,
            "shared_tests": None,
        },
        "failed_stage": "native_build",
        "failure": "timed out",
        "failure_details": {"returncode": 124},
    }


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _upstream(tmp_path):
    repo = tmp_path / "upstream"
    subprocess.run(
        ["git", "init", "-b", "master", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.com")
    (repo / "README.md").write_text("upstream\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_task_spec_command_writes_normalized_contract(tmp_path):
    output = tmp_path / "task-spec.json"

    result = _run(
        "task-spec",
        "--app",
        "Kvrocks",
        "--version",
        "2.16.0",
        "--os-version",
        "24.03-LTS-SP4",
        "--domain",
        "database",
        "--source-url",
        "https://github.com/apache/kvrocks",
        "--output",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["task_id"].startswith("new-image-database-kvrocks")
    assert json.loads(output.read_text())["app"] == "kvrocks"


def test_task_spec_command_fails_without_writing_unsafe_input(tmp_path):
    output = tmp_path / "task-spec.json"

    result = _run(
        "task-spec",
        "--app",
        "../kvrocks",
        "--version",
        "2.16.0",
        "--os-version",
        "24.03-lts-sp4",
        "--domain",
        "Database",
        "--source-url",
        "https://github.com/apache/kvrocks",
        "--output",
        str(output),
    )

    assert result.returncode == 2
    assert "app:" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    ("outcome", "reports", "expected"),
    (
        (
            "converged",
            {
                "x86_64": _passed_native_report(),
                "aarch64": _passed_native_report(),
            },
            {"converged": True, "terminal_status": ""},
        ),
        (
            "format checker mismatch",
            {
                "x86_64": _passed_native_report(commit_sha="a" * 40),
                "aarch64": _passed_native_report(commit_sha="b" * 40),
            },
            {"converged": False, "terminal_status": ""},
        ),
        (
            "infrastructure retry",
            {
                "x86_64": _infra_native_report(),
                "aarch64": _infra_native_report(),
            },
            {"converged": False, "terminal_status": ""},
        ),
    ),
)
def test_phase1_decide_records_every_successful_outcome(
    tmp_path,
    monkeypatch,
    outcome,
    reports,
    expected,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    report_dir, args = _phase1_decide_args(tmp_path, reports)

    result = _run(*args)

    assert result.returncode == 0, f"{outcome}: {result.stderr}"
    recorded = json.loads(
        (report_dir / "round-decision-1.json").read_text()
    )
    assert recorded["round"] == 1
    assert {
        "converged": recorded["converged"],
        "terminal_status": recorded["terminal_status"],
    } == expected


def test_phase1_decide_records_an_error_before_failing(tmp_path):
    incomplete = _passed_native_report()
    incomplete["checks"] = {"native_build": True}
    report_dir, args = _phase1_decide_args(
        tmp_path,
        {
            "x86_64": incomplete,
            "aarch64": _passed_native_report(),
        },
    )

    result = _run(*args)

    assert result.returncode == 2
    recorded = json.loads((report_dir / "decision-error-1.json").read_text())
    assert recorded == {
        "error": "native report checks are incomplete: x86_64",
        "error_type": "NativeRepairError",
        "round": 1,
        "status": "error",
    }


def test_phase1_decide_keeps_each_round_evidence(tmp_path):
    reports = {
        "x86_64": _passed_native_report(),
        "aarch64": _passed_native_report(),
    }
    report_dir, round_one = _phase1_decide_args(
        tmp_path,
        reports,
        round_number=1,
    )
    _, round_two = _phase1_decide_args(
        tmp_path,
        reports,
        round_number=2,
    )

    assert _run(*round_one).returncode == 0
    assert _run(*round_two).returncode == 0
    assert sorted(path.name for path in report_dir.iterdir()) == [
        "round-decision-1.json",
        "round-decision-2.json",
    ]


def test_phase1_decide_redacts_secret_from_error_evidence(
    tmp_path,
    monkeypatch,
):
    from scripts.harness import flow

    secret = "deepseek-secret"
    reports = {
        "x86_64": _passed_native_report(),
        "aarch64": _passed_native_report(),
    }
    report_dir, cli_args = _phase1_decide_args(tmp_path, reports)
    args = flow._parser().parse_args(cli_args)
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    monkeypatch.setattr(
        flow,
        "decide_round",
        lambda **_: (_ for _ in ()).throw(
            flow.NativeRepairError(f"request rejected: {secret}")
        ),
    )

    with pytest.raises(flow.NativeRepairError):
        flow.cmd_phase1_decide(args)

    evidence = (report_dir / "decision-error-1.json").read_text()
    assert secret not in evidence
    assert "request rejected: REDACTED" in evidence


def test_phase1_decide_derives_native_evidence_roots_from_report_paths(
    tmp_path,
    monkeypatch,
):
    from scripts.harness import flow

    reports = {
        "x86_64": _infra_native_report(),
        "aarch64": _infra_native_report(),
    }
    report_dir, cli_args = _phase1_decide_args(tmp_path, reports)
    args = flow._parser().parse_args(cli_args)
    x86_root = tmp_path / "phase1-x86"
    arm_root = tmp_path / "phase1-arm"
    for architecture, root in (("x86_64", x86_root), ("aarch64", arm_root)):
        root.mkdir()
        (root / f"{architecture}.json").write_text(json.dumps(reports[architecture]))
        (root / "diagnostics").mkdir()
    args.x86_report = x86_root / "x86_64.json"
    args.arm_report = arm_root / "aarch64.json"
    captured = {}

    def decide(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            converged=False,
            round_number=1,
            repair_attempts=0,
            validated_patch_sha256="",
            terminal_status="",
        )

    monkeypatch.setattr(flow, "decide_round", decide)
    flow.cmd_phase1_decide(args)

    assert captured["evidence_roots"] == {
        "x86_64": (x86_root / "diagnostics").resolve(),
        "aarch64": (arm_root / "diagnostics").resolve(),
    }
    assert (report_dir / "round-decision-1.json").is_file()


def test_phase1_decide_preserves_error_when_evidence_write_fails(
    tmp_path,
    monkeypatch,
    capsys,
):
    from scripts.harness import flow

    reports = {
        "x86_64": _passed_native_report(),
        "aarch64": _passed_native_report(),
    }
    _, cli_args = _phase1_decide_args(tmp_path, reports)
    args = flow._parser().parse_args(cli_args)
    monkeypatch.setattr(
        flow,
        "decide_round",
        lambda **_: (_ for _ in ()).throw(
            flow.NativeRepairError("original decision failure")
        ),
    )
    monkeypatch.setattr(
        flow,
        "_write_json",
        lambda *_: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(
        flow.NativeRepairError,
        match="original decision failure",
    ):
        flow.cmd_phase1_decide(args)

    assert "disk full" in capsys.readouterr().err


def test_delivery_config_command_reports_zero_write_validate_only(tmp_path):
    output = tmp_path / "delivery.json"

    result = _run(
        "delivery-config",
        "--environment",
        "test",
        "--delivery-mode",
        "validate_only",
        "--target-repo",
        "openeuler/openeuler-docker-images",
        "--push-repo",
        "qq_42020325/openeuler-docker-images",
        "--target-branch",
        "master",
        "--output",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(output.read_text())
    assert summary["allows_branch_push"] is False
    assert summary["allows_pr_create"] is False
    assert summary["duplicate_pr_guard_enabled"] is False


def test_candidate_commands_create_then_verify_for_same_base(tmp_path):
    task_spec = tmp_path / "input-task.json"
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    _write_candidate_payload(candidate)
    task_spec.write_text(
        json.dumps(
            {
                "app": "kvrocks",
                "version": "2.16.0",
                "os_version": "24.03-lts-sp4",
                "domain": "Database",
                "source_url": "https://github.com/apache/kvrocks",
                "scenario": "new-image",
            }
        )
    )

    created = _run(
        "candidate-create",
        "--candidate-dir",
        str(candidate),
        "--task-spec",
        str(task_spec),
        "--base-sha",
        BASE_SHA,
        "--validated-run-id",
        "123456",
    )
    verified = _run(
        "candidate-verify",
        "--candidate-dir",
        str(candidate),
        "--expected-run-id",
        "123456",
    )

    assert created.returncode == 0, created.stderr
    assert verified.returncode == 0, verified.stderr
    assert "promotion_action" not in json.loads(verified.stdout)


def test_candidate_verify_does_not_require_a_current_base(tmp_path):
    task_spec = tmp_path / "input-task.json"
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    _write_candidate_payload(candidate)
    task_spec.write_text(
        json.dumps(
            {
                "app": "kvrocks",
                "version": "2.16.0",
                "os_version": "24.03-lts-sp4",
                "domain": "Database",
                "source_url": "https://github.com/apache/kvrocks",
            }
        )
    )
    created = _run(
        "candidate-create",
        "--candidate-dir",
        str(candidate),
        "--task-spec",
        str(task_spec),
        "--base-sha",
        BASE_SHA,
        "--validated-run-id",
        "123456",
    )

    verified = _run(
        "candidate-verify",
        "--candidate-dir",
        str(candidate),
        "--expected-run-id",
        "123456",
    )

    assert created.returncode == 0, created.stderr
    assert verified.returncode == 0, verified.stderr
    assert "promotion_action" not in json.loads(verified.stdout)


def test_target_workspace_commands_clone_create_and_replay_patch(tmp_path):
    upstream = _upstream(tmp_path)
    base_sha = _git(upstream, "rev-parse", "HEAD")
    generated = tmp_path / "generated"
    patch = tmp_path / "generation.patch"

    cloned = _run(
        "target-clone",
        "--source",
        str(upstream),
        "--destination",
        str(generated),
        "--branch",
        "master",
    )
    assert cloned.returncode == 0, cloned.stderr
    assert json.loads(cloned.stdout)["base_sha"] == base_sha

    (generated / "Database").mkdir()
    (generated / "Database" / "new-file").write_text("candidate\n")
    created = _run(
        "target-create-patch",
        "--workspace",
        str(generated),
        "--branch",
        "master",
        "--base-sha",
        base_sha,
        "--output",
        str(patch),
    )
    assert created.returncode == 0, created.stderr
    assert patch.stat().st_size > 0

    replay = tmp_path / "replay"
    exact_clone = _run(
        "target-clone",
        "--source",
        str(upstream),
        "--destination",
        str(replay),
        "--branch",
        "master",
        "--expected-sha",
        base_sha,
    )
    applied = _run(
        "target-apply-patch",
        "--workspace",
        str(replay),
        "--branch",
        "master",
        "--base-sha",
        base_sha,
        "--patch",
        str(patch),
    )

    assert exact_clone.returncode == 0, exact_clone.stderr
    assert applied.returncode == 0, applied.stderr
    assert (replay / "Database" / "new-file").read_text() == "candidate\n"


def test_target_workspace_command_replays_disjoint_recovered_patch(tmp_path):
    upstream = _upstream(tmp_path)
    validated_base = _git(upstream, "rev-parse", "HEAD")
    generated = tmp_path / "generated"
    patch = tmp_path / "generation.patch"
    _run(
        "target-clone",
        "--source",
        str(upstream),
        "--destination",
        str(generated),
        "--branch",
        "master",
    )
    (generated / "Database").mkdir()
    (generated / "Database" / "candidate").write_text("candidate\n")
    _run(
        "target-create-patch",
        "--workspace",
        str(generated),
        "--branch",
        "master",
        "--base-sha",
        validated_base,
        "--output",
        str(patch),
    )
    (upstream / "Security").mkdir()
    (upstream / "Security" / "scan.md").write_text("scan\n")
    _git(upstream, "add", "Security/scan.md")
    _git(upstream, "commit", "-m", "unrelated update")
    replay = tmp_path / "replay"
    cloned = _run(
        "target-clone",
        "--source",
        str(upstream),
        "--destination",
        str(replay),
        "--branch",
        "master",
    )
    current_base = json.loads(cloned.stdout)["base_sha"]
    evidence = tmp_path / "recovery.json"

    applied = _run(
        "target-apply-recovered-patch",
        "--workspace",
        str(replay),
        "--branch",
        "master",
        "--current-base-sha",
        current_base,
        "--validated-base-sha",
        validated_base,
        "--patch",
        str(patch),
        "--output",
        str(evidence),
    )

    assert applied.returncode == 0, applied.stderr
    payload = json.loads(evidence.read_text())
    assert payload["status"] == "passed"
    assert payload["upstream_changed_paths"] == ["Security/scan.md"]
    assert payload["candidate_changed_paths"] == ["Database/candidate"]


def test_pipeline_stage_commands_are_exposed():
    for command in (
        "fork-deliver",
        "issue-contract-test",
        "phase1-generate",
        "phase1-smoke-generate",
        "phase1-native-smoke",
        "phase1-native-repair",
        "phase1-native-validate",
        "phase1-infra-evidence",
        "phase1-decide",
    ):
        result = _run(command, "--help")
        assert result.returncode == 0, f"{command}: {result.stderr}"

    for script, command in (
        (GATE_CLI, "task-contract"),
        (ARTIFACT_CLI, "aggregate-native"),
    ):
        result = _run_script(script, command, "--help")
        assert result.returncode == 0, f"{command}: {result.stderr}"


def test_flow_is_the_only_phase_one_entry():
    for command in (
        "task-spec",
        "candidate-create",
        "candidate-verify",
        "target-clone",
        "target-create-patch",
        "target-apply-patch",
        "target-apply-recovered-patch",
        "fork-deliver",
        "issue-contract-test",
        "phase1-generate",
        "phase1-smoke-generate",
        "phase1-native-smoke",
        "phase1-native-repair",
        "phase1-native-validate",
        "phase1-infra-evidence",
        "phase1-decide",
    ):
        result = _run(command, "--help")
        assert result.returncode == 0, f"{command}: {result.stderr}"
    assert not (ROOT / "scripts" / "harness" / "phase1.py").exists()


def test_fork_delivery_reads_token_only_from_environment():
    result = _run("fork-deliver", "--help")

    assert result.returncode == 0, result.stderr
    assert "--token" not in result.stdout
    assert "GITCODE_TOKEN" in result.stdout
    assert "--delivery-run-id" in result.stdout
    assert "--delivery-run-attempt" in result.stdout


def test_issue_contract_test_is_explicit_and_reads_environment_token():
    result = _run("issue-contract-test", "--help")

    assert result.returncode == 0, result.stderr
    assert "--token" not in result.stdout
    assert "GITCODE_TOKEN" in result.stdout
    assert "create, update, comment and close" in result.stdout


def test_shared_agent_cli_reports_contract_errors_without_traceback(tmp_path):
    task = tmp_path / "task.json"
    task.write_text("{}")
    env = os.environ.copy()
    env.pop("DEEPSEEK_API_KEY", None)
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "phase1-generate",
            "--workspace",
            str(tmp_path),
            "--task-spec",
            str(task),
            "--base-sha",
            "1" * 40,
            "--report-dir",
            str(tmp_path / "reports"),
            "--opencode",
            str(tmp_path / "opencode"),
            "--hadolint",
            str(tmp_path / "hadolint"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "DEEPSEEK_API_KEY is required" in result.stderr
    assert "Traceback" not in result.stderr


def test_phase1_generate_wires_hadolint_into_pipeline(
    tmp_path,
    monkeypatch,
):
    from scripts.harness import flow

    calls = {}
    task = object()
    dockerfile = tmp_path / "Dockerfile"
    hadolint = tmp_path / "hadolint"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setattr(flow, "_load_task", lambda _: task)

    def fake_lint_dockerfile(**kwargs):
        calls["lint"] = kwargs
        return {"status": "passed"}

    def fake_pipeline(**kwargs):
        calls["pipeline"] = kwargs
        kwargs["image_linter"](dockerfile)
        return SimpleNamespace(
            status="passed",
            qa_fix_rounds=0,
            gate_report={"status": "passed"},
        )

    monkeypatch.setattr(flow, "lint_dockerfile", fake_lint_dockerfile)
    monkeypatch.setattr(flow, "run_generation_pipeline", fake_pipeline)

    flow.cmd_phase1_generate(
        SimpleNamespace(
            workspace=tmp_path / "target",
            report_dir=tmp_path / "reports",
            task_spec=tmp_path / "task.json",
            base_sha="1" * 40,
            opencode=tmp_path / "opencode",
            hadolint=hadolint,
        )
    )

    assert calls["pipeline"]["task"] is task
    assert "goss_executable" not in calls["pipeline"]
    assert calls["pipeline"]["evidence_resolver"].__name__ == (
        "freeze_creator_evidence"
    )
    assert calls["lint"] == {
        "executable": hadolint,
        "dockerfile": dockerfile,
    }
