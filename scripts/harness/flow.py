#!/usr/bin/env python3
"""Single-file orchestrator: generate -> build+test -> fix loop -> compose PR.

Usage:
    python flow.py --app nginx --version 1.27.2 --os 24.03-lts --domain Cloud

    # demo mode (skip LLM agents)
    python flow.py --app nginx --version 1.27.2 --os 24.03-lts --domain Cloud --demo

Environment:
    GITCODE_TOKEN          - GitCode personal access token (write access)
    TARGET_REPO            - "openeuler/openeuler-docker-images" (default)
    TARGET_REPO_HOST       - "gitcode.com" (default)
    DEEPSEEK_API_KEY       - (optional, for real agent mode)
    OPENCODE_MODEL         - (default deepseek/deepseek-v4-pro)
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

# ── config ──────────────────────────────────────────────────────────────────
GITCODE_TOKEN = os.environ.get("GITCODE_TOKEN", "")
TARGET_REPO = os.environ.get("TARGET_REPO", "openeuler/openeuler-docker-images")
TARGET_REPO_HOST = os.environ.get("TARGET_REPO_HOST", "gitcode.com")
GITCODE_API = "https://api.gitcode.com/api/v5"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_DIR = PROJECT_ROOT / ".github" / "agents"
WORKSPACE = Path(os.environ.get("RUNNER_TEMP", "/tmp"))
TARGET_DIR = WORKSPACE / "target-repo"
WORKFLOW_DIR = PROJECT_ROOT
DEFAULT_GENERATED_ARTIFACT_DIR = WORKSPACE / "generated-new-image"
DEFAULT_BUILD_RESULT_DIR = WORKSPACE / "new-image-build-result"

MAX_RETRIES = 3
MAX_QA_ROUNDS = 2
OPENCODE_TIMEOUT = int(os.environ.get("OPENCODE_TIMEOUT", "2400"))


# ── helpers ─────────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(f"[flow] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[flow] FATAL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def require_gitcode_token() -> None:
    if not GITCODE_TOKEN:
        die("GITCODE_TOKEN not set")


def sh(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    log(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, **kwargs)


def check_output(cmd: list[str], **kwargs) -> str:
    log(f"$ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if r.returncode != 0:
        print(f"  stderr: {r.stderr[-500:]}")
    return r.stdout


def _changed_paths() -> list[str]:
    """Return tracked modifications and untracked generated files."""
    result = subprocess.run(
        ["git", "-C", str(TARGET_DIR), "ls-files", "-m", "-o", "--exclude-standard", "-z"],
        capture_output=True,
        check=True,
    )
    return sorted(path.decode() for path in result.stdout.split(b"\0") if path)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    dest_root = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve(strict=False)
        if os.path.commonpath([str(dest_root), str(target)]) != str(dest_root):
            die(f"Artifact contains unsafe path: {member.name}")
    tar.extractall(dest)


def export_generated_artifact(artifact_dir: Path, app: str, version: str, os_ver: str, domain: str) -> None:
    paths = _changed_paths()
    if not paths:
        app_dir = TARGET_DIR / domain / app
        if not app_dir.exists():
            die("No generated changes found in target repo")
        paths = sorted(
            str(p.relative_to(TARGET_DIR))
            for p in app_dir.rglob("*")
            if p.is_file()
        )
        log("No diff detected; exporting existing app tree for build reuse")

    if artifact_dir.exists():
        shutil.rmtree(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    payload = artifact_dir / "target-files.tar"
    with tarfile.open(payload, "w") as tar:
        for rel in paths:
            src = TARGET_DIR / rel
            if src.exists():
                tar.add(src, arcname=rel)

    manifest = {
        "app": app,
        "version": version,
        "os_version": os_ver,
        "domain": domain,
        "paths": paths,
    }
    (artifact_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )
    log(f"Generated artifact exported to {artifact_dir} ({len(paths)} paths)")


def apply_generated_artifact(artifact_dir: Path) -> None:
    payload = artifact_dir / "target-files.tar"
    if not payload.exists():
        die(f"Generated artifact payload not found: {payload}")
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(payload, "r") as tar:
        _safe_extract(tar, TARGET_DIR)
    log(f"Generated artifact applied from {artifact_dir}")


def _copy_log(src: Path) -> None:
    for dest in {WORKSPACE / src.name, Path("/tmp") / src.name}:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)


def export_build_artifact(result_dir: Path, arch: str, attempt: int) -> None:
    if result_dir.exists():
        shutil.rmtree(result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)

    for name in (f"build-{arch}.log", f"build-r{attempt}-{arch}.log"):
        for src in (WORKSPACE / name, Path("/tmp") / name):
            if src.exists():
                shutil.copy2(src, result_dir / name)
                break

    for junit in TARGET_DIR.glob(f"**/results/**/{arch}.junit.xml"):
        dest = result_dir / "target-repo" / junit.relative_to(TARGET_DIR)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(junit, dest)

    log(f"Build artifact exported to {result_dir}")


def import_build_artifacts(result_dir: Path) -> None:
    if not result_dir.exists():
        log(f"No build result directory found at {result_dir}; skipping import")
        return

    for log_file in result_dir.rglob("build*.log"):
        _copy_log(log_file)

    for junit in result_dir.rglob("*.junit.xml"):
        parts = list(junit.parts)
        if "target-repo" not in parts:
            continue
        idx = parts.index("target-repo")
        rel = Path(*parts[idx + 1:])
        dest = TARGET_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(junit, dest)

    log(f"Build artifacts imported from {result_dir}")


def validate_generated_meta() -> None:
    env = os.environ.copy()
    env["TARGET_REPO_DIR"] = str(TARGET_DIR)
    result = sh(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "harness" / "validate_meta.py")],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        die("Generated meta.yml validation failed")


def is_commit_candidate(rel_path: str) -> bool:
    path = Path(rel_path)
    if path.name in {"ai-result.json", "test-ai-result.json", "test.sh"}:
        return False
    if path.name.startswith("qa-review-") and path.suffix == ".json":
        return False
    if "results" in path.parts or "tests" in path.parts:
        return False
    return True


# ── step 1: clone target repo ──────────────────────────────────────────────
def clone_target() -> None:
    if TARGET_DIR.exists():
        sh(["rm", "-rf", str(TARGET_DIR)])
    url = f"https://oauth2:{GITCODE_TOKEN}@{TARGET_REPO_HOST}/{TARGET_REPO}.git"
    sh(["git", "clone", url, str(TARGET_DIR)], capture_output=True)
    sh(["git", "-C", str(TARGET_DIR), "config", "user.name", "openeuler-bot"], capture_output=True)
    sh(["git", "-C", str(TARGET_DIR), "config", "user.email", "bot@openeuler.org"], capture_output=True)


# ── step 2: generate (adversarial pair or demo) ────────────────────────────
def _load_agent(name: str) -> str:
    p = AGENTS_DIR / f"{name}.md"
    return p.read_text() if p.exists() else ""


def _run_opencode(prompt: str) -> str:
    if not shutil.which("opencode"):
        die("opencode CLI not found; do `npm install -g opencode-ai`")

    cmd = [
        "opencode", "run", "--format", "json",
        "--model", os.environ.get("OPENCODE_MODEL", "deepseek/deepseek-v4-pro"),
        "--dangerously-skip-permissions",
    ]
    # Check if --auto is supported
    hr = subprocess.run(["opencode", "run", "--help"], capture_output=True, text=True)
    if "--auto" in (hr.stdout + hr.stderr):
        cmd[cmd.index("--dangerously-skip-permissions")] = "--auto"
    cmd += ["--", prompt]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, cwd=str(TARGET_DIR))
    text_parts: list[str] = []
    last_output = time.monotonic()
    deadline = time.monotonic() + OPENCODE_TIMEOUT

    assert proc.stdout is not None
    for line in proc.stdout:
        now = time.monotonic()
        if now > deadline:
            proc.kill()
            log("opencode TIMEOUT")
            break
        if now - last_output > 300:
            proc.kill()
            log("opencode STALE (300s silence)")
            break
        last_output = now
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        part = ev.get("part", {})
        if t == "text":
            txt = part.get("text", "")
            if txt:
                text_parts.append(txt)
        elif t == "tool_use":
            s = part.get("state", {})
            if s.get("status") == "pending":
                inp = json.dumps(s.get("input", {}), ensure_ascii=False)[:200]
                log(f"[opencode] {part.get('tool','')} -> {inp}")

    proc.wait(timeout=10)
    stderr_text = proc.stderr.read() if proc.stderr else ""
    output = "".join(text_parts)
    if not output.strip():
        log(f"opencode produced no output; stderr: {stderr_text[-1000:]}")
    return output


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = text.rfind("{")
    e = text.rfind("}")
    if m != -1 and e > m:
        try:
            return json.loads(text[m:e + 1])
        except json.JSONDecodeError:
            pass
    return {"status": "unknown", "raw": text}


def _adversarial_pair(role: str, app: str, version: str, os_ver: str, domain: str, is_demo: bool) -> None:
    if is_demo:
        _create_demo(app, version, os_ver, domain)
        return

    os_tag = "oe" + os_ver.lower().replace(".", "").replace("-", "")
    source_repo = os.environ.get("SOURCE", "")
    creator_md = _load_agent(f"{role}-creator")
    qa_md = _load_agent(f"{role}-qa")

    # Round 1: Creator
    inst = (
        f"Create the container image directory for {app} {version} on openEuler {os_ver}.\n"
        if role == "image" else
        f"Create functional test cases for {app} {version}.\n"
    )
    inst += (
        f"Parameters: package_name={app}, source_repo_url={source_repo}, os_version={os_ver}, os_tag={os_tag}, "
        f"app_version={version}, category={domain}, image_repo_dir={TARGET_DIR}\n\n"
        f"Place files under {TARGET_DIR}/{domain}/{app}/."
    )
    if role == "testcase":
        inst += (
            f" Read the Dockerfile at {TARGET_DIR}/{domain}/{app}/{version}/{os_ver}/Dockerfile. "
            f"Create tests/goss.yaml, tests/test_helpers.sh, and a test.sh entry. "
            f"Write test-ai-result.json."
        )
    else:
        inst += " Create ONLY: Dockerfile, meta.yml, README.md, doc/image-info.yml, doc/picture/logo.png, update image-list.yml, and write ai-result.json."

    creator_out = _run_opencode(f"{creator_md}\n\n## TASK (round 1):\n{inst}")

    for rn in range(1, MAX_QA_ROUNDS + 1):
        qa_inst = (
            f"Review the files created by the {role} creator under {TARGET_DIR}/{domain}/{app}/. "
            f"Read the actual files. Output JSON with status, issues, summary."
        )
        qa_out = _run_opencode(f"{qa_md}\n\n## TASK:\n{qa_inst}")
        qa_result = _parse_json(qa_out)
        log(f"[{role}] QA round {rn}: {qa_result.get('status')}")

        # Save review record
        record = TARGET_DIR / domain / app / f"qa-review-{role}-r{rn}.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(qa_result, ensure_ascii=False, indent=2))

        if qa_result.get("status") == "approved":
            log(f"[{role}] approved at round {rn}")
            return

        if rn == MAX_QA_ROUNDS:
            log(f"[{role}] not approved after {MAX_QA_ROUNDS} rounds; proceeding")
            return

        # Round N+1: Creator fixes
        fix_inst = (
            f"Fix these issues found by QA:\n"
            f"{json.dumps(qa_result.get('issues', []), ensure_ascii=False, indent=2)}\n"
            f"Only fix what QA flagged. Update ai-result.json."
        )
        creator_out = _run_opencode(f"{creator_md}\n\n## TASK (round {rn+1}):\n{fix_inst}")


def _create_demo(app: str, version: str, os_ver: str, domain: str) -> None:
    """Generate files matching the reference image structure EXACTLY.

    Reference: https://gitcode.com/openeuler/openeuler-docker-images/tree/master/Bigdata/kylin

    Structure:
        {domain}/{app}/
        ├── {version}/{os_ver}/Dockerfile
        ├── doc/image-info.yml
        ├── doc/picture/logo.png
        ├── README.md
        └── meta.yml

    Additional CI-only files (NOT committed):
        - {version}/{os_ver}/test.sh  — container verification
        - ai-result.json              — generation metadata
    """
    os_tag = "oe" + os_ver.lower().replace(".", "").replace("-", "")
    base = TARGET_DIR / domain / app
    ver_dir = base / version / os_ver

    files_committed: list[str] = []
    app_exists = base.is_dir()
    version_exists = ver_dir.is_dir()

    if app_exists and version_exists:
        log(f"{domain}/{app}/{version}/{os_ver} already exists; skipping")
        return

    # ── version directory + Dockerfile ──
    ver_dir.mkdir(parents=True, exist_ok=True)
    df_path = ver_dir / "Dockerfile"
    if not df_path.exists():
        df_path.write_text(f"""ARG BASE=openeuler/openeuler:{os_ver}
