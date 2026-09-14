"""Workspace-local Git: hardening flags on every call, trailers, no route out (§5.7)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from slas_git.workspace import (
    HARDENING_ARGS,
    HARDENING_ENV,
    PUSH_EXPLANATION,
    CommandResult,
    FakeGitExec,
    GitError,
    GitWorkspace,
    Identity,
    LocalGitExec,
    agent_trailers,
    parse_trailers,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def test_every_call_carries_the_hardening_flags_and_identity() -> None:
    fake = FakeGitExec()
    ws = GitWorkspace(fake, cwd="/workspace", identity=Identity.for_user("pat", "Pat Lin"))
    ws.add_all()
    argv, cwd, env, stdin = fake.calls[0]
    assert argv[0] == "git" and argv[1 : 1 + len(HARDENING_ARGS)] == list(HARDENING_ARGS)
    assert argv[-2:] == ["add", "--all"] and cwd == "/workspace" and stdin is None
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == "/bin/false" and env["GIT_AUTHOR_EMAIL"] == "pat@slas.local"
    assert "core.hooksPath=/var/empty" in argv and "credential.helper=" in argv
    assert "protocol.allow=never" in argv, "no remote protocol works from the workspace"
    for key in HARDENING_ENV:
        assert key in env


def test_git_errors_are_three_part_sentences() -> None:
    fake = FakeGitExec()
    fake.script("commit", result=CommandResult(exit_code=1, stderr="fatal: nothing to commit\n"))
    ws = GitWorkspace(fake, cwd="/workspace")
    with pytest.raises(GitError) as raised:
        ws.commit("x")
    assert raised.value.message.what_happened == "git commit did not finish in /workspace."
    assert raised.value.message.likely_cause == "fatal: nothing to commit"
    fake.script("rev-parse", "--abbrev-ref", result=CommandResult(exit_code=128))
    with pytest.raises(GitError) as silent:
        ws.current_branch()
    assert silent.value.message.likely_cause == "git exited with code 128."
    assert silent.value.message.what_to_do.startswith("Open the Terminal tab")
    with pytest.raises(ValueError, match="needs a subject"):
        ws.commit("   ")
    assert ws.explain_push() == PUSH_EXPLANATION


def test_parse_trailers_reads_the_last_paragraph_only() -> None:
    assert parse_trailers("Body text.\n\nSlas-Agent: coding\nSlas-Ticket: T-coding-0001\n") == {
        "Slas-Agent": "coding",
        "Slas-Ticket": "T-coding-0001",
    }
    assert parse_trailers("Just prose, with a colon: here and there") == {}
    assert parse_trailers("") == {}
    assert agent_trailers("coding", "T-coding-0007") == {
        "Slas-Agent": "coding",
        "Slas-Ticket": "T-coding-0007",
    }


@needs_git
def test_real_repo_init_branch_commit_with_trailers_log_and_diff(tmp_path: Path) -> None:
    ws = GitWorkspace(
        LocalGitExec(), cwd=str(tmp_path), identity=Identity.for_user("pat", "Pat Lin")
    )
    assert not ws.is_repo()
    ws.init()
    ws.init()  # idempotent
    assert ws.is_repo() and ws.head_sha() is None and ws.log() == []
    assert ws.remotes() == [], "a fresh workspace has no remote"

    (tmp_path / "README.md").write_text("# Fan\n", encoding="utf-8")
    assert ws.has_changes() and ws.status() == [("??", "README.md")]
    ws.checkout_branch("slas/T-coding-0001")
    ws.add_all()
    sha = ws.commit(
        "Add README", body="- first task", trailers=agent_trailers("coding", "T-coding-0001")
    )
    assert ws.current_branch() == "slas/T-coding-0001" and ws.head_sha() == sha
    (commit,) = ws.log()
    assert commit.sha == sha and commit.subject == "Add README" and commit.by_agent
    assert commit.trailers == {"Slas-Agent": "coding", "Slas-Ticket": "T-coding-0001"}
    assert not ws.has_changes()

    # A person's commit has no trailers; the history tells the two apart.
    (tmp_path / "notes.txt").write_text("mine\n", encoding="utf-8")
    ws.add_all()
    ws.commit("Personal note")
    agent_commits = [c for c in ws.log() if c.by_agent]
    assert len(ws.log()) == 2 and len(agent_commits) == 1

    ws.checkout_branch("slas/T-coding-0001")  # existing branch: plain checkout
    diff = ws.diff("4b825dc642cb6eb9a060e54bf8d69288fbee4904")  # the empty tree
    assert (
        "+# Fan" in diff and ws.diff("4b825dc642cb6eb9a060e54bf8d69288fbee4904", stat=True).strip()
    )

    # Hooks never run: a hostile pre-commit hook in the repo is ignored (core.hooksPath).
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho HOOK_RAN > hook.txt\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    (tmp_path / "again.txt").write_text("x\n", encoding="utf-8")
    ws.add_all()
    ws.commit("Third")
    assert not (tmp_path / "hook.txt").exists(), "the hook did not execute"

    # And a push has nowhere to go: no remote, protocol.allow=never.
    result = ws.git("push", check=False)
    assert result.exit_code != 0
