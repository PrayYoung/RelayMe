#!/usr/bin/env python3
"""Vendor-neutral registered coding executor launcher for RelayMe.

This launcher acts as the agent-local execution bridge between RelayMe's sandboxed
worktree/task-output environment and non-interactive coding agents (Codex CLI,
OpenCode, or deterministic mock runners).

It conforms to the executor runner interface:
  - If invoked with 'rm', cleans up and exits 0.
  - If invoked with 'run ... -v worktree:/workspace:rw -v output:/output:rw -v brief:/input/brief.txt:ro',
    extracts the mounts, prepares the execution environment, runs the registered coding agent
    within the isolated worktree, captures the executor-native report, runs the verification tests,
    and produces RelayMe review artifacts (executor_report.md, test.log, result.json).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

TASK_INSTRUCTIONS = """You are an autonomous coding executor working in a sandboxed disposable git repository.
Task brief:
{brief}

Requirements:
1. Inspect the repository to understand the structure and failure.
2. Make the minimal necessary code edits to fix the issue or implement the brief.
3. Run the local test suite (e.g. pytest) to verify that your changes pass.
4. Do NOT create git commits or attempt to push.
5. Conclude with a clear summary describing:
   - Root cause or problem investigated
   - Exact files modified
   - Verification tests executed and result
   - Any remaining observations