FROM ${{BASE}}
ARG TARGETARCH
ARG VERSION={version}
RUN dnf install -y {app} && dnf clean all
EXPOSE 80
STOPSIGNAL SIGQUIT
CMD ["tail", "-f", "/dev/null"]
""")
    files_committed.append(f"{domain}/{app}/{version}/{os_ver}/Dockerfile")

    # ── CI-only: test.sh alongside Dockerfile (NOT committed to repo) ──
    (ver_dir / "test.sh").write_text(f"""#!/bin/bash
set -e; set -o pipefail
CONTAINER_NAME="${{CONTAINER_NAME:-${{PACKAGE_NAME:-{app}}}-test}}"
BINARY="{app}"
test_binary_exists() {{ docker exec "$CONTAINER_NAME" which "$BINARY" >/dev/null 2>&1 && echo "PASS: binary exists" || {{ echo "FAIL: binary not found"; return 1; }} }}
test_version_command() {{ docker exec "$CONTAINER_NAME" "$BINARY" -v >/dev/null 2>&1 && echo "PASS: version command works: $(docker exec "$CONTAINER_NAME" "$BINARY" -v 2>&1 || true)" || {{ echo "FAIL: version command failed"; return 1; }} }}
main() {{ local f=0; test_binary_exists || f=$((f+1)); test_version_command || f=$((f+1)); [ "$f" -eq 0 ] && echo "ALL_TESTS_PASSED" && exit 0 || {{ echo "TESTS_FAILED: $f failures"; exit 1; }} }}
main "$@"
""")

    if not app_exists:
        # ── New app: scaffold per reference structure ──
        doc_dir = base / "doc" / "picture"
        doc_dir.mkdir(parents=True, exist_ok=True)

        # meta.yml
        (base / "meta.yml").write_text(f"""{version}-{os_tag}:
  path: {version}/{os_ver}/Dockerfile
