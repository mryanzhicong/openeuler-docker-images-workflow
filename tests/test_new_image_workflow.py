import os
import re
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "create_new_images.yml"
WATCH_PATH = ROOT / ".github" / "workflows" / "monitor_new_image_issues.yml"
ROUND_PATH = ROOT / ".github" / "workflows" / "_create_new_image_rounds.yml"
ROUNDS = ("round-1", "round-2", "round-3", "round-4")
ARCHITECTURE_JOBS = ("x86_64", "aarch64")
ACTIONLINT_CONFIG = ROOT / ".github" / "actionlint.yaml"
ACTIONS_DIR = ROOT / ".github" / "actions"
PHASE1_ACTIONS = (
    "phase1-setup",
    "phase1-replay",
    "phase1-emit-patch",
    "phase1-existing-as-new-context",
)


def _workflow(path=None):
    data = yaml.safe_load((path or WORKFLOW_PATH).read_text())
    assert isinstance(data, dict)
    return data


def _action_path(name):
    return ACTIONS_DIR / name / "action.yml"


def _action(name):
    data = yaml.safe_load(_action_path(name).read_text())
    assert isinstance(data, dict)
    return data


def _action_text(name):
    return _action_path(name).read_text()


def _trigger(data):
    return data.get("on", data.get(True))


def _job_text(job):
    return yaml.safe_dump(job, sort_keys=True)


def test_phase1_is_manual_only_with_explicit_operations():
    trigger = _trigger(_workflow())

    assert set(trigger) == {"workflow_dispatch"}
    operation = trigger["workflow_dispatch"]["inputs"]["operation"]
    assert operation["type"] == "choice"
    assert operation["default"] == "pipeline_smoke"
    assert operation["options"] == [
        "pipeline_smoke",
        "validate_only",
        "existing_as_new_probe",
        "scenario_one",
        "resume_round",
        "resume_package",
        "recover_package",
        "fork_pr",
        "fork_pr_resume",
        "failure_issue_contract_test",
    ]
    assert "validated_run_id" in trigger["workflow_dispatch"]["inputs"]
    assert "source_run_id" in trigger["workflow_dispatch"]["inputs"]
    assert "generation_run_id" in trigger["workflow_dispatch"]["inputs"]
    assert "resume_from_round" in trigger["workflow_dispatch"]["inputs"]
    assert len(trigger["workflow_dispatch"]["inputs"]) <= 10


def test_prepare_job_leaves_time_for_the_bounded_adversarial_path():
    assert _workflow()["jobs"]["prepare"]["timeout-minutes"] == 360


def test_existing_as_new_probe_hides_reference_and_delivers_alias_pr():
    jobs = _workflow()["jobs"]
    prepare = jobs["prepare"]
    steps = prepare["steps"]
    by_name = {step["name"]: step for step in steps}

    assert "existing_as_new_probe" in prepare["if"]
    normalize = by_name["Normalize TaskSpec"]
    assert "existing_as_new_probe" in normalize["run"]
    assert '${APP}-e2e-test' in normalize["run"]

    hide = by_name["Hide existing app for probe"]
    restore = by_name["Restore existing app after probe"]
    generate = by_name["Generate candidate via agent"]
    assert "existing_as_new_probe" in hide["if"]
    assert "update-index --skip-worktree" in hide["run"]
    assert "phase1-hidden-reference" in hide["run"]
    assert "existing_as_new_probe" in generate["if"]
    assert "always()" in restore["if"]
    assert "existing_as_new_probe" in restore["if"]
    assert "update-index" in restore["run"]
    assert "--no-skip-worktree" in restore["run"]
    assert "phase1-hidden-reference" in restore["run"]

    names = [step["name"] for step in steps]
    assert names.index(hide["name"]) < names.index(generate["name"])
    assert names.index(generate["name"]) < names.index(restore["name"])
    assert names.index(restore["name"]) < names.index(
        "Create generation patch"
    )

    delivery = jobs["deliver-fork-pr"]
    delivery_text = _job_text(delivery)
    assert "inputs.operation == 'existing_as_new_probe'" in delivery["if"]
    assert "needs.package-candidate.result == 'success'" in delivery["if"]
    assert "inputs.operation == 'existing_as_new_probe'" in delivery_text
    assert "github.run_id" in delivery_text
    assert "phase1-candidate-" in delivery_text

    package_text = _job_text(jobs["package-candidate"])
    assert "Existing-as-new test passed" in package_text
    assert "must not be merged" in package_text


def test_existing_as_new_context_reaches_generation_and_every_fixer():
    jobs = _workflow()["jobs"]
    prepare_steps = jobs["prepare"]["steps"]
    prepare_by_name = {step["name"]: step for step in prepare_steps}
    context = prepare_by_name["Apply existing-as-new context"]

    assert context["uses"] == (
        "./.github/actions/phase1-existing-as-new-context"
    )
    assert "existing_as_new_probe" in context["if"]
    assert context["with"]["canonical-app"] == "${{ inputs.app }}"
    assert context["with"]["target-alias"] == "${{ inputs.app }}-e2e-test"
    assert prepare_steps.index(context) < prepare_steps.index(
        prepare_by_name["Generate candidate via agent"]
    )

    for name in ROUNDS:
        call = jobs[name]["with"]
        assert "existing_as_new_probe" in call["canonical_app"]
        assert "inputs.app" in call["canonical_app"]
        assert "existing_as_new_probe" in call["target_alias"]
        assert "inputs.app" in call["target_alias"]
        assert "e2e-test" in call["target_alias"]

    round_workflow = _workflow(ROUND_PATH)
    call_inputs = _trigger(round_workflow)["workflow_call"]["inputs"]
    assert call_inputs["canonical_app"]["required"] is False
    assert call_inputs["target_alias"]["required"] is False
    decide_steps = round_workflow["jobs"]["decide"]["steps"]
    decide_by_name = {step["name"]: step for step in decide_steps}
    fixer_context = decide_by_name["Apply existing-as-new context"]
    assert fixer_context["uses"] == context["uses"]
    assert "inputs.canonical_app != ''" in fixer_context["if"]
    assert fixer_context["with"]["canonical-app"] == (
        "${{ inputs.canonical_app }}"
    )
    assert fixer_context["with"]["target-alias"] == (
        "${{ inputs.target_alias }}"
    )
    assert decide_steps.index(fixer_context) < decide_steps.index(
        decide_by_name["Converge or repair"]
    )