"""


def _find_mount(args: list[str], target_suffix: str) -> Path | None:
    for i, arg in enumerate(args[:-1]):
        if arg == "-v":
            val = args[i + 1]
            parts = val.split(":")
            if len(parts) >= 2 and parts[1] == target_suffix:
                return Path(parts[0]).resolve()
    return None


def _extract_paths(args: list[str]) -> tuple[Path, Path, Path]:
    # Support standard podman mounts:
    worktree = _find_mount(args, "/workspace")
    output = _find_mount(args, "/output")
    brief_path = _find_mount(args, "/input/brief.txt")

    # Also support direct flags:
    for i, arg in enumerate(args[:-1]):
        if arg in ("--worktree", "--workspace") and not worktree:
            worktree = Path(args[i + 1]).resolve()
        elif arg == "--output" and not output:
            output = Path(args[i + 1]).resolve()
        elif arg == "--brief" and not brief_path:
            brief_path = Path(args[i + 1]).resolve()

    if not worktree or not output:
        raise ValueError(f"Could not resolve worktree and output paths from args: {args}")
    if not brief_path or not brief_path.is_file():
        # Fallback to output/brief.txt if brief_path not specified separately
        brief_path = output / "brief.txt"

    return worktree, output, brief_path


def run_codex(worktree: Path, output: Path, brief: str, codex_bin: str) -> tuple[int, str]:
    report_file = output / "executor_report.md"
    prompt = TASK_INSTRUCTIONS.format(brief=brief)

    cmd = [
        codex_bin,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C", str(worktree),
        "-o", str(report_file),
        prompt,
    ]

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(worktree),
            timeout=300,
        )
        duration_s = time.monotonic() - start
        stdout_text = proc.stdout.decode("utf-8", errors="replace")
        stderr_text = proc.stderr.decode("utf-8", errors="replace")
    except Exception as exc:
        return 1, f"Codex execution failed: {exc}"

    if not report_file.is_file() or report_file.stat().st_size == 0:
        # Fallback: write last message or stdout summary if -o didn't capture
        report_file.write_text(stdout_text or "Codex completed without separate report output.\n", encoding="utf-8")

    return proc.returncode, stdout_text


def run_opencode(worktree: Path, output: Path, brief: str, opencode_bin: str) -> tuple[int, str]:
    report_file = output / "executor_report.md"
    prompt = TASK_INSTRUCTIONS.format(brief=brief)

    model = os.environ.get("RELAYME_OPENCODE_MODEL", "opencode/mimo-v2.5-free")
    cmd = [
        opencode_bin,
        "run",
        "-m", model,
        "--dir", str(worktree),
        prompt,
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(worktree),
            timeout=300,
        )
        stdout_text = proc.stdout.decode("utf-8", errors="replace")
    except Exception as exc:
        return 1, f"OpenCode execution failed: {exc}"

    if not report_file.is_file():
        report_file.write_text(stdout_text or "OpenCode execution completed.\n", encoding="utf-8")

    return proc.returncode, stdout_text


def run_mock(worktree: Path, output: Path, brief: str) -> tuple[int, str]:
    """Deterministic mock coding agent for testing and dry-runs."""
    report_file = output / "executor_report.md"
    report_content = (
        f"# Executor Report: Automated Diagnosis & Fix\n\n"
        f"## Brief\n{brief}\n\n"
        f"## Changes Made\n"
        f"- Diagnosed failing test in repository workspace.\n"
        f"- Applied minimal code fix in disposable worktree.\n\n"
        f"## Verification\n"
        f"All test suites passed successfully.\n"
    )
    report_file.write_text(report_content, encoding="utf-8")

    # If math_utils fixture exists, fix it deterministically
    math_file = worktree / "math_utils.py"
    if math_file.is_file():
        content = math_file.read_text(encoding="utf-8")
        if "return n * (n - 1)" in content:
            math_file.write_text(content.replace("return n * (n - 1)", "return n * (n + 1) // 2"), encoding="utf-8")

    return 0, "Mock executor completed successfully."


def run_verification_tests(worktree: Path, output: Path) -> tuple[int, list[dict[str, Any]]]:
    """Run pytest inside worktree and write test.log."""
    test_log_path = output / "test.log"
    pytest_bin = shutil.which("pytest") or sys.executable + " -m pytest"
    
    cmd = [sys.executable, "-m", "pytest", "-v", "--tb=short"]
    proc = subprocess.run(
        cmd,
        cwd=str(worktree),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    test_output = proc.stdout.decode("utf-8", errors="replace")
    test_log_path.write_text(test_output, encoding="utf-8")

    tests_run = []
    for line in test_output.splitlines():
        line_clean = line.strip()
        if " PASSED" in line_clean:
            test_name = line_clean.split()[0]
            tests_run.append({"name": test_name, "status": "passed"})
        elif " FAILED" in line_clean:
            test_name = line_clean.split()[0]
            tests_run.append({"name": test_name, "status": "failed"})

    return proc.returncode, tests_run


def _find_executable(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    nvm_dir = os.environ.get("NVM_DIR")
    nvm_base = Path(nvm_dir) if nvm_dir else Path.home() / ".nvm"
    node_versions = nvm_base / "versions" / "node"
    if node_versions.is_dir():
        for candidate in sorted(node_versions.glob("*/bin/" + name), reverse=True):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return ""


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] == "rm":
        sys.exit(0)

    # Handle run invocation
    if args[0] == "run":
        args = args[1:]

    worktree, output, brief_path = _extract_paths(args)
    brief = brief_path.read_text(encoding="utf-8") if brief_path.is_file() else ""

    engine = os.environ.get("RELAYME_EXECUTOR_ENGINE", "").lower()
    codex_bin = _find_executable("codex")
    opencode_bin = _find_executable("opencode")

    if not engine:
        if Path(codex_bin).is_file():
            engine = "codex"
        elif Path(opencode_bin).is_file():
            engine = "opencode"
        else:
            engine = "mock"

    if engine == "codex":
        code, log_out = run_codex(worktree, output, brief, codex_bin)
    elif engine == "opencode":
        code, log_out = run_opencode(worktree, output, brief, opencode_bin)
    else:
        code, log_out = run_mock(worktree, output, brief)

    test_exit, tests_run = run_verification_tests(worktree, output)

    # Write result.json
    all_passed = (test_exit == 0) and (code == 0)
    summary = "coding executor completed: tests passed" if all_passed else f"coding executor completed: tests returned {test_exit}"
    result_data = {
        "summary": summary,
        "engine": engine,
        "tests": tests_run,
        "warnings": [] if all_passed else [f"test exit code was {test_exit}"],
        "exit_code": 0 if all_passed else 1,
    }
    (output / "result.json").write_text(json.dumps(result_data, indent=2), encoding="utf-8")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
