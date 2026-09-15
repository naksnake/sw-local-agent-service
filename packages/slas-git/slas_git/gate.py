"""The validation gate every push passes (CLAUDE.md §5.7).

    changes inside the project path · secret scan · no new hooks, submodules or symlinks
    escaping the tree · size and file count sane · LFS off unless enabled ·
    agent-authored diffs also pass the Consensus Router (§5.3)

Deterministic code decides every check except the last; the cross-check is input to the
gate's verdict, never the verdict itself (INV-11), and a person can still stop the push.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import Field

from slas_git.redact import SECRET_SHAPES
from slas_git.workspace import CommandResult, GitWorkspace
from slas_schemas.common import SlasModel
from slas_schemas.vote import ConsensusVerdict

HOOK_DIRS = ("hooks", ".githooks", ".husky", ".git-hooks")
_LFS = re.compile(r"filter\s*=\s*lfs")


class GatePolicy(SlasModel):
    max_files: int = Field(default=2000, ge=1)
    max_blob_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    max_total_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    lfs_enabled: bool = False


class GateCheck(SlasModel):
    name: str
    ok: bool
    sentence: str


class GateReport(SlasModel):
    checks: list[GateCheck] = Field(default_factory=list)
    verdict: ConsensusVerdict | None = None

    @property
    def passed(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def problems(self) -> list[str]:
        return [check.sentence for check in self.checks if not check.ok]

    def sentence(self) -> str:
        if self.passed:
            return f"All {len(self.checks)} checks passed."
        return f"{len(self.problems)} of {len(self.checks)} checks failed: " + " ".join(
            self.problems
        )


class SecretFinding(SlasModel):
    path: str
    rule: str
    line: int | None = None


class SecretScanner(Protocol):
    name: str

    def scan(self, diff_text: str) -> list[SecretFinding]: ...


class RegexSecretScanner:
    """Built-in fallback when gitleaks is not installed: the credential shapes from redact.py,
    applied to added lines of the diff."""

    name = "built-in patterns"

    def scan(self, diff_text: str) -> list[SecretFinding]:
        findings: list[SecretFinding] = []
        path = "?"
        line_no = 0
        for line in diff_text.splitlines():
            if line.startswith("+++ b/"):
                path = line[6:]
                continue
            if line.startswith("@@"):
                match = re.search(r"\+(\d+)", line)
                line_no = int(match.group(1)) - 1 if match else 0
                continue
            if line.startswith("+") and not line.startswith("+++"):
                line_no += 1
                for name, pattern in SECRET_SHAPES:
                    if name in ("askpass_answer", "authorization_header"):
                        continue
                    if pattern.search(line):
                        findings.append(SecretFinding(path=path, rule=name, line=line_no))
                        break
            elif not line.startswith("-"):
                line_no += 1
        return findings


class GitleaksRunner(Protocol):
    def run(self, argv: Sequence[str], *, stdin: str) -> CommandResult: ...


class GitleaksScanner:
    """`gitleaks detect --pipe` over the diff; JSON report on stdout."""

    name = "gitleaks"

    def __init__(self, runner: GitleaksRunner) -> None:
        self.runner = runner

    def scan(self, diff_text: str) -> list[SecretFinding]:
        import json

        result = self.runner.run(
            [
                "gitleaks",
                "detect",
                "--pipe",
                "--no-banner",
                "--report-format",
                "json",
                "--report-path",
                "/dev/stdout",
                "--exit-code",
                "0",
            ],
            stdin=diff_text,
        )
        try:
            data = json.loads(result.stdout or "[]")
        except ValueError:
            data = []
        findings: list[SecretFinding] = []
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict):
                findings.append(
                    SecretFinding(
                        path=str(item.get("File", "?")),
                        rule=str(item.get("RuleID", "gitleaks")),
                        line=int(item["StartLine"])
                        if isinstance(item.get("StartLine"), int)
                        else None,
                    )
                )
        return findings


class CrossChecker(Protocol):
    def cross_check(self, decision: str, evidence: list[str]) -> ConsensusVerdict: ...


def _escapes(link_target: str, link_path: str) -> bool:
    if link_target.startswith("/"):
        return True
    depth = len(PurePosixPath(link_path).parts) - 1
    level = 0
    for part in PurePosixPath(link_target).parts:
        if part == "..":
            level += 1
            if level > depth:
                return True
        elif part != ".":
            level -= 1
    return False


def validate_push(
    workspace: GitWorkspace,
    *,
    base: str,
    head: str,
    policy: GatePolicy,
    scanner: SecretScanner,
    agent_authored: bool = False,
    cross_checker: CrossChecker | None = None,
    protected_branch: bool = False,
    may_push_protected: bool = False,
) -> GateReport:
    checks: list[GateCheck] = []

    def check(name: str, ok: bool, good: str, bad: str) -> None:
        checks.append(GateCheck(name=name, ok=ok, sentence=good if ok else bad))

    # What changed between base and head: mode, sha and path per entry.
    tree = workspace.git(
        "diff-tree", "-r", "--no-commit-id", "--diff-filter=ACMRT", base, head
    ).stdout
    entries: list[tuple[str, str, str]] = []
    for line in tree.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 4:
            entries.append((parts[1], parts[3], path))  # new mode, new sha, path
    paths = [path for _, _, path in entries]

    # 1 · path scope: nothing may name a parent, an absolute path or the repo metadata.
    bad_paths = [
        p
        for p in paths
        if p.startswith("/") or ".." in PurePosixPath(p).parts or p.split("/")[0] == ".git"
    ]
    check(
        "path_scope",
        not bad_paths,
        "Every change stays inside the project.",
        f"Changes leave the project path: {', '.join(bad_paths[:3])}.",
    )

    # 2 · hooks, submodules, escaping symlinks.
    hooks = [p for p in paths if any(part in HOOK_DIRS for part in PurePosixPath(p).parts[:-1])]
    check(
        "hooks",
        not hooks,
        "No hook files were added.",
        f"Hook files are not accepted: {', '.join(hooks[:3])}.",
    )
    submodules = [p for p in paths if p == ".gitmodules"] + [
        p for mode, _, p in entries if mode == "160000"
    ]
    check(
        "submodules",
        not submodules,
        "No submodule was added.",
        "Submodules are not accepted: they pull code from somewhere the gate cannot see.",
    )
    escaping: list[str] = []
    for mode, sha, path in entries:
        if mode == "120000":
            target = workspace.git("cat-file", "-p", sha).stdout.strip()
            if _escapes(target, path):
                escaping.append(f"{path} → {target}")
    check(
        "symlinks",
        not escaping,
        "No symlink points outside the tree.",
        f"Symlinks escape the tree: {', '.join(escaping[:3])}.",
    )

    # 3 · size and count.
    total = 0
    oversized: list[str] = []
    for mode, sha, path in entries:
        if mode in ("100644", "100755"):
            size = int(workspace.git("cat-file", "-s", sha).stdout.strip() or "0")
            total += size
            if size > policy.max_blob_bytes:
                oversized.append(f"{path} ({size:,} bytes)")
    check(
        "size",
        len(paths) <= policy.max_files and total <= policy.max_total_bytes and not oversized,
        f"{len(paths)} {'file' if len(paths) == 1 else 'files'}, {total:,} bytes changed.",
        f"Too much for one push: {len(paths)} files, {total:,} bytes"
        + (f"; oversized: {', '.join(oversized[:3])}" if oversized else "")
        + f". The limits are {policy.max_files} files and {policy.max_total_bytes:,} bytes.",
    )

    # 4 · LFS.
    lfs_hits: list[str] = []
    if not policy.lfs_enabled:
        for mode, sha, path in entries:
            is_attributes = PurePosixPath(path).name == ".gitattributes" and mode.startswith("100")
            if is_attributes and _LFS.search(workspace.git("cat-file", "-p", sha).stdout):
                lfs_hits.append(path)
    check(
        "lfs",
        not lfs_hits,
        "Git LFS is not used." if not policy.lfs_enabled else "Git LFS is enabled for this remote.",
        f"Git LFS is off for this remote, but {', '.join(lfs_hits)} turns it on.",
    )

    # 5 · secrets.
    diff_text = workspace.git("diff", "--no-color", "--no-ext-diff", base, head).stdout
    findings = scanner.scan(diff_text)
    check(
        "secrets",
        not findings,
        f"No secret found by {scanner.name}.",
        f"{scanner.name} found {len(findings)} secret-like "
        f"{'value' if len(findings) == 1 else 'values'}: "
        + ", ".join(f"{f.path}:{f.line} ({f.rule})" for f in findings[:3])
        + ". Remove them and commit again.",
    )

    # 6 · protected branch policy.
    check(
        "branch_policy",
        not protected_branch or may_push_protected,
        "Pushing to a branch."
        if not protected_branch
        else "Pushing to a protected branch, which you may do.",
        "Direct pushes to a protected branch need git:push_protected, which is off by default; "
        "push to a branch and open a merge request instead.",
    )

    # 7 · agent-authored diffs pass the Consensus Router.
    verdict: ConsensusVerdict | None = None
    if agent_authored:
        if cross_checker is None:
            check(
                "cross_check",
                False,
                "",
                "An agent wrote this change and no voters are configured to check it; review it "
                "yourself before pushing.",
            )
        else:
            verdict = cross_checker.cross_check(
                "code_change",
                [f"Diff stat:\n{workspace.diff(base, stat=True)}", f"Diff:\n{diff_text[:40_000]}"],
            )
            check("cross_check", verdict.agreed, verdict.sentence, verdict.sentence)
    return GateReport(checks=checks, verdict=verdict)