def test_existing_as_new_context_action_updates_every_agent_prompt(tmp_path):
    action = _action("phase1-existing-as-new-context")
    step = action["runs"]["steps"][0]
    prompt_dir = tmp_path / ".github" / "agents"
    prompt_dir.mkdir(parents=True)
    prompts = (
        "image-creator.md",
        "testcase-creator.md",
        "testcase-qa.md",
        "code-fixer.md",
    )
    for prompt in prompts:
        (prompt_dir / prompt).write_text(f"original {prompt}\n")

    completed = subprocess.run(
        ["bash", "-c", step["run"]],
        env={
            **os.environ,
            "GITHUB_WORKSPACE": str(tmp_path),
            "CANONICAL_APP": "kylin",
            "TARGET_ALIAS": "kylin-e2e-test",
        },
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    for prompt in prompts:
        text = (prompt_dir / prompt).read_text()
        assert f"original {prompt}" in text
        assert "The canonical application is `kylin`." in text
        assert "`kylin-e2e-test` is only a temporary" in text
        assert "Do not create a probe-only, client-only" in text
        assert "Tests must start and validate the candidate image itself" in text


def test_existing_as_new_context_does_not_require_image_qa_prompt():
    action = _action("phase1-existing-as-new-context")
    step = action["runs"]["steps"][0]

    assert "image-qa.md" not in step["run"]


def test_existing_as_new_probe_shell_restores_reference_without_diff(tmp_path):
    steps = {
        step["name"]: step
        for step in _workflow()["jobs"]["prepare"]["steps"]
    }
    workspace = tmp_path / "phase1-target"
    reference = workspace / "Bigdata" / "kylin"
    reference.mkdir(parents=True)
    (reference / "Dockerfile").write_text("existing\n")
    (workspace / "Bigdata" / "image-list.yml").write_text(
        "images:\n  kylin: kylin\n"
    )
    subprocess.run(
        ["git", "init", "-b", "master", str(workspace)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.name", "Fixture"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "config",
            "user.email",
            "fixture@example.com",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "add", "."],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    (tmp_path / "phase1-prepare").mkdir()
    env = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "SOURCE_APP": "kylin",
        "DOMAIN": "Bigdata",
    }

    hidden = subprocess.run(
        ["bash", "-c", steps["Hide existing app for probe"]["run"]],
        env=env,
        capture_output=True,
        text=True,
    )
    assert hidden.returncode == 0, hidden.stderr
    assert not reference.exists()
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "add",
            "--intent-to-add",
            "--",
            ".",
        ],
        check=True,
        capture_output=True,
    )
    assert subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""

    alias = workspace / "Bigdata" / "kylin-e2e-test"
    alias.mkdir()
    (alias / "Dockerfile").write_text("generated\n")
    restored = subprocess.run(
        [
            "bash",
            "-c",
            steps["Restore existing app after probe"]["run"],
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert restored.returncode == 0, restored.stderr
    assert (reference / "Dockerfile").read_text() == "existing\n"
    status = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Bigdata/kylin-e2e-test/" in status
    assert "Bigdata/kylin/" not in status



def test_scenario_one_runs_full_validation_chain_and_delivers_same_run():
    jobs = _workflow()["jobs"]

    assert "scenario_one" in jobs["prepare"]["if"]
    # A converged output is not enough: the combined round must have completed
    # successfully, including publishing its decision evidence.
    package_condition = jobs["package-candidate"]["if"]
    for name in ROUNDS:
        assert f"needs.{name}.result == 'success'" in package_condition
        assert f"needs.{name}.outputs.converged == 'true'" in package_condition

    delivery = jobs["deliver-fork-pr"]
    delivery_text = _job_text(delivery)
    assert delivery["needs"] == ["package-candidate"]
    assert "always()" in delivery["if"]
    assert "inputs.operation == 'scenario_one'" in delivery["if"]
    assert "needs.package-candidate.result == 'success'" in delivery["if"]
    assert "needs.release-x86-builders.result" not in delivery["if"]
    assert "needs.release-arm-builders.result" not in delivery["if"]
    assert "inputs.operation == 'fork_pr'" in delivery["if"]
    assert "github.run_id" in delivery_text

    prepare_steps = {step["name"]: step for step in jobs["prepare"]["steps"]}
    assert (
        "scenario_one"
        in prepare_steps["Generate candidate via agent"]["if"]
    )
    # Rounds are operation-agnostic: they validate whatever prepare staged.
    assert jobs["round-1"]["with"]["operation"] == "${{ inputs.operation }}"


def test_validation_rounds_group_build_test_and_fix_jobs():
    jobs = _workflow()["jobs"]

    for index, job_id in enumerate(ROUNDS, start=1):
        assert jobs[job_id]["name"] == f"Round {index}: validate and fix"

    round_workflow = _workflow(ROUND_PATH)
    assert round_workflow["name"] == "Phase 1 - Build, test and fix candidate"
    assert set(round_workflow["jobs"]) == {"x86_64", "aarch64", "decide"}
    assert round_workflow["jobs"]["x86_64"]["name"] == "Validate (x86_64)"
    assert round_workflow["jobs"]["aarch64"]["name"] == "Validate (aarch64)"
    decide = round_workflow["jobs"]["decide"]
    assert decide["name"] == (
        "Decide round outcome"
    )
    assert decide["needs"] == ["x86_64", "aarch64"]
    assert "always()" in decide["if"]


def test_issue_trigger_reuses_scenario_one_and_finalizes_the_source_issue():
    data = _workflow()
    inputs = _trigger(data)["workflow_dispatch"]["inputs"]
    jobs = data["jobs"]

    assert "Issue number" in inputs["source_run_id"]["description"]

    delivery = jobs["deliver-fork-pr"]
    assert delivery["outputs"]["pr_url"] == (
        "${{ steps.delivery.outputs.pr_url }}"
    )

    finalizer = jobs["finalize-trigger-issue"]
    assert "always()" in finalizer["if"]
    assert "scenario_one" in finalizer["if"]
    assert "startsWith(inputs.source_run_id, 'issue:')" in finalizer["if"]
    assert "deliver-fork-pr" in finalizer["needs"]
    text = _job_text(finalizer)
    assert "issue-finalize" in text
    assert '${SOURCE_ISSUE#issue:}' in text
    assert "needs.deliver-fork-pr.outputs.pr_url" in text
    assert "GITCODE_TOKEN" in text
    for name in ROUNDS:
        assert f"needs.{name}.result == 'success'" in text
        assert f"needs.{name}.outputs.terminal_status" in text
    assert "needs-human-review" in text
    assert "phase1-needs-human-${{ github.run_id }}" in text
    assert "--failure-evidence-dir" in text
    delivery_step = next(
        step
        for step in delivery["steps"]
        if step["name"] == "Promote candidate and create fork PR"
    )
    assert delivery_step["env"]["SOURCE_RUN_ID"] == (
        "${{ inputs.source_run_id }}"
    )
    assert "--source-issue-number" in delivery_step["run"]
    assert 'case "${SOURCE_RUN_ID}"' in delivery_step["run"]
    assert "case \"${{ inputs.source_run_id }}\"" not in delivery_step["run"]


def test_repair_budget_terminal_is_preserved_without_emitting_round_five():
    workflow = _workflow(ROUND_PATH)
    call = _trigger(workflow)["workflow_call"]
    job = workflow["jobs"]["decide"]
    steps = {step["name"]: step for step in job["steps"]}

    assert "terminal_status" in call["outputs"]
    assert job["outputs"]["terminal_status"] == (
        "${{ steps.judge.outputs.terminal_status }}"
    )
    emit_condition = steps["Emit next round candidate"]["if"]
    assert "terminal_status == ''" in emit_condition
    terminal_emit = steps["Emit terminal candidate"]
    assert "terminal_status != ''" in terminal_emit["if"]
    assert terminal_emit["with"]["output-dir"].endswith(
        "/phase1-terminal-candidate"
    )
    generation = steps["Download terminal generation evidence"]
    assert "terminal_status != ''" in generation["if"]
    assert generation["with"]["name"] == (
        "phase1-generation-${{ github.run_id }}"
    )
    for round_number in (1, 2, 3):
        previous = steps[
            f"Download round {round_number} decision evidence"
        ]
        assert "terminal_status != ''" in previous["if"]
        assert f"inputs.round > {round_number}" in previous["if"]
        assert previous["with"]["name"] == (
            f"phase1-decide{round_number}-${{{{ github.run_id }}}}"
        )
        assert previous["with"]["path"].endswith(
            f"/phase1-terminal/decisions/round{round_number}"
        )
    assert "phase1-terminal-candidate/changes.patch" in steps[
        "Collect terminal evidence"
    ]["run"]
    assert "phase1-terminal-generation/generation-reports" in steps[
        "Collect terminal evidence"
    ]["run"]
    terminal_upload = steps["Upload needs-human-review evidence"]
    assert "needs-human-review" in terminal_upload["if"]
    assert terminal_upload["with"]["name"] == (
        "phase1-needs-human-${{ github.run_id }}"
    )
    hard_stop_upload = steps["Upload hard-stop evidence"]
    assert "hard-stop" in hard_stop_upload["if"]
    assert hard_stop_upload["with"]["name"] == (
        "phase1-hard-stop-${{ github.run_id }}"
    )
    names = list(steps)
    assert names.index("Fail hard-stop decision") > names.index(
        "Upload hard-stop evidence"
    )
    hard_stop_failure = steps["Fail hard-stop decision"]
    assert "hard-stop" in hard_stop_failure["if"]
    assert "exit 1" in hard_stop_failure["run"]


def test_round_decision_artifact_is_strict_and_merge_safe():
    workflow = _workflow(ROUND_PATH)
    steps = {
        step["name"]: step
        for step in workflow["jobs"]["decide"]["steps"]
    }

    upload = steps["Upload round decision evidence"]
    assert "always()" in upload["if"]
    assert upload["with"]["if-no-files-found"] == "error"

    package_steps = {
        step["name"]: step
        for step in _workflow()["jobs"]["package-candidate"]["steps"]
    }
    download = package_steps["Download round decision evidence"]
    assert download["with"]["pattern"].lstrip().startswith("phase1-decide*")
    assert download["with"]["merge-multiple"] is True


def test_issue_watcher_polls_on_schedule_and_scans_up_to_max_issues():
    """The watcher fires on its own and claims a bounded scan of new Issues.

    `83173d8` commented the cron out so the watcher could not fire at real
    GitCode Issues during the testing phase; the P0 autopilot re-enables it.
    The schedule path ignores the Issue allowlist (no --issue-number), while a
    manual dispatch keeps the allowlist fail-fast so testers cannot reach real
    Issues by accident.
    """
    data = _workflow(WATCH_PATH)
    trigger = _trigger(data)

    assert set(trigger) == {"schedule", "workflow_dispatch"}
    assert trigger["schedule"][0]["cron"] == "*/5 * * * *"
    assert '#   - cron: "*/5 * * * *"' not in WATCH_PATH.read_text()
    assert data["permissions"] == {
        "actions": "write",
        "contents": "read",
    }
    assert data["concurrency"]["cancel-in-progress"] is False
    assert len(data["jobs"]) == 1

    job = data["jobs"]["watch"]
    text = _job_text(job)
    assert job["runs-on"] == "ubuntu-latest"
    assert "if" not in job
    assert "github.event_name == 'workflow_dispatch'" in text
    assert "PHASE1_TEST_ISSUE_NUMBER" in text
    assert "issue-watch" in text
    assert "--max-issues" in text
    assert "MAX_ISSUES" in text
    assert '${GITHUB_EVENT_NAME}' in text
    assert "GITCODE_TOKEN" in text
    assert "github.token" in text
    assert "DEEPSEEK_API_KEY" not in text
    assert "self-hosted" not in text


def test_phase1_task_defaults_are_the_confirmed_kvrocks_contract():
    inputs = _trigger(_workflow())["workflow_dispatch"]["inputs"]

    assert inputs["app"]["default"] == "kvrocks"
    assert inputs["version"]["default"] == "2.16.0"
    assert inputs["os_version"]["default"] == "24.03-lts-sp4"
    assert inputs["domain"]["default"] == "Database"
    assert inputs["source_url"]["default"] == (
        "https://github.com/apache/kvrocks/tree/v2.16.0"
    )



def test_only_native_validation_uses_self_hosted_runner_pools():
    jobs = _workflow()["jobs"]
    round_jobs = _workflow(ROUND_PATH)["jobs"]

    for name in (
        "prepare",
        "seed-resume",
        "package-candidate",
        "deliver-fork-pr",
        "issue-contract-test",
    ):
        assert jobs[name]["runs-on"] == "ubuntu-latest"
    # Each round pins both native runners; emulation would hide the very
    # architecture differences the round exists to find.
    assert round_jobs["x86_64"]["runs-on"] == [
        "self-hosted",
        "Linux",
        "X64",
        "oe-image-x86",
    ]
    assert round_jobs["aarch64"]["runs-on"] == [
        "self-hosted",
        "Linux",
        "ARM64",
        "oe-image-arm64",
    ]
    assert round_jobs["decide"]["runs-on"] == "ubuntu-latest"

    for path in (WORKFLOW_PATH, ROUND_PATH):
        text = path.read_text()
        assert "docker/setup-qemu-action" not in text
        assert "docker/setup-buildx-action" not in text



def test_every_round_validates_one_candidate_on_both_architectures():
    jobs = _workflow()["jobs"]
    round_workflow = _workflow(ROUND_PATH)

    assert jobs["round-1"]["needs"] == ["prepare", "seed-resume"]
    for index, name in enumerate(ROUNDS):
        round_call = jobs[name]
        assert round_call["uses"] == "./.github/workflows/_create_new_image_rounds.yml"
        assert round_call["with"]["round"] == str(index + 1)
        # pipeline_smoke converges deterministically; giving it a repair
        # budget would burn model calls the Fixer cannot possibly help with,
        # because the smoke image is built from a synthetic context.
        assert round_call["with"]["max_rounds"] == (
            "${{ inputs.operation == 'pipeline_smoke' && '0' || '3' }}"
        )
        # GitHub expressions have no arithmetic, so the next round is explicit.
        assert round_call["with"]["next_round"] == str(index + 2)

    decide = round_workflow["jobs"]["decide"]
    assert decide["needs"] == ["x86_64", "aarch64"]
    assert "always()" in decide["if"]
    call_outputs = _trigger(round_workflow)["workflow_call"]["outputs"]
    assert call_outputs["converged"]["value"] == (
        "${{ jobs.decide.outputs.converged }}"
    )
    assert call_outputs["terminal_status"]["value"] == (
        "${{ jobs.decide.outputs.terminal_status }}"
    )

    # A later round exists only because the previous one did not converge.
    for index, name in enumerate(ROUNDS[1:]):
        assert jobs[name]["needs"] == [ROUNDS[index], "seed-resume"]
        condition = jobs[name]["if"]
        assert f"needs.{ROUNDS[index]}.result == 'success'" in condition
        assert (
            f"needs.{ROUNDS[index]}.outputs.converged != 'true'"
            in condition
        )
        assert (
            f"needs.{ROUNDS[index]}.outputs.terminal_status == ''"
            in condition
        )

    workflow_text = WORKFLOW_PATH.read_text()
    assert "scripts/harness/run.py" not in workflow_text
    # The serial chain and its extra revalidation job are gone.
    assert "revalidate" not in workflow_text
    assert "phase1-native-repair" not in workflow_text



def test_resume_restarts_one_round_from_another_run_of_the_same_pipeline():
    jobs = _workflow()["jobs"]
    seed = jobs["seed-resume"]
    seed_text = _job_text(seed)

    assert seed["if"] == "${{ inputs.operation == 'resume_round' }}"
    assert "phase1-patch${{ inputs.resume_from_round }}-${{" in seed_text
    assert "run-id: ${{ inputs.source_run_id }}" in seed_text
    assert "github-token: ${{ github.token }}" in seed_text
    # Republished under this run so every round reads one uniform name.
    assert "phase1-patch${{ inputs.resume_from_round }}-${{ github.run_id }}" in (
        seed_text
    )
    # Packaging still needs the original generation reports. Resume must
    # republish them under this run just like it republishes the round patch.
    assert "phase1-generation-${{ inputs.source_run_id }}" in seed_text
    assert "phase1-generation-${{ github.run_id }}" in seed_text

    for index, name in enumerate(ROUNDS):
        condition = jobs[name]["if"]
        assert "needs.seed-resume.result == 'success'" in condition
        assert f"inputs.resume_from_round == '{index + 1}'" in condition

    package = jobs["package-candidate"]
    package_text = _job_text(package)
    immutable_upload = next(
        step
        for step in package["steps"]
        if step.get("name") == "Upload immutable candidate"
    )
    diagnostic_upload = next(
        step
        for step in package["steps"]
        if step.get("name") == "Upload resumed diagnostic candidate"
    )
    assert "inputs.operation != 'resume_round'" in immutable_upload["if"]
    assert "inputs.operation != 'resume_package'" in immutable_upload["if"]
    assert "resume_round" in diagnostic_upload["if"]
    assert "resume_package" in diagnostic_upload["if"]
    assert "phase1-resume-candidate-" in _job_text(diagnostic_upload)
    assert "resume-provenance.json" in package_text
    assert '"promotable": false' in WORKFLOW_PATH.read_text()
    assert '"mode":"%s"' in WORKFLOW_PATH.read_text()


def test_resume_republishes_decisions_before_the_restarted_round():
    steps = {
        step["name"]: step
        for step in _workflow()["jobs"]["seed-resume"]["steps"]
    }
    cases = (
        (1, "inputs.resume_from_round != '1'"),
        (
            2,
            "inputs.resume_from_round == '3' || inputs.resume_from_round == '4'",
        ),
        (3, "inputs.resume_from_round == '4'"),
    )

    for round_number, condition in cases:
        download = steps[
            f"Download round {round_number} decision evidence"
        ]
        marker = steps[
            f"Record missing round {round_number} decision"
        ]
        upload = steps[
            f"Republish round {round_number} decision evidence"
        ]
        assert condition in download["if"]
        assert download["id"] == f"source_decision{round_number}"
        assert download["continue-on-error"] is True
        assert marker["if"] == download["if"]
        assert f"steps.source_decision{round_number}.outcome" in marker["run"]
        assert f"missing-source-decision-round{round_number}.json" in marker["run"]
        assert "inputs.source_run_id" in marker["run"]
        assert upload["if"] == download["if"]
        assert download["with"]["name"] == (
            f"phase1-decide{round_number}-${{{{ inputs.source_run_id }}}}"
        )
        assert download["with"]["run-id"] == "${{ inputs.source_run_id }}"
        assert upload["with"]["name"] == (
            f"phase1-decide{round_number}-${{{{ github.run_id }}}}"
        )


def test_artifact_producers_cover_each_package_input_mode():
    jobs = _workflow()["jobs"]
    prepare_text = _job_text(jobs["prepare"])
    seed_text = _job_text(jobs["seed-resume"])
    package_text = _job_text(jobs["package-candidate"])

    # Fresh runs produce both inputs in this run.
    assert "phase1-generation-${{ github.run_id }}" in prepare_text
    assert "phase1-patch1-${{ github.run_id }}" in prepare_text
    # Round resume republishes both source inputs under the current run, so
    # all downstream consumers keep one stable artifact naming contract.
    assert "phase1-generation-${{ inputs.source_run_id }}" in seed_text
    assert "phase1-generation-${{ github.run_id }}" in seed_text
    assert "phase1-patch${{ inputs.resume_from_round }}-${{" in seed_text
    assert "phase1-patch${{ inputs.resume_from_round }}-${{ github.run_id }}" in (
        seed_text
    )
    # Package resume intentionally consumes the source run directly.
    assert "phase1-converged-${{ inputs.operation == 'resume_package' &&" in (
        package_text
    )
    assert "inputs.source_run_id || github.run_id" in package_text


def test_round_artifact_producers_and_consumers_use_the_same_templates():
    """Cross-check reusable workflow boundaries instead of isolated strings."""

    def artifact_names(path, action):
        steps = [
            step
            for job in _workflow(path)["jobs"].values()
            for step in job.get("steps", [])
        ]
        return {
            step["with"]["name"]
            for step in steps
            if action in step.get("uses", "")
            and isinstance(step.get("with", {}).get("name"), str)
        }

    round_uploads = artifact_names(ROUND_PATH, "upload-artifact")
    round_downloads = artifact_names(ROUND_PATH, "download-artifact")
    main_uploads = artifact_names(WORKFLOW_PATH, "upload-artifact")

    assert {
        "phase1-round${{ inputs.round }}-x86_64-${{ github.run_id }}",
        "phase1-round${{ inputs.round }}-aarch64-${{ github.run_id }}",
    }.issubset(round_uploads & round_downloads)
    current_patch = "phase1-patch${{ inputs.round }}-${{ github.run_id }}"
    assert current_patch in round_downloads
    assert (
        "phase1-patch${{ inputs.next_round }}-${{ github.run_id }}"
        in round_uploads
    )

    jobs = _workflow()["jobs"]
    prepare = _job_text(jobs["prepare"])
    seed = _job_text(jobs["seed-resume"])
    package = _job_text(jobs["package-candidate"])
    delivery = _job_text(jobs["deliver-fork-pr"])
    assert "phase1-patch1-${{ github.run_id }}" in prepare
    assert "phase1-patch${{ inputs.resume_from_round }}-${{" in seed
    assert "phase1-converged-${{" in package
    assert "phase1-converged-${{ github.run_id }}" in round_uploads
    assert "phase1-candidate-${{" in delivery
    assert (
        "phase1-${{ inputs.operation == 'pipeline_smoke' && 'smoke-' || '' }}"
        "candidate-${{ github.run_id }}"
        in main_uploads
    )
    # Terminal evidence is for human diagnosis and must never be promoted.
    assert "phase1-needs-human-" not in package
    assert "phase1-needs-human-" not in delivery


def test_candidate_verify_workflow_does_not_claim_to_check_current_base():
    package = _workflow()["jobs"]["package-candidate"]
    seal = next(
        step
        for step in package["steps"]
        if step.get("name") == "Seal immutable candidate bundle"
    )

    assert "candidate-verify" in seal["run"]
    assert "--current-base-sha" not in seal["run"]
    # The similarly named argument remains meaningful for recovered patches.
    assert "--current-base-sha" in WORKFLOW_PATH.read_text()


def test_sealed_candidate_preserves_both_junit_reports():
    seal = next(
        step
        for step in _workflow()["jobs"]["package-candidate"]["steps"]
        if step.get("name") == "Seal immutable candidate bundle"
    )

    assert "native-reports/x86_64.junit.xml" in seal["run"]
    assert "candidate/reports/x86_64.junit.xml" in seal["run"]
    assert "native-reports/aarch64.junit.xml" in seal["run"]
    assert "candidate/reports/aarch64.junit.xml" in seal["run"]


def test_recover_package_combines_generation_and_validation_runs():
    jobs = _workflow()["jobs"]
    package = jobs["package-candidate"]
    text = _job_text(package)

    assert "recover_package" in package["if"]
    assert "inputs.generation_run_id" in text
    assert "inputs.source_run_id" in text
    assert "target-apply-recovered-patch" in text
    assert "recovery-provenance.json" in text
    assert text.count("cmp ") == 4
    assert "generation/task-spec.json" in text
    assert "generation/base-sha.txt" in text
    assert "promotable: true" in WORKFLOW_PATH.read_text()
    assert "phase1-candidate-" in text
    assert "phase1-resume-candidate-" in text
    assert "DEEPSEEK_API_KEY" not in text
    assert "phase1-native-repair" not in text
    assert "phase1-native-validate" not in text


def test_recover_package_only_accepts_confirmed_full_validation_lineage():
    package = _job_text(_workflow()["jobs"]["package-candidate"])
    workflow_text = WORKFLOW_PATH.read_text()

    assert "30478803960" in package
    assert "30483501656" in package
    for digest in (
        "3b9579c664c1a699121156d48384d43647a8ac6671490469d1189d788308a56f",
        "65682e11649b4e992ee000872317f2b4d04786e1c56a233549dd2c8b2222fc37",
        "afad06c7206854c432cba89c1a19ffd9836a79281763cd1736f01f3e0aae729d",
        "8b046be0392a36dccb28b1d14f2c504c5b95fb6e3f518809731639d5e2c7ce86",
    ):
        assert digest in package
    assert package.count(".checks == {") >= 2
    for check in (
        "native_build",
        "dgoss",
        "shared_tests",
        "restart_persistence",
    ):
        assert workflow_text.count(f'"{check}": true') >= 2
    assert 'evidence_run_id="${{ inputs.source_run_id }}"' in workflow_text
    assert '--run-id "${evidence_run_id}"' in workflow_text
    assert '--expected-run-id "${evidence_run_id}"' in workflow_text



def test_validate_only_jobs_have_no_gitcode_credential_or_write_command():
    jobs = _workflow()["jobs"]

    for name in ("prepare", "seed-resume", "package-candidate"):
        text = _job_text(jobs[name])
        assert "GITCODE_TOKEN" not in text
        assert "fork-deliver" not in text
        assert "issue-contract-test" not in text
    for path in (ROUND_PATH,):
        text = path.read_text()
        assert "GITCODE_TOKEN" not in text
        assert "fork-deliver" not in text
        assert "issue-contract-test" not in text
    for name in PHASE1_ACTIONS:
        text = _action_text(name)
        assert "GITCODE_TOKEN" not in text
        assert "fork-deliver" not in text
        assert "issue-contract-test" not in text
    assert _workflow()["permissions"] == {
        "actions": "read",
        "contents": "read",
    }


def test_only_the_decision_stage_receives_a_model_key():
    jobs = _workflow()["jobs"]
    round_workflow = _workflow(ROUND_PATH)
    expected = (
        "${{ inputs.operation != 'pipeline_smoke' && "
        "secrets.DEEPSEEK_API_KEY || '' }}"
    )

    for name in ROUNDS:
        key = jobs[name]["secrets"]["DEEPSEEK_API_KEY"]
        # Empty strings are falsy in GitHub expressions, so the safe form must
        # test the non-smoke branch before selecting the secret.
        assert key == expected
    call = _trigger(round_workflow)["workflow_call"]
    assert call["secrets"]["DEEPSEEK_API_KEY"]["required"] is False
    for name in ARCHITECTURE_JOBS:
        assert "DEEPSEEK_API_KEY" not in _job_text(round_workflow["jobs"][name])
    assert "DEEPSEEK_API_KEY" in _job_text(round_workflow["jobs"]["decide"])
    assert "inherit" not in WORKFLOW_PATH.read_text()


def test_the_smoke_path_can_never_reach_the_fixer():
    jobs = _workflow()["jobs"]

    for name in ROUNDS:
        assert "pipeline_smoke" in jobs[name]["with"]["max_rounds"]
        assert "'0'" in jobs[name]["with"]["max_rounds"]
    # A zero budget makes a smoke regression fail before it ever builds a
    # prompt, so pipeline_smoke cannot go green via a business terminal state.
    round_call = _trigger(_workflow(ROUND_PATH))["workflow_call"]
    assert round_call["secrets"]["DEEPSEEK_API_KEY"]["required"] is False


def test_fork_pr_reuses_named_artifact_from_exact_validated_run():
    job = _workflow()["jobs"]["deliver-fork-pr"]
    text = _job_text(job)

    assert "inputs.operation == 'fork_pr'" in job["if"]
    assert "inputs.validated_run_id" in text
    assert "phase1-candidate-" in text
    assert "run-id" in text
    assert "fork-deliver" in text
    assert "--delivery-run-id" in text
    assert "github.run_id" in text
    assert "--delivery-run-attempt" in text
    assert "github.run_attempt" in text
    assert "GITCODE_TOKEN" in text
    assert "--token" not in text


def test_fork_pr_resume_reuses_named_resume_artifact_without_revalidation():
    workflow = _workflow()
    trigger = _trigger(workflow)
    operations = trigger["workflow_dispatch"]["inputs"]["operation"]["options"]
    job = workflow["jobs"]["deliver-fork-pr"]
    steps = {step["name"]: step for step in job["steps"]}

    assert "fork_pr_resume" in operations
    assert "inputs.operation == 'fork_pr_resume'" in job["if"]
    assert "fork_pr_resume" in steps["Validate validated run ID"]["if"]

    download = steps["Download resumed candidate"]
    artifact_name = download["with"]["name"]
    assert "inputs.operation == 'fork_pr_resume'" in download["if"]
    assert artifact_name == (
        "phase1-resume-candidate-${{ inputs.validated_run_id }}"
    )
    assert "inputs.validated_run_id" in download["with"]["run-id"]


def test_issue_probe_is_isolated_and_explicit():
    jobs = _workflow()["jobs"]
    issue = jobs["issue-contract-test"]

    assert "failure_issue_contract_test" in issue["if"]
    assert "issue-contract-test" in _job_text(issue)
    assert "GITCODE_TOKEN" in _job_text(issue)
    assert "GITCODE_TOKEN" in _job_text(jobs["deliver-fork-pr"])


def test_actions_are_commit_pinned_and_python_install_requires_hashes():
    uses = [
        step["uses"]
        for job in _workflow()["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]
    for name in PHASE1_ACTIONS:
        uses.extend(
            step["uses"]
            for step in _action(name)["runs"]["steps"]
            if "uses" in step
        )

    assert uses
    external = [use for use in uses if not use.startswith("./")]
    local = [use for use in uses if use.startswith("./")]
    assert external
    assert all(
        re.fullmatch(r"actions/[^@]+@[0-9a-f]{40}", use) for use in external
    )
    assert local
    assert all(
        (ROOT / use.removeprefix("./") / "action.yml").is_file()
        for use in local
    )

    setup = _action_text("phase1-setup")
    setup_action = _action("phase1-setup")
    assert setup_action["inputs"]["python-version"]["default"] == ""
    assert "--require-hashes" in setup
    assert ".github/python-phase1.lock.txt" in setup
    assert '${HOME}/.cache/oe-image-tools' in setup
    assert '${RUNNER_TEMP}/oe-image-tools' in setup
    assert (
        "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
        in setup
    )


def test_legacy_evidence_is_allowed_only_on_the_reviewed_recovery_path():
    workflow_text = WORKFLOW_PATH.read_text()
    aggregate = next(
        step
        for step in _workflow()["jobs"]["package-candidate"]["steps"]
        if step.get("name") == "Aggregate dual-architecture result evidence"
    )["run"]

    # Reports predating validated_patch_sha256 may only be accepted for the
    # one reviewed run whose report bytes are already pinned by SHA256.
    assert workflow_text.count("--allow-legacy-evidence") == 2
    assert 'legacy_evidence=""' in aggregate
    assert '"${{ inputs.operation }}" = "recover_package"' in aggregate
    assert "TODO(test-recovery-cleanup)" in aggregate
    assert "${legacy_evidence}" in aggregate



def test_each_native_job_cleans_before_and_after_without_release_jobs():
    jobs = _workflow()["jobs"]
    round_jobs = _workflow(ROUND_PATH)["jobs"]
    workflow_text = WORKFLOW_PATH.read_text()

    assert "release-x86-builders" not in jobs
    assert "release-arm-builders" not in jobs
    assert "phase1-native-release" not in workflow_text
    for name, architecture in (
        ("x86_64", "x86_64"),
        ("aarch64", "aarch64"),
    ):
        job = round_jobs[name]
        commands = "\n".join(
            str(step.get("run", "")) for step in job["steps"]
        )
        assert f"--architecture {architecture}" in commands
        assert "--job-temp" in commands
        assert "--phase before" in commands
        assert "--phase after" in commands
        assert "--runner-name" in commands
        assert "runner.name" in commands
        after = next(
            step for step in job["steps"]
            if step["name"].startswith("Clean native runner after")
        )
        assert "always()" in after["if"]


def test_test_duplicate_pr_skip_has_an_explicit_production_removal_marker():
    text = WORKFLOW_PATH.read_text()

    assert "TODO(production-duplicate-pr-guard)" in text
    assert "duplicate PR" in text


def test_actionlint_knows_the_confirmed_custom_runner_labels():
    config = yaml.safe_load(ACTIONLINT_CONFIG.read_text())

    assert config["self-hosted-runner"]["labels"] == [
        "oe-image-x86",
        "oe-image-arm64",
    ]



def test_jobs_and_run_have_readable_display_names():
    data = _workflow()

    assert "inputs.operation" in data["run-name"]
    assert "inputs.app" in data["run-name"]
    expected = {
        "prepare": "Prepare round 1 candidate",
        "seed-resume": "Seed resumed run artifacts",
        "round-1": "Round 1: validate and fix",
        "round-2": "Round 2: validate and fix",
        "round-3": "Round 3: validate and fix",
        "round-4": "Round 4: validate and fix",
        "package-candidate": "Seal and publish final candidate",
        "deliver-fork-pr": "Create fork PR from candidate",
        "finalize-trigger-issue": "Finalize trigger Issue",
        "issue-contract-test": "Test failure Issue lifecycle",
    }
    assert {
        job_id: job["name"] for job_id, job in data["jobs"].items()
    } == expected



def test_pipeline_smoke_reuses_candidate_chain_without_ai_or_gitcode_steps():
    jobs = _workflow()["jobs"]
    assert "pipeline_smoke" in jobs["prepare"]["if"]
    assert "converged == 'true'" in jobs["package-candidate"]["if"]

    prepare_steps = {step["name"]: step for step in jobs["prepare"]["steps"]}
    smoke_generate = prepare_steps["Create deterministic smoke candidate"]
    assert "pipeline_smoke" in smoke_generate["if"]
    assert "phase1-smoke-generate" in smoke_generate["run"]
    assert "DEEPSEEK_API_KEY" not in _job_text(smoke_generate)

    # The smoke candidate always passes, so round-1 converges and rounds 2-4
    # skip themselves; the same round jobs serve both paths.
    round_jobs = _workflow(ROUND_PATH)["jobs"]
    for name in ARCHITECTURE_JOBS:
        job = round_jobs[name]
        smoke_steps = [
            step
            for step in job["steps"]
            if step.get("name", "").startswith("Run native pipeline smoke")
        ]
        assert len(smoke_steps) == 1
        assert "phase1-native-smoke" in smoke_steps[0]["run"]
        assert "pipeline_smoke" in smoke_steps[0]["if"]

    package_text = _job_text(jobs["package-candidate"])
    assert "phase1-smoke-candidate-" in package_text
    assert "not promotable" in package_text


def test_prepare_uploads_reports_and_diagnostic_patch_after_generation_failure():
    prepare = _workflow()["jobs"]["prepare"]
    steps = {step["name"]: step for step in prepare["steps"]}

    diagnostic_patch = steps["Create failure diagnostic patch"]
    assert "failure()" in diagnostic_patch["if"]
    assert "steps.base.outputs.sha != ''" in diagnostic_patch["if"]
    assert diagnostic_patch["continue-on-error"] is True
    assert diagnostic_patch["uses"] == "./.github/actions/phase1-emit-patch"
    assert diagnostic_patch["with"]["tolerate-missing"] == "true"

    upload = steps["Upload generation artifact"]
    assert "always()" in upload["if"]
    assert "steps.base.outputs.sha != ''" in upload["if"]
    assert upload["with"]["path"] == "${{ runner.temp }}/phase1-prepare/"


def test_hadolint_is_advisory_without_a_project_rule_allowlist():
    jobs = _workflow()["jobs"]
    prepare_steps = {
        step["name"]: step for step in jobs["prepare"]["steps"]
    }
    package_steps = {
        step["name"]: step for step in jobs["package-candidate"]["steps"]
    }

    smoke_lint = prepare_steps["Lint generated Dockerfile"]
    final_gate = package_steps["Enforce final target gates and lint"]
    for step in (smoke_lint, final_gate):
        assert "--ignore" not in step["run"]
        assert "lint_status=0" in step["run"]
        assert "|| lint_status=$?" in step["run"]
        assert "Hadolint advisory exit status" in step["run"]

    assert "generation-reports/hadolint.txt" in smoke_lint["run"]
    assert "candidate/reports/hadolint.txt" in final_gate["run"]
    assert "scripts/harness/gate_diff.py" in final_gate["run"]
    assert "--phase final" in final_gate["run"]
    assert "--ignore DL" not in WORKFLOW_PATH.read_text()


def test_validate_only_lints_before_agent_qa_inside_generation():
    prepare_steps = {
        step["name"]: step
        for step in _workflow()["jobs"]["prepare"]["steps"]
    }

    generate = prepare_steps["Generate candidate via agent"]
    assert "--hadolint" in generate["run"]
    assert "steps.tools.outputs.hadolint_path" in generate["run"]

    standalone_lint = prepare_steps["Lint generated Dockerfile"]
    assert standalone_lint["if"] == "${{ inputs.operation == 'pipeline_smoke' }}"



def test_a_failed_round_still_publishes_the_evidence_the_decision_needs():
    round_jobs = _workflow(ROUND_PATH)["jobs"]

    for name, architecture in (("x86_64", "x86_64"), ("aarch64", "aarch64")):
        steps = {step.get("name"): step for step in round_jobs[name]["steps"]}
        validate = steps[f"Validate natively ({architecture})"]
        # The decision stage rules on the round, so a failed build must not
        # fail the job before its report is uploaded.
        assert validate["continue-on-error"] is True
        upload = steps[f"Upload round evidence ({architecture})"]
        assert "always()" in upload["if"]
        assert upload["with"]["name"] == (
            "phase1-round${{ inputs.round }}-"
            f"{architecture}" + "-${{ github.run_id }}"
        )
        assert upload["with"]["if-no-files-found"] == "error"


def test_round_replay_turns_only_exhausted_clone_disconnects_into_infra_evidence():
    round_jobs = _workflow(ROUND_PATH)["jobs"]

    for name, architecture in (("x86_64", "x86_64"), ("aarch64", "aarch64")):
        steps = {step.get("name"): step for step in round_jobs[name]["steps"]}
        replay = steps["Replay round candidate"]
        assert replay["id"] == "replay"
        assert replay["with"]["architecture"] == architecture
        assert replay["with"]["evidence-dir"] == (
            "${{ runner.temp }}/phase1-round"
        )

        validate = steps[f"Validate natively ({architecture})"]
        smoke = steps[f"Run native pipeline smoke ({architecture})"]
        assert "steps.replay.outputs.replayed == 'true'" in validate["if"]
        assert "steps.replay.outputs.replayed == 'true'" in smoke["if"]

    replay_action = _action("phase1-replay")
    assert replay_action["outputs"]["replayed"]["value"] == (
        "${{ steps.replay.outputs.replayed }}"
    )
    replay_body = replay_action["runs"]["steps"][0]["run"]
    assert "clone_status" in replay_body
    assert 'if [ "${clone_status}" -eq 75 ]' in replay_body
    assert "phase1-infra-evidence" in replay_body
    assert "exit \"${clone_status}\"" in replay_body


def test_summary_markdown_does_not_trigger_single_quote_shellcheck_warning():
    text = WORKFLOW_PATH.read_text()

    assert "printf -- '- Candidate artifact:" not in text
    assert "printf -- '- Promotion input" not in text


def test_shared_stage_steps_live_in_one_composite_action_each():
    workflow_text = WORKFLOW_PATH.read_text()

    for name in PHASE1_ACTIONS:
        assert _action(name)["runs"]["using"] == "composite"

    # Every shared step body exists exactly once, inside its action.
    for fragment in (
        "python3 -m venv",
        "scripts/bootstrap_tools.py",
        "scripts/runner_preflight.py",
        "target-apply-patch",
    ):
        assert fragment not in workflow_text

    setup = _action_text("phase1-setup")
    assert "python3 -m venv" in setup
    assert "scripts/bootstrap_tools.py" in setup
    assert "scripts/runner_preflight.py" in setup
    assert "target-apply-patch" in _action_text("phase1-replay")
    assert "target-create-patch" in _action_text("phase1-emit-patch")


def _delegated_setup_step(job):
    steps = job["steps"]
    checkout = next(
        index
        for index, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/checkout@")
    )
    # A local action is only resolvable after the repository is checked out,
    # so checkout must stay in the job and precede every local action. Cheap
    # input guards may run first so bad input fails before any provisioning.
    assert all(
        not step.get("uses", "").startswith("./") for step in steps[:checkout]
    )
    setup = next(
        step for step in steps[checkout + 1:]
        if step.get("uses") == "./.github/actions/phase1-setup"
    )
    assert setup["uses"] == "./.github/actions/phase1-setup"
    return setup



def test_control_and_native_jobs_delegate_their_setup_modes():
    jobs = _workflow()["jobs"]

    for name, arch, preflight in (
        ("prepare", "x86_64", False),
        ("package-candidate", "x86_64", False),
    ):
        setup = _delegated_setup_step(jobs[name])
        assert setup["id"] == "tools"
        assert setup["with"]["python-version"] == "3.11"
        assert setup["with"]["arch"] == arch
        assert setup["with"].get("preflight", "false") == str(preflight).lower()

    round_jobs = _workflow(ROUND_PATH)["jobs"]
    for name in ARCHITECTURE_JOBS:
        setup = _delegated_setup_step(round_jobs[name])
        assert "python-version" not in setup["with"]
        assert setup["with"]["preflight"] == "true"
        assert "tool-cache-root" not in setup["with"]
    decide_setup = _delegated_setup_step(round_jobs["decide"])
    assert decide_setup["with"]["python-version"] == "3.11"
    assert decide_setup["with"]["arch"] == "x86_64"

    for name in ("deliver-fork-pr", "issue-contract-test"):
        setup = _delegated_setup_step(jobs[name])
        assert setup["with"]["python-version"] == "3.11"



def test_replay_action_always_pins_the_exact_validated_base_sha():
    replay = _action_text("phase1-replay")

    assert "--expected-sha" in replay
    assert "--branch master" in replay
    # A plain clone would silently drift onto a newer target master.
    assert replay.count("target-clone") == 1

    replays = [
        step
        for path in (WORKFLOW_PATH, ROUND_PATH)
        for job in _workflow(path)["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses") == "./.github/actions/phase1-replay"
    ]
    assert len(replays) == 4
    assert all("input-dir" in step["with"] for step in replays)


def test_emit_patch_action_refuses_a_missing_base_sha_by_default():
    emit = _action(_action_path("phase1-emit-patch").parent.name)
    body = emit["runs"]["steps"][0]["run"]

    assert emit["inputs"]["tolerate-missing"]["default"] == "false"
    assert "exit 2" in body
    assert "exit 0" in body
    assert 'if [ "${TOLERATE_MISSING}" = "true" ]' in body
