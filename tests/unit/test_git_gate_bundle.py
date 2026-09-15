"""The validation gate on real repositories, and bundles in and out (§5.7)."""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from slas_git.bundle import export_bundle, import_bundle, verify_bundle
from slas_git.gate import (
    GatePolicy,
    GitleaksScanner,
    RegexSecretScanner,
    SecretFinding,
    validate_push,
)
from slas_git.workspace import CommandResult, GitError, GitWorkspace, Identity, LocalGitExec
from slas_kernel.rca import FakeCrossChecker
from slas_schemas.vote import Vote

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def repo(path: Path) -> GitWorkspace:
    path.mkdir(parents=True, exist_ok=True)
    ws = GitWorkspace(LocalGitExec(), cwd=str(path), identity=Identity.for_user("pat", "Pat"))
    ws.init()
    (path / "README.md").write_text("# base\n", encoding="utf-8")
    ws.add_all()
    ws.commit("Base")
    return ws


def commit_on_branch(
    ws: GitWorkspace, branch: str, files: dict[str, str], *, symlink: tuple[str, str] | None = None
) -> str:
    ws.git("checkout", "--quiet", "main")
    ws.checkout_branch(branch)
    root = Path(ws.cwd)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if symlink is not None:
        os.symlink(symlink[1], root / symlink[0])
    ws.add_all()
    return ws.commit(f"Change on {branch}")


def gate(
    ws: GitWorkspace,
    head: str,
    *,
    policy: GatePolicy | None = None,
    protected_branch: bool = False,
    may_push_protected: bool = False,
) -> tuple[bool, dict[str, bool]]:
    base = ws.git("rev-parse", "main").stdout.strip()
    report = validate_push(
        ws,
        base=base,
        head=head,
        policy=policy or GatePolicy(),
        scanner=RegexSecretScanner(),
        protected_branch=protected_branch,
        may_push_protected=may_push_protected,
    )
    return report.passed, {c.name: c.ok for c in report.checks}


def test_a_clean_change_passes_every_check(tmp_path: Path) -> None:
    ws = repo(tmp_path)
    head = commit_on_branch(ws, "feature", {"src/app.py": "print('hi')\n"})
    base = ws.git("rev-parse", "main").stdout.strip()
    report = validate_push(
        ws, base=base, head=head, policy=GatePolicy(), scanner=RegexSecretScanner()
    )
    assert report.passed and report.problems == []
    assert report.sentence() == "All 8 checks passed."
    assert [c.name for c in report.checks] == [
        "path_scope",
        "hooks",
        "submodules",
        "symlinks",
        "size",
        "lfs",
        "secrets",
        "branch_policy",
    ]
    assert report.checks[4].sentence == "1 file, 12 bytes changed."


@pytest.mark.parametrize(
    ("files", "symlink", "failing", "fragment"),
    [
        (
            {".githooks/pre-push": "#!/bin/sh\nexit 0\n"},
            None,
            "hooks",
            "Hook files are not accepted",
        ),
        ({".husky/pre-commit": "x"}, None, "hooks", "Hook files are not accepted"),
        (
            {".gitmodules": '[submodule "x"]\n\tpath = x\n\turl = https://evil/x.git\n'},
            None,
            "submodules",
            "Submodules are not accepted",
        ),
        (
            {},
            ("escape", "../../etc/passwd"),
            "symlinks",
            "Symlinks escape the tree: escape → ../../etc/passwd",
        ),
        ({}, ("abs", "/etc/passwd"), "symlinks", "Symlinks escape the tree"),
        (
            {".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n"},
            None,
            "lfs",
            "Git LFS is off for this remote",
        ),
        (
            {"config.py": 'TOKEN = "glpat-abcdefghijklmnopqrst"\n'},
            None,
            "secrets",
            "built-in patterns found 1 secret-like value: config.py:1 (gitlab_pat)",
        ),
    ],
)
def test_the_gate_refuses_hooks_submodules_escaping_symlinks_lfs_and_secrets(
    tmp_path: Path,
    files: dict[str, str],
    symlink: tuple[str, str] | None,
    failing: str,
    fragment: str,
) -> None:
    ws = repo(tmp_path)
    head = commit_on_branch(ws, "feature", files, symlink=symlink)
    base = ws.git("rev-parse", "main").stdout.strip()
    report = validate_push(
        ws, base=base, head=head, policy=GatePolicy(), scanner=RegexSecretScanner()
    )
    assert not report.passed
    failed = {c.name for c in report.checks if not c.ok}
    assert failed == {failing}, report.sentence()
    assert fragment in report.sentence()