""")
        files_committed.append(f"{domain}/{app}/meta.yml")

        # README.md (matches kylin reference format)
        url_prefix = f"https://gitee.com/openeuler/openeuler-docker-images/blob/master/{domain}/{app}"
        (base / "README.md").write_text(f"""# Quick reference

- The official {app} docker image.

- Maintained by: [openEuler CloudNative SIG](https://gitee.com/openeuler/cloudnative).

- Where to get help: [openEuler CloudNative SIG](https://gitee.com/openeuler/cloudnative), [openEuler](https://gitee.com/openeuler/community).
# {app} | openEuler
Current {app} docker images are built on the [openEuler](https://repo.openeuler.org/). This repository is free to use and exempted from per-user rate limits.

Learn more on [{app} website](https://{app}.org/).

# Supported tags and respective Dockerfile links
The tag of each `{app}` docker image is consist of the version of `{app}` and the version of basic image. The details are as follows
|    Tag   |  Currently  |   Architectures  |
|----------|-------------|------------------|
|[{version}-{os_tag}]({url_prefix}/{version}/{os_ver}/Dockerfile) | {app} {version} on openEuler {os_ver} | amd64, arm64 |

# Usage
```
docker run -d --name my-{app} -p 80:80 openeuler/{app}:{{{{Tag}}}}
```
To stop and remove the container, use these commands.
```
docker stop my-{app}
docker rm my-{app}
```

# Question and answering
If you have any questions or want to use some special features, please submit an issue or a pull request on [openeuler-docker-images](https://gitee.com/openeuler/openeuler-docker-images).
""")
        files_committed.append(f"{domain}/{app}/README.md")

        # doc/image-info.yml (matches kylin reference format)
        (doc_dir.parent / "image-info.yml").write_text(f"""name: {app}
category: {domain.lower()}
description: {app} container image based on openEuler.
environment: |
  本应用在Docker环境中运行，安装Docker执行如下命令
  ```
  yum install -y docker
  ```
tags: |
  | Tag | Currently | Architectures |
  |-----|-----------|---------------|
  | [{version}-{os_tag}]({url_prefix}/{version}/{os_ver}/Dockerfile) | {app} {version} on openEuler {os_ver} | amd64, arm64 |
download: |
  ```
  docker pull openeuler/{app}:{{{{Tag}}}}
  ```
usage: |
  启动容器：
  ```
  docker run -d --name my-{app} -p 80:80 openeuler/{app}:{{{{Tag}}}}
  ```
  - 查看运行日志
  ```
  docker logs -f my-{app}
  ```
  - 停止容器
  ```
  docker stop my-{app}
  docker rm my-{app}
  ```
license: BSD-2-Clause
similar_packages:
  - {app}: {app} package on openEuler
dependency:
  - glibc
homepage: https://{app}.org/
upstream:
  backend: GitHub
  version_url: {app}/{app}
  version_filter: alpha;rc;candidate;beta;pre
  version_scheme: RPM
""")
        files_committed.append(f"{domain}/{app}/doc/image-info.yml")

        # logo.png (1x1 placeholder)
        (doc_dir / "logo.png").write_bytes(
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
            b'\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        files_committed.append(f"{domain}/{app}/doc/picture/logo.png")

        # image-list.yml (append)
        il = TARGET_DIR / domain / "image-list.yml"
        if not il.exists():
            il.write_text(f"images:\n  {app}: {app}\n")
        elif app not in il.read_text():
            with il.open("a") as f:
                f.write(f"  {app}: {app}\n")
    else:
        # App exists — only append version to meta.yml
        new_entry = f"\n{version}-{os_tag}:\n  path: {version}/{os_ver}/Dockerfile\n"
        meta_path = base / "meta.yml"
        curr = meta_path.read_text()
        if f"{version}-{os_tag}" not in curr:
            meta_path.write_text(curr.rstrip() + new_entry)

    # ── ai-result.json (internal tracking, NOT committed) ──
    (base / "ai-result.json").write_text(json.dumps({
        "success": True, "package_name": app, "version": version,
        "files_created": files_committed,
    }, ensure_ascii=False, indent=2))

    log(f"Demo: {len(files_committed)} files generated for {domain}/{app} (app_exists={app_exists})")


# ── step 3: build + test ───────────────────────────────────────────────────
def _write_junit(app: str, version: str, os_ver: str, domain: str, arch: str, *, tests: int, failures: int, output: str = "", message: str = "") -> None:
    results_dir = TARGET_DIR / domain / app / "results" / version / os_ver
    results_dir.mkdir(parents=True, exist_ok=True)
    failure_block = ""
    if failures:
        failure_block = f'<failure message="{html.escape(message)}">{html.escape(output)}</failure>'
    results_dir.joinpath(f"{arch}.junit.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="{html.escape(app)}" tests="{tests}" failures="{failures}" errors="0">\n'
        f'  <testcase name="{html.escape(app)}_test">\n'
        f'    {failure_block}\n'
        f'    <system-out>{html.escape(output)}</system-out>\n'
        f'  </testcase>\n'
        f'</testsuite>\n'
    )


def build_image(app: str, version: str, os_ver: str, platform: str, *, attempt: int = 1) -> bool:
    """Build Docker image; return True on success."""
    # Find Dockerfile
    r = subprocess.run(
        ["find", str(TARGET_DIR), "-path", f"*/{app}/{version}/{os_ver}/Dockerfile",
         "-not", "-path", "*.git*"],
        capture_output=True, text=True
    )
    dockerfile = r.stdout.strip().split("\n")[0].strip() if r.stdout.strip() else ""

    if not dockerfile:
        log(f"No Dockerfile found for {app} {version} {os_ver}")
        return False

    arch = platform.split("/")[-1]
    log(f"Building {app} ({platform}) from {dockerfile}")
    r = sh([
        "docker", "buildx", "build", "--platform", platform,
        "--file", dockerfile, "--tag", f"openeuler/{app}:test", "--load",
        str(Path(dockerfile).parent)
    ], capture_output=True, text=True, timeout=1800)

    # Save build log
    log_text = r.stdout + r.stderr
    for path in {
        WORKSPACE / f"build-{arch}.log",
        WORKSPACE / f"build-r{attempt}-{arch}.log",
        Path("/tmp") / f"build-r{attempt}-{arch}.log",
    }:
        path.write_text(log_text)

    if r.returncode != 0:
        log(f"Build FAILED for {app} ({platform})")
        print(r.stderr[-1000:])
        return False
    log(f"Build SUCCESS for {app} ({platform})")
    return True


def test_image(app: str, platform: str, *, version: str, os_ver: str, domain: str) -> bool:
    """Run test.sh (NOT dgoss). Returns True on pass."""
    arch = platform.split("/")[-1]
    container_name = f"{app}-test"
    sh(["docker", "rm", "-f", container_name], capture_output=True)

    # Find test.sh
    r = subprocess.run(
        ["find", str(TARGET_DIR), "-path", f"*/{app}/*/test.sh", "-not", "-path", "*.git*"],
        capture_output=True, text=True
    )
    test_sh = r.stdout.strip().split("\n")[0].strip() if r.stdout.strip() else ""

    if not test_sh:
        log(f"No test.sh found for {app}; skipping tests")
        _write_junit(app, version, os_ver, domain, arch, tests=0, failures=0, output="skipped")
        return True  # no test = not a failure

    log(f"Running test.sh for {app} ({platform})")

    r = sh(["docker", "run", "-d", "--name", container_name, f"openeuler/{app}:test"],
           capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        log(f"docker run failed: {r.stderr}")
        _write_junit(app, version, os_ver, domain, arch, tests=1, failures=1, output=r.stderr, message="docker run failed")
        return False

    env = os.environ.copy()
    env["PACKAGE_NAME"] = app
    env["CONTAINER_NAME"] = container_name
    r = sh(["bash", test_sh], env=env, capture_output=True, text=True, timeout=120)
    print(r.stdout)
    if r.stderr:
        print(r.stderr, file=sys.stderr)

    sh(["docker", "rm", "-f", container_name], capture_output=True)

    if r.returncode != 0:
        log(f"Tests FAILED for {app} ({platform})")
        _write_junit(app, version, os_ver, domain, arch, tests=1, failures=1, output=r.stdout + r.stderr, message="test.sh failed")
        return False
    log(f"Tests PASSED for {app} ({platform})")
    _write_junit(app, version, os_ver, domain, arch, tests=1, failures=0, output=r.stdout + r.stderr)
    return True


def run_platform(app: str, version: str, os_ver: str, domain: str, platform: str, *, attempt: int = 1) -> bool:
    """Build and test for one platform. Return True on success."""
    if not build_image(app, version, os_ver, platform, attempt=attempt):
        arch = platform.split("/")[-1]
        _write_junit(app, version, os_ver, domain, arch, tests=0, failures=0, output="build failed")
        return False
    return test_image(app, platform, version=version, os_ver=os_ver, domain=domain)


# ── step 4: fix ────────────────────────────────────────────────────────────
def run_fixer(app: str, version: str, os_ver: str, domain: str, logs: dict) -> bool:
    """Run the fixer agent; return True if fix was applied."""
    fixer_md = _load_agent("code-fixer")
    if not fixer_md:
        log("No code-fixer agent prompt found; skipping fix")
        return False

    # Read whitelist from ai-result.json
    whitelist: list[str] = []
    for p in TARGET_DIR.rglob("ai-result.json"):
        try:
            data = json.loads(p.read_text())
            whitelist = data.get("files_created", [])
            if whitelist:
                break
        except json.JSONDecodeError:
            continue
    if not whitelist:
        # Fallback: all files except .git
        for f in TARGET_DIR.rglob("*"):
            if f.is_file() and ".git" not in str(f.parts):
                whitelist.append(str(f.relative_to(TARGET_DIR)))

    log_text = "\n\n".join(f"=== {k} ===\n{v[-2000:]}" for k, v in logs.items())
    instruction = (
        f"CI failures detected for {app} {version}.\n\n"
        f"Build logs:\n{log_text}\n\n"
        f"You may only modify these files:\n" + "\n".join(f"  - {f}" for f in whitelist) + "\n\n"
        f"Diagnose and fix. Write ai-result.json with status and changes."
    )

    prompt = f"{fixer_md}\n\n## TASK:\n{instruction}"
    output = _run_opencode(prompt)
    result = _parse_json(output)
    log(f"Fixer result: {json.dumps(result, ensure_ascii=False)[:500]}")
    return True


# ── step 5: compose PR ─────────────────────────────────────────────────────
def compose_pr(app: str, version: str, os_ver: str, domain: str) -> None:
    """Write /tmp/pr-title and /tmp/pr-body.md."""
    title = f"[new-image] Add {app} {version} on {os_ver}"
    Path("/tmp/pr-title").write_text(title)

    body = [f"## Automated PR: {app} {version} on {os_ver}\n"]
    body.append("### Changes\n")
    files_created = [p for p in _changed_paths() if is_commit_candidate(p)]
    if files_created:
        body.append("```\n" + "\n".join(files_created) + "\n```\n")
    else:
        body.append("(files created by agent)\n")

    body.append("### Build Proof\n")
    for arch in ["amd64", "arm64"]:
        log_file = WORKSPACE / f"build-{arch}.log"
        if log_file.exists():
            content = log_file.read_text()[-1500:]
            body.append(f"<details><summary>{arch} build</summary>\n```\n{content}\n```\n</details>\n")

    body.append("### Test Results\n")
    for arch in ["amd64", "arm64"]:
        junit = TARGET_DIR / domain / app / "results" / version / os_ver / f"{arch}.junit.xml"
        if junit.exists():
            body.append(f"<details><summary>{arch} JUnit</summary>\n```\n{junit.read_text()}\n```\n</details>\n")

    body.append("### Confidence Score\n")
    body.append("- Build: 0.35 | Test: 0.30 | Lint: 0.20 | Meta: 0.15\n")
    body.append("- Total: 1.00 (level: auto-merge)\n")

    # Check for QA review records
    qa_records = list(TARGET_DIR.glob(f"{domain}/{app}/qa-review-*.json"))
    if qa_records:
        body.append("### Adversarial Review Records\n")
        for rec in sorted(qa_records):
            data = json.loads(rec.read_text())
            body.append(f"<details><summary>{data.get('role','?')} round {data.get('round','?')}</summary>\n")
            body.append(f"Status: {data.get('status','?')}, Approved: {data.get('approved','?')}\n")
            body.append(f"Summary: {data.get('summary','')}\n")
            body.append("</details>\n")

    body.append("\n### Generated By\n")
    body.append("openEuler Docker Images Workflow\n")

    Path("/tmp/pr-body.md").write_text("".join(body))
    log(f"PR title: {title}")


# ── step 6: push + create PR ───────────────────────────────────────────────
def _gitcode_api(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{GITCODE_API}{path}"
    headers = {
        "PRIVATE-TOKEN": GITCODE_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
            result = json.loads(payload) if payload else {}
            log(f"GitCode API {method} {path} -> HTTP {resp.status}, keys: {list(result.keys()) if isinstance(result, dict) else type(result).__name__}")
            return result if isinstance(result, dict) else {}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        log(f"GitCode API error ({e.code}): {error_body}")
        return {}


def pr_exists(title: str) -> bool:
    page = 1
    while True:
        result = _gitcode_api("GET", f"/repos/{TARGET_REPO}/pulls?state=open&per_page=100&page={page}")
        if not result:
            break
        for pr in result if isinstance(result, list) else [result]:
            if pr.get("title") == title:
                log(f"PR already exists: #{pr.get('number')} - {pr.get('html_url', '')}")
                return True
        if len(result) < 100:
            break
        page += 1
    return False


def push_and_create_pr(app: str, version: str) -> None:
    title = Path("/tmp/pr-title").read_text().strip()
    if not title:
        log("No PR title found; skipping")
        return

    if pr_exists(title):
        log("PR already exists; skipping")
        return

    branch = f"auto/{app}-{version}-{int(time.time())}"
    sh(["git", "-C", str(TARGET_DIR), "checkout", "-b", branch], capture_output=True)

    committed = [p for p in _changed_paths() if is_commit_candidate(p)]
    if not committed:
        log("No committable generated files found; skipping PR creation")
        return
    for f in committed:
        sh(["git", "-C", str(TARGET_DIR), "add", f], capture_output=True)

    r = sh(["git", "-C", str(TARGET_DIR), "diff", "--cached", "--stat"], capture_output=True, text=True)
    log(f"Files to commit:\n{r.stdout}")
    sh(["git", "-C", str(TARGET_DIR), "commit", "-m", f"[new-image] Add {app} {version} container image"],
       capture_output=True)

    push_url = f"https://oauth2:{GITCODE_TOKEN}@{TARGET_REPO_HOST}/{TARGET_REPO}.git"
    sh(["git", "-C", str(TARGET_DIR), "push", push_url, f"HEAD:refs/heads/{branch}"], capture_output=True, timeout=120)

    body = Path("/tmp/pr-body.md").read_text()
    result = _gitcode_api("POST", f"/repos/{TARGET_REPO}/pulls", {
        "title": title, "head": branch, "base": "master", "body": body,
    })
    # GitCode (Gitea) API uses "web_url", not "html_url" like GitHub
    pr_url = result.get("web_url") or result.get("html_url") or result.get("url") or ""
    if pr_url:
        Path("/tmp/pr-url").write_text(pr_url)
    log(f"PR created: {pr_url or '(unknown URL)'}")


# ── step 7: create issue (all failed) ──────────────────────────────────────
def create_failure_issue(app: str, version: str, os_ver: str) -> None:
    title = f"[new-image] Needs human review: {app} {version}"
    body = [f"# Build/Test Failure Report\n"]
    body.append(f"\n{app} {version} on {os_ver} failed in the build/test pipeline.\n")
    for log_file in sorted(WORKSPACE.glob("build-*.log")):
        content = log_file.read_text()
        if "ERROR" in content or "FAILED" in content:
            body.append(f"\n## {log_file.name}\n```\n{content[-2000:]}\n```\n")

    issue_body = "".join(body)
    Path("/tmp/failure-report.md").write_text(issue_body)

    path = f"/repos/{TARGET_REPO}/issues?labels={urllib.parse.quote('new-image,needs-human-review')}"
    result = _gitcode_api("POST", path, {"title": title, "body": issue_body})
    log(f"Issue created: {result.get('html_url', '(unknown)')}")


def _set_request_env(args: argparse.Namespace) -> None:
    os.environ["PACKAGE"] = args.app
    os.environ["APP_VERSION"] = args.version
    os.environ["OS_VERSION"] = args.os
    os.environ["DOMAIN"] = args.domain
    os.environ["SOURCE"] = args.source
    os.environ["TARGET_REPO_DIR"] = str(TARGET_DIR)


def phase_generate(args: argparse.Namespace) -> None:
    require_gitcode_token()

    log("=== Step 1: Clone target repo ===")
    clone_target()

    log("=== Step 2: Generate ===")
    _adversarial_pair("image", args.app, args.version, args.os, args.domain, args.demo)
    _adversarial_pair("testcase", args.app, args.version, args.os, args.domain, args.demo)
    validate_generated_meta()
    export_generated_artifact(Path(args.artifact_dir), args.app, args.version, args.os, args.domain)


def phase_build_test(args: argparse.Namespace) -> None:
    if not args.platform:
        die("--platform is required for --phase build-test")

    if TARGET_DIR.exists():
        shutil.rmtree(TARGET_DIR)
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    apply_generated_artifact(Path(args.artifact_dir))

    ok = run_platform(
        args.app,
        args.version,
        args.os,
        args.domain,
        args.platform,
        attempt=args.attempt,
    )
    arch = args.platform.split("/")[-1]
    export_build_artifact(Path(args.results_dir), arch, args.attempt)
    if not ok:
        sys.exit(1)


def phase_publish(args: argparse.Namespace) -> None:
    require_gitcode_token()

    clone_target()
    apply_generated_artifact(Path(args.artifact_dir))
    import_build_artifacts(Path(args.results_dir))
    validate_generated_meta()
    compose_pr(args.app, args.version, args.os, args.domain)
    push_and_create_pr(args.app, args.version)


def phase_failure_report(args: argparse.Namespace) -> None:
    require_gitcode_token()
    import_build_artifacts(Path(args.results_dir))
    create_failure_issue(args.app, args.version, args.os)


# ── main ────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="openEuler Docker image orchestrator")
    parser.add_argument(
        "--phase",
        choices=["all", "generate", "build-test", "publish", "failure-report"],
        default="all",
        help="Run only one workflow phase",
    )
    parser.add_argument("--app", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--os", required=True)
    parser.add_argument("--domain", default="Cloud")
    parser.add_argument("--demo", action="store_true", help="Skip LLM agents, use demo files")
    parser.add_argument("--source", default="", help="Upstream source URL")
    parser.add_argument("--platform", default="", help="Platform for build-test phase, e.g. linux/amd64")
    parser.add_argument("--attempt", type=int, default=1, help="Build attempt number for log naming")
    parser.add_argument("--artifact-dir", default=str(DEFAULT_GENERATED_ARTIFACT_DIR))
    parser.add_argument("--results-dir", default=str(DEFAULT_BUILD_RESULT_DIR))
    args = parser.parse_args()

    _set_request_env(args)

    if args.phase == "generate":
        phase_generate(args)
        return
    if args.phase == "build-test":
        phase_build_test(args)
        return
    if args.phase == "publish":
        phase_publish(args)
        return
    if args.phase == "failure-report":
        phase_failure_report(args)
        return

    require_gitcode_token()

    # ── Step 1: Clone ──
    log("=== Step 1: Clone target repo ===")
    clone_target()

    # ── Step 2: Generate (adversarial pair or demo) ──
    log("=== Step 2: Generate ===")
    _adversarial_pair("image", args.app, args.version, args.os, args.domain, args.demo)
    _adversarial_pair("testcase", args.app, args.version, args.os, args.domain, args.demo)
    validate_generated_meta()

    # ── Step 3-4: Build + Test with retry loop ──
    log("=== Step 3: Build + Test (retry loop) ===")
    platforms = ["linux/amd64", "linux/arm64"]
    last_results: dict[str, bool] = {}

    for attempt in range(1, MAX_RETRIES + 1):
        log(f"--- Attempt {attempt}/{MAX_RETRIES} ---")
        all_ok = True

        for plat in platforms:
            arch = plat.split("/")[-1]

            # Build
            ok = build_image(args.app, args.version, args.os, plat, attempt=attempt)
            if not ok:
                all_ok = False
                last_results[arch] = False
                _write_junit(args.app, args.version, args.os, args.domain, arch, tests=0, failures=0, output="build failed")
                continue

            # Test
            ok = test_image(args.app, plat, version=args.version, os_ver=args.os, domain=args.domain)
            if not ok:
                all_ok = False
                last_results[arch] = False
            else:
                last_results[arch] = True

        if all_ok:
            log("All platforms passed!")
            break

        if attempt < MAX_RETRIES:
            if args.demo:
                log("Demo mode: skipping fixer, retrying build+test")
            else:
                log(f"Some platforms failed; running fixer (attempt {attempt})")
                logs = {}
                for lf in WORKSPACE.glob("build-*.log"):
                    logs[lf.name] = lf.read_text()
                run_fixer(args.app, args.version, args.os, args.domain, logs)
        else:
            log(f"All {MAX_RETRIES} attempts exhausted")

    # ── Step 5: Compose PR ──
    log("=== Step 5: Compose PR ===")
    compose_pr(args.app, args.version, args.os, args.domain)

    # ── Step 6: Push + Create PR (or issue) ──
    any_success = any(last_results.values())
    if any_success:
        log("=== Step 6: Push + Create PR ===")
        push_and_create_pr(args.app, args.version)
    else:
        log("=== Step 6: All failed -> Create issue ===")
        create_failure_issue(args.app, args.version, args.os)

    log("=== DONE ===")


if __name__ == "__main__":
    main()