def test_size_lfs_enabled_symlink_inside_and_protected_branch_policy(tmp_path: Path) -> None:
    ws = repo(tmp_path)
    head = commit_on_branch(
        ws,
        "feature",
        {"big.txt": "x" * 2000, "a/b/c.txt": "c\n"},
        symlink=("a/link", "../a/b/c.txt"),
    )
    passed, checks = gate(ws, head, policy=GatePolicy(max_blob_bytes=1000))
    assert not passed and checks["size"] is False and checks["symlinks"] is True, (
        "a symlink inside the tree is fine"
    )
    passed, checks = gate(ws, head, policy=GatePolicy(max_files=1))
    assert not passed and checks["size"] is False
    lfs = commit_on_branch(ws, "lfs", {".gitattributes": "*.bin filter=lfs\n"})
    passed, checks = gate(ws, lfs, policy=GatePolicy(lfs_enabled=True))
    assert passed and checks["lfs"] is True
    passed, checks = gate(ws, head, protected_branch=True)
    assert not passed and checks["branch_policy"] is False
    passed, checks = gate(ws, head, protected_branch=True, may_push_protected=True)
    assert passed


def test_agent_authored_changes_go_through_the_consensus_router(tmp_path: Path) -> None:
    ws = repo(tmp_path)
    head = commit_on_branch(ws, "slas/T-coding-0001", {"src/app.py": "print('hi')\n"})
    base = ws.git("rev-parse", "main").stdout.strip()
    votes = [
        Vote(voter=f"v{i}", verdict="approve", reason="fine", confidence=0.9) for i in range(3)
    ]
    checker = FakeCrossChecker(votes, agreed=True)
    report = validate_push(
        ws,
        base=base,
        head=head,
        policy=GatePolicy(),
        scanner=RegexSecretScanner(),
        agent_authored=True,
        cross_checker=checker,
    )
    assert report.passed and report.verdict is not None and len(report.verdict.votes) == 3
    assert checker.calls[0][0] == "code_change" and "+print('hi')" in checker.calls[0][1][1]
    assert (
        report.checks[-1].name == "cross_check"
        and report.checks[-1].sentence == "3 of 3 agree with the conclusion."
    )

    disagree = FakeCrossChecker(
        [votes[0].model_copy(update={"verdict": "reject", "reason": "no test"})], agreed=False
    )
    report = validate_push(
        ws,
        base=base,
        head=head,
        policy=GatePolicy(),
        scanner=RegexSecretScanner(),
        agent_authored=True,
        cross_checker=disagree,
    )
    assert not report.passed and "cross_check" in {c.name for c in report.checks if not c.ok}

    report = validate_push(
        ws,
        base=base,
        head=head,
        policy=GatePolicy(),
        scanner=RegexSecretScanner(),
        agent_authored=True,
    )
    assert not report.passed and "no voters are configured" in report.sentence()

    person = validate_push(
        ws,
        base=base,
        head=head,
        policy=GatePolicy(),
        scanner=RegexSecretScanner(),
        agent_authored=False,
        cross_checker=checker,
    )
    assert person.verdict is None and len(checker.calls) == 1, (
        "a person's own push is not cross-checked"
    )


def test_gitleaks_scanner_parses_the_json_report() -> None:
    class Runner:
        def __init__(self) -> None:
            self.stdin = ""

        def run(self, argv: Sequence[str], *, stdin: str) -> CommandResult:
            self.stdin = stdin
            assert argv[:3] == ["gitleaks", "detect", "--pipe"]
            return CommandResult(
                exit_code=0,
                stdout='[{"File": "a.py", "RuleID": "gitlab-pat", "StartLine": 3}, {"junk": 1}]',
            )

    runner = Runner()
    findings = GitleaksScanner(runner).scan("+++ b/a.py\n+x\n")
    assert findings == [
        SecretFinding(path="a.py", rule="gitlab-pat", line=3),
        SecretFinding(path="?", rule="gitleaks"),
    ]
    assert runner.stdin.startswith("+++ b/a.py")
    assert GitleaksScanner(runner).name == "gitleaks"


def test_bundle_export_verify_import(tmp_path: Path) -> None:
    source = repo(tmp_path / "a")
    commit_on_branch(source, "feature", {"f.txt": "f\n"})
    info = export_bundle(source, tmp_path / "Bundles" / "a.bundle")
    assert Path(info.path).is_file() and info.size_bytes > 0
    assert set(info.refs) >= {"refs/heads/main", "refs/heads/feature"}
    assert info.sentence().startswith("Bundle a.bundle: ")

    target = GitWorkspace(LocalGitExec(), cwd=str(tmp_path / "b"))
    (tmp_path / "b").mkdir()
    target.init()
    assert sorted(verify_bundle(target, Path(info.path))) == sorted(info.refs)
    branches = import_bundle(target, Path(info.path))
    assert sorted(branches) == ["feature", "main"]
    assert (
        target.git("rev-parse", "refs/remotes/bundle/feature").stdout.strip()
        == source.git("rev-parse", "feature").stdout.strip()
    )
    with pytest.raises(GitError, match="is not a file"):
        verify_bundle(target, tmp_path / "missing.bundle")
    (tmp_path / "junk.bundle").write_bytes(b"not a bundle")
    with pytest.raises(GitError, match="cannot be applied"):
        verify_bundle(target, tmp_path / "junk.bundle")
    only_main = export_bundle(source, tmp_path / "Bundles" / "main.bundle", refs=["main"])
    assert only_main.refs == ["refs/heads/main"]
