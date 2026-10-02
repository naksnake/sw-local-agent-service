"""The Coding Agent on the kernel: plan → breakdown → toolchain → iterate → commit → check → ZIP."""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slas_git.workspace import GitWorkspace, LocalGitExec
from slas_kernel.clock import FakeClock
from slas_kernel.executor import ExecutionContext
from slas_kernel.kernel import Kernel
from slas_kernel.rca import FakeCrossChecker
from slas_kernel.store import FileTicketStore
from slas_orchestrator.coding.agent import BreakdownNotApprovedError, CodingAgent
from slas_orchestrator.coding.breakdown import (
    Breakdown,
    FakeBreakdowner,
    LanguageChoice,
    TaskItem,
    files_named,
    propose,
)
from slas_orchestrator.coding.coder import MAX_CHECK_OUTPUT_CHARS, build_messages
from slas_orchestrator.coding.executor import (
    CheckResult,
    CodingExecutor,
    EditError,
    EditRequest,
    EditSet,
    FakeCoder,
    Replacement,
    apply_edits,
    apply_replacements,
    fix_script_modes,
    missing_files,
    snapshot,
)
from slas_orchestrator.coding.export import zip_project
from slas_orchestrator.coding.plan_doc import PlanError, parse_plan
from slas_sandbox_manager.manager import SandboxManager, Session
from slas_sandbox_manager.runtime import ExecResult, FakeSandboxRuntime
from slas_sandbox_manager.toolchains import default_manifest
from slas_schemas.job import MesTicket, Upload
from slas_schemas.plan import Step
from slas_schemas.ticket import TicketState
from slas_schemas.vote import Vote, VoteVerdict

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

PLAN = """# Fan controller

- Parse `config.yaml` into a dataclass in `fan_ctl.py`.
- Add a `pytest` test for the parser.

```python
import fan_ctl
```
"""


def votes(*verdicts: VoteVerdict) -> list[Vote]:
    return [
        Vote(
            voter=f"voter-{i}",
            verdict=v,
            reason="looks right" if v == "approve" else "missing a test",
            confidence=0.8,
        )
        for i, v in enumerate(verdicts, start=1)
    ]


# --- ingest and breakdown -------------------------------------------------------------------


def test_parse_plan_reads_title_tasks_and_languages() -> None:
    doc = parse_plan(PLAN)
    assert doc.title == "Fan controller"
    assert doc.tasks == [
        "Parse `config.yaml` into a dataclass in `fan_ctl.py`.",
        "Add a `pytest` test for the parser.",
    ]
    assert doc.languages[0] == "python" and "config" in doc.languages
    assert doc.job_id.startswith("job-") and doc.sentence().startswith("Fan controller: 2 tasks;")
    assert parse_plan("just a paragraph", filename="fan-plan.md").title == "fan plan"
    with pytest.raises(PlanError, match="is empty"):
        parse_plan("   \n")
    with pytest.raises(PlanError, match="larger than 256 KB"):
        parse_plan("- x\n" * 100_000)


def test_propose_uses_the_plan_bullets_or_the_breakdowner() -> None:
    doc = parse_plan(PLAN)
    fallback = propose(doc)
    assert [t.title for t in fallback.tasks] == doc.tasks
    assert fallback.languages[0] == LanguageChoice(language="python", version=None)
    drafted = propose(
        doc, breakdowner=FakeBreakdowner([TaskItem(n=1, title="One task", files=["fan_ctl.py"])])
    )
    assert [t.title for t in drafted.tasks] == ["One task"]
    assert propose(parse_plan("# Title only")).tasks[0].title == "Title only"
    manifest = default_manifest()
    assert fallback.sentence(manifest) == (
        "The agent will work in an isolated sandbox with Python 3.12.6, YAML/JSON config 1.35.1, "
        "do 2 tasks, commit on its own branch, cross-check the result with 3 voters, and export "
        "a ZIP."
    )
    pinned = fallback.model_copy(
        update={
            "languages": [LanguageChoice(language="rust", version="1.99")],
            "cross_check": False,
        }
    )
    assert pinned.sentence(manifest).endswith(
        "skip the cross-check, as you asked, and export a ZIP. Rust 1.99 isn't in the offline "
        "toolchain bundle, so the newest bundled 1.80.1 is used instead."
    )
    with pytest.raises(ValueError, match="needs the name of a saved remote"):
        fallback.model_copy(update={"export": "remote"}).model_validate(
            {**fallback.model_dump(), "export": "remote"}
        )


def test_agent_ingests_only_plan_uploads_and_plans_only_approved_breakdowns() -> None:
    agent = CodingAgent(now=lambda: datetime(2026, 9, 14, tzinfo=UTC))
    with pytest.raises(PlanError, match="does not take MES tickets"):
        agent.ingest(MesTicket(ticket_no="M1", station="s1", unit_sn="u1", requested_by="pat"))
    with pytest.raises(PlanError, match="arrived without its content"):
        agent.ingest(Upload(filename="plan.md", uploaded_by="pat"))
    job = agent.ingest(Upload(filename="plan.md", uploaded_by="pat", content=PLAN))
    assert job.agent == "coding" and job.title == "Fan controller"
    assert job.target is not None and job.target.ref == "sandbox:fan-controller"
    with pytest.raises(BreakdownNotApprovedError) as raised:
        agent.plan(job)
    assert raised.value.message.what_to_do.endswith("press Start task.")

    breakdown = agent.propose(job)
    agent.approve(
        job.id,
        breakdown.model_copy(
            update={"languages": [LanguageChoice(language="python", version="3.11")]}
        ),
    )
    plan = agent.plan(job)
    assert [s.primitive for s in plan.steps] == [
        "toolchain",
        "sandbox_open",
        "iterate",
        "iterate",
        "commit",
        "export_zip",
        "cross_check",
    ]
    assert plan.steps[0].title == "Toolchain: Python 3.11.10."
    assert plan.steps[0].args["resolutions"][0]["requested"] == "3.11"
    assert plan.steps[1].args["image"] == "local/slas/sandbox-python:3.11.10"
    assert plan.summary.startswith(
        "The agent will work in an isolated sandbox with Python 3.11.10, do 2 tasks"
    )
    assert plan.steps[2].args["checks"][0] == {
        "kind": "lint",
        "argv": ["slas-check", "lint"],
        "description": "ruff check .",
    }
    assert agent.sop_template().kind == "code_walkthrough"


# --- edits and snapshots -----------------------------------------------------------------------


def test_apply_edits_stays_inside_the_project(tmp_path: Path) -> None:
    changed = apply_edits(tmp_path, EditSet(files={"src/a.py": "x = 1\n", "b.txt": "b\n"}))
    assert changed == ["src/a.py", "b.txt"] and (tmp_path / "src" / "a.py").read_text() == "x = 1\n"
    assert apply_edits(tmp_path, EditSet(files={"src/a.py": "x = 1\n"})) == [], (
        "unchanged content is not a change"
    )
    assert apply_edits(tmp_path, EditSet(files={"b.txt": None, "missing.txt": None})) == ["b.txt"]
    for bad in ("../escape.py", "/etc/passwd", ".git/config", "src/../../x"):
        with pytest.raises(EditError):
            apply_edits(tmp_path, EditSet(files={bad: "x"}))
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref\n")
    (tmp_path / "big.bin").write_bytes(b"\x00" * 10)
    assert set(snapshot(tmp_path)) == {"src/a.py"}, ".git and binaries are left out"


def test_targeted_replacements_change_one_place_and_name_what_did_not_apply(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("def a():\n    return 1\n\ndef b():\n    return 1\n")
    edits = EditSet(
        replacements=[
            Replacement(path="app.py", find="return 1", replace="return 9"),  # twice: refused
            Replacement(
                path="app.py", find="def a():\n    return 1", replace="def a():\n    return 2"
            ),
            Replacement(path="app.py", find="def c():", replace=""),
            Replacement(path="gone.py", find="x", replace="y"),
        ]
    )
    changed, problems = apply_replacements(tmp_path, edits)
    assert changed == ["app.py"]
    assert (tmp_path / "app.py").read_text() == "def a():\n    return 2\n\ndef b():\n    return 1\n"
    assert problems[0].startswith("In app.py the text to replace occurs 2 times")
    assert problems[1].startswith("In app.py the text to replace does not occur")
    assert problems[2] == "gone.py does not exist; write it as a whole file instead."
    with pytest.raises(EditError):
        apply_replacements(
            tmp_path, EditSet(replacements=[Replacement(path="../x.py", find="a", replace="b")])
        )


def test_the_snapshot_reads_the_files_that_matter_first(tmp_path: Path) -> None:
    for i in range(70):
        (tmp_path / f"a{i:02}.py").write_text("x\n")
    (tmp_path / "zz_named.py").write_text("named\n")
    files = snapshot(tmp_path, first=["zz_named.py"])
    assert next(iter(files)) == "zz_named.py" and len(files) == 60
    assert "zz_named.py" not in snapshot(tmp_path), "without priority the bound cuts it"


def test_the_prompt_lists_the_tree_and_keeps_the_head_of_long_check_output() -> None:
    long = "error: first problem\n" + "x" * 10_000 + "\n1 failed"
    request = EditRequest(
        task=TaskItem(n=1, title="Fix it"),
        iteration=2,
        languages=["python"],
        files={"app.py": "x = 1\n"},
        failures=[CheckResult(kind="test", description="tests", exit_code=1, output=long)],
        paths=["app.py", "tests/test_app.py"],
    )
    system, user = build_messages(request)
    assert "`replacements`" in system.content and "`approach`" in system.content
    assert "Project tree:\napp.py\ntests/test_app.py\n" in user.content
    assert "error: first problem" in user.content and "1 failed" in user.content
    assert "[… output shortened …]" in user.content
    shown = user.content.split("[test] tests (exit 1)\n", 1)[1].split("\n\nProject tree", 1)[0]
    assert len(shown) <= MAX_CHECK_OUTPUT_CHARS + 30


def test_a_task_line_names_the_files_it_must_leave_behind(tmp_path: Path) -> None:
    """T-coding-0018 passed every check without __main__.py and examples/."""
    assert files_named(
        "Write report.py with the text and JSON reports, plus cli.py and __main__.py for the "
        "options and the exit codes"
    ) == ["report.py", "cli.py", "__main__.py"]
    assert files_named("Add examples/baseline.json and examples/after.json, and a README.md.") == [
        "examples/baseline.json",
        "examples/after.json",
        "README.md",
    ]
    assert files_named("Use Python 3.12 and keep `fan_ctl.py` small") == ["fan_ctl.py"]
    assert files_named("Add pytest tests under tests/ for every case") == []

    (tmp_path / "invdiff").mkdir()
    (tmp_path / "invdiff" / "cli.py").write_text("x = 1\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "__main__.py").write_text("")
    names = ["cli.py", "__main__.py", "examples/after.json"]
    assert missing_files(tmp_path, names) == ["__main__.py", "examples/after.json"]
    (tmp_path / "invdiff" / "__main__.py").write_text("")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "after.json").write_text("{}")
    assert missing_files(tmp_path, names) == []


@needs_git
def test_a_task_is_not_done_while_a_file_it_names_is_missing(tmp_path: Path) -> None:
    coder = FakeCoder(EditSet(files={"fan_ctl.py": "def parse(t):\n    return t\n"}))
    harness = Harness(
        tmp_path, coder=coder, checker=FakeCrossChecker(votes("approve"), agreed=True)
    )
    named = TaskItem(
        n=1, title="Write fan_ctl.py and __main__.py", files=["fan_ctl.py", "__main__.py"]
    )
    ticket = harness.kernel.store.load(harness.start(tasks=[named]))
    task = ticket.steps[2]
    assert task.status == "failed" and task.verdict is not None
    assert "files failed" in task.verdict.sentence
    told = coder.requests[-1].failures[-1].output
    assert told == "The task names files that do not exist yet: __main__.py. Write them."


def test_the_executable_bit_follows_the_shebang(tmp_path: Path) -> None:
    """ruff EXE001 failed three rounds: the model writes contents and can never set a mode."""
    script = tmp_path / "templog.py"
    script.write_text("#!/usr/bin/env python3\nprint(1)\n", encoding="utf-8")
    script.chmod(0o644)
    module = tmp_path / "lib.py"
    module.write_text("x = 1\n", encoding="utf-8")
    module.chmod(0o755)
    shell = tmp_path / "run.sh"
    shell.write_text("echo hi\n", encoding="utf-8")
    shell.chmod(0o755)
    cache = tmp_path / ".ruff_cache" / "x.py"
    cache.parent.mkdir()
    cache.write_text("#!/bin/sh\n", encoding="utf-8")
    cache.chmod(0o644)

    assert fix_script_modes(tmp_path) == ["lib.py", "templog.py"]
    assert script.stat().st_mode & 0o777 == 0o755
    assert module.stat().st_mode & 0o777 == 0o644
    assert shell.stat().st_mode & 0o777 == 0o755, "only .py files lose an unexplained x bit"
    assert cache.stat().st_mode & 0o777 == 0o644, "tool caches are not the project"
    assert fix_script_modes(tmp_path) == [], "a second pass changes nothing"
    assert ".ruff_cache/x.py" not in snapshot(tmp_path)


def test_zip_export_is_reproducible_and_skips_git(tmp_path: Path) -> None:
    project = tmp_path / "p"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "x").write_text("no")
    (project / "a.py").write_text("print(1)\n")
    first = zip_project(project, tmp_path / "out" / "p.zip")
    second = zip_project(project, tmp_path / "out2" / "p.zip")
    assert first.sha256 == second.sha256 and first.kind == "zip"
    with zipfile.ZipFile(first.path) as archive:
        assert archive.namelist() == ["a.py"]
        assert archive.getinfo("a.py").external_attr >> 16 == 0o644

    script = project / "run.py"
    script.write_text("#!/usr/bin/env python3\nprint(1)\n")
    script.chmod(0o755)
    third = zip_project(project, tmp_path / "out3" / "p.zip")
    with zipfile.ZipFile(third.path) as archive:
        assert archive.getinfo("run.py").external_attr >> 16 == 0o755, "unzips runnable"
        assert archive.getinfo("a.py").external_attr >> 16 == 0o644


# --- end to end through the kernel -------------------------------------------------------------


class Harness:
    def __init__(
        self, tmp_path: Path, *, coder: FakeCoder, checker: FakeCrossChecker | None
    ) -> None:
        self.runtime = FakeSandboxRuntime()
        self.clock = FakeClock(datetime(2026, 9, 14, 8, tzinfo=UTC))
        self.manager = SandboxManager(
            runtime=self.runtime, data_root=tmp_path, clock=self.clock, runsc_available=True
        )
        self.agent = CodingAgent(now=self.clock.now)
        self.executor = CodingExecutor(
            manager=self.manager,
            coder=coder,
            cross_checker=checker,
            git_access=lambda session: (LocalGitExec(), session.project_dir),
            display_names={"pat": "Pat Lin"},
        )
        self.kernel = Kernel(
            data_root=tmp_path,
            agent=self.agent,
            executor=self.executor,
            store=FileTicketStore(tmp_path),
            clock=self.clock,
        )
        # The fake sandbox "runs" the checks by looking at the project on disk: tests pass
        # once fan_ctl.py mentions `def parse`.
        self.runtime.handle_with(self._check)

    def _check(self, argv: Sequence[str], cwd: str) -> ExecResult | None:
        if list(argv[:1]) != ["slas-check"]:
            return None
        session = next(iter(self.manager._sessions.values()))
        source = Path(session.project_dir) / "fan_ctl.py"
        if argv[1] == "test":
            if source.exists() and "def parse" in source.read_text():
                return ExecResult(exit_code=0, stdout="1 passed")
            return ExecResult(exit_code=1, stderr="FAILED test_parse - AttributeError: parse")
        return ExecResult(exit_code=0, stdout=f"{argv[1]} ok")

    def start(
        self,
        *,
        languages: list[LanguageChoice] | None = None,
        cross_check: bool = True,
        tasks: list[TaskItem] | None = None,
    ) -> str:
        job = self.agent.ingest(Upload(filename="plan.md", uploaded_by="pat", content=PLAN))
        breakdown = self.agent.propose(job)
        approved = Breakdown(
            title=breakdown.title,
            tasks=tasks
            or [
                TaskItem(
                    n=1,
                    title="Parse config.yaml into a dataclass in fan_ctl.py",
                    files=["fan_ctl.py"],
                )
            ],
            languages=languages or [LanguageChoice(language="python")],
            cross_check=cross_check,
            max_iterations=6,
        )
        self.agent.approve(job.id, approved)
        return self.kernel.run(Upload(filename="plan.md", uploaded_by="pat", content=PLAN)).id


@needs_git
def test_the_agent_commits_passing_code_on_a_branch_with_trailers_and_exports(
    tmp_path: Path,
) -> None:
    coder = FakeCoder(
        EditSet(files={"fan_ctl.py": "class Config: ...\n"}, note="first try"),
        EditSet(
            files={
                "fan_ctl.py": (
                    "class Config: ...\n\ndef parse(text: str) -> Config:\n    return Config()\n"
                )
            }
        ),
    )
    checker = FakeCrossChecker(votes("approve", "approve", "concern"), agreed=True)
    harness = Harness(tmp_path, coder=coder, checker=checker)
    ticket_id = harness.start(
        languages=[
            LanguageChoice(language="python"),
            LanguageChoice(language="rust", version="1.99"),
        ]
    )
    ticket = harness.kernel.store.load(ticket_id)

    assert ticket.state is TicketState.DONE
    # The toolchain choice is on the ticket: plan summary, first step, first feed line.
    assert ticket.plan is not None
    assert ticket.plan.steps[0].title == (
        "Toolchain: Python 3.12.6 and Rust 1.80.1. Rust 1.99 isn't in the offline toolchain "
        "bundle, so the newest bundled 1.80.1 is used instead."
    )
    assert (
        ticket.steps[0].verdict is not None
        and ticket.steps[0].verdict.sentence == ticket.plan.steps[0].title
    )
    assert "Rust 1.99 isn't in the offline toolchain bundle" in ticket.plan.summary
    # The sandbox: gVisor, no network; the checks ran inside it as argv.
    assert ticket.steps[1].verdict is not None and ticket.steps[1].verdict.sentence.startswith(
        "The sandbox runs under gVisor."
    )
    assert (
        harness.runtime.created[0].runtime == "runsc"
        and harness.runtime.created[0].network == "none"
    )
    assert ("slas-check", "test") in {e[1] for e in harness.runtime.execs}
    # Iterate: two iterations, the second passed; the coder saw the failure in between.
    task = ticket.steps[2]
    assert task.status == "done" and task.verdict is not None
    assert (
        task.verdict.sentence
        == "Parse config.yaml into a dataclass in fan_ctl.py: done after 2 iterations; "
        "lint ok, type ok, build ok, test ok."
    )
    assert (
        coder.requests[1].failures[0].kind == "test"
        and "AttributeError" in coder.requests[1].failures[0].output
    )
    assert coder.requests[1].files["fan_ctl.py"] == "class Config: ...\n"
    # Commit on the agent's branch with trailers, in Projects/<slug>/.git.
    project = tmp_path / "Coding" / "pat" / "Projects" / "fan-controller"
    ws = GitWorkspace(LocalGitExec(), cwd=str(project))
    assert ws.current_branch() == f"slas/{ticket_id}"
    (commit,) = ws.log()
    assert commit.trailers == {"Slas-Agent": "coding", "Slas-Ticket": ticket_id}
    assert commit.subject == "Fan controller"
    assert ws.remotes() == []
    assert ticket.steps[3].verdict is not None and ticket.steps[3].verdict.sentence.startswith(
        f"Committed {commit.sha[:10]} on branch slas/{ticket_id}"
    )
    # ZIP export on the ticket, without .git.
    (export,) = ticket.exports
    assert export.kind == "zip" and Path(export.path).name == "fan-controller.zip"
    assert Path(export.path).parent == tmp_path / "Coding" / "pat" / "Artifacts" / ticket_id
    with zipfile.ZipFile(export.path) as archive:
        assert archive.namelist() == ["fan_ctl.py"]
    # Three voters on the final diff; the concern is surfaced, the votes are on the ticket.
    assert [v.voter for v in ticket.votes] == ["voter-1", "voter-2", "voter-3"]
    assert checker.calls[0][0] == "code_change" and "+def parse" in checker.calls[0][1][2]
    assert ticket.steps[5].verdict is not None and ticket.steps[5].verdict.sentence.startswith(
        "2 of 3 agree"
    )
    # The walkthrough SOP exists in both languages and carries the commit and the check results.
    assert ticket.sop is not None
    en = Path(ticket.sop.en).read_text(encoding="utf-8")
    zh = Path(ticket.sop.zh).read_text(encoding="utf-8")
    assert f"slas/{ticket_id}" in en and f"slas/{ticket_id}" in zh
    assert "Walk through what the Coding Agent changed" in en
    # The journal's first feed line after the state changes is the toolchain choice.
    entries = [
        json.loads(line)
        for line in (tmp_path / "Tickets" / ticket_id / "journal.jsonl").read_text().splitlines()
    ]
    first_observation = next(e for e in entries if e["kind"] == "observation")
    assert first_observation["step_id"] == "toolchain"
    assert first_observation["payload"]["summary"].startswith(
        "Toolchain: Python 3.12.6 and Rust 1.80.1."
    )


@needs_git
def test_stall_detection_stops_after_three_iterations_without_progress(tmp_path: Path) -> None:
    coder = FakeCoder(EditSet(files={"fan_ctl.py": "class Config: ...\n"}))  # then nothing
    harness = Harness(
        tmp_path, coder=coder, checker=FakeCrossChecker(votes("approve"), agreed=True)
    )
    ticket = harness.kernel.store.load(harness.start())
    assert ticket.state is TicketState.NEEDS_REVIEW
    task = ticket.steps[2]
    assert task.status == "failed" and task.verdict is not None
    assert task.verdict.sentence == (
        "Parse config.yaml into a dataclass in fan_ctl.py: stopped after 4 iterations because the "
        "last 3 made no progress (test failed). A person needs to look at it."
    )
    assert len(coder.requests) == 4, "one edit, then three empty proposals → stop"
    assert ticket.steps[3].status == "pending", "nothing was committed"
    assert ticket.exports == [] and ticket.votes == []
    assert ticket.rca is not None and ticket.rca.uncertain


@needs_git
@needs_git
def test_every_coding_round_carries_the_whole_plan(tmp_path: Path) -> None:
    """A task line alone ("plan", "coding") told the model too little to write anything."""
    coder = FakeCoder(EditSet(files={"fan_ctl.py": "def parse(t):\n    return t\n"}))
    harness = Harness(
        tmp_path, coder=coder, checker=FakeCrossChecker(votes("approve"), agreed=True)
    )
    harness.start()
    assert coder.requests and all(r.plan == PLAN for r in coder.requests)
    prompt = build_messages(coder.requests[0])[1].content
    assert "The plan:\n# Fan controller" in prompt and "Add a `pytest` test" in prompt


@needs_git
def test_an_empty_project_is_never_done_even_when_every_check_passes(tmp_path: Path) -> None:
    coder = FakeCoder()  # the model proposes nothing at all
    harness = Harness(
        tmp_path, coder=coder, checker=FakeCrossChecker(votes("approve"), agreed=True)
    )
    ticket = harness.kernel.store.load(harness.start())
    assert ticket.state is TicketState.NEEDS_REVIEW
    task = ticket.steps[2]
    assert task.status == "failed" and task.verdict is not None
    assert "files failed" in task.verdict.sentence
    # The task names fan_ctl.py, so the agent says which file is missing.
    assert "do not exist yet: fan_ctl.py" in coder.requests[-1].failures[-1].output
    assert ticket.steps[3].status == "pending", "nothing was committed"


@needs_git
def test_tool_caches_are_never_committed(tmp_path: Path) -> None:
    """The reviewer objected to committed __pycache__ files (T-coding-0015)."""
    coder = FakeCoder(
        EditSet(
            files={
                "fan_ctl.py": "def parse(t):\n    return t\n",
                "__pycache__/fan_ctl.cpython-312.pyc": "cache",
                ".pytest_cache/v/cache/lastfailed": "{}",
            }
        )
    )
    harness = Harness(
        tmp_path, coder=coder, checker=FakeCrossChecker(votes("approve"), agreed=True)
    )
    ticket = harness.kernel.store.load(harness.start())
    project = next(tmp_path.glob("Coding/*/Projects/*"))
    tracked = subprocess.run(
        ["git", "-C", str(project), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert "fan_ctl.py" in tracked
    assert not any(p.startswith(("__pycache__/", ".pytest_cache/")) for p in tracked), tracked
    assert (project / "__pycache__" / "fan_ctl.cpython-312.pyc").is_file(), "left on disk"
    exclude = (project / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "__pycache__/" in exclude and ".mypy_cache/" in exclude
    assert ticket.steps[3].status == "done"


def test_a_disagreeing_panel_sends_the_ticket_to_review_with_the_concerns(tmp_path: Path) -> None:
    coder = FakeCoder(EditSet(files={"fan_ctl.py": "def parse(t):\n    return t\n"}))
    checker = FakeCrossChecker(votes("approve", "reject", "reject"), agreed=False)
    harness = Harness(tmp_path, coder=coder, checker=checker)
    ticket = harness.kernel.store.load(harness.start())
    assert ticket.state is TicketState.NEEDS_REVIEW
    assert ticket.steps[5].status == "failed" and ticket.steps[5].verdict is not None
    assert (
        "The conclusion is marked uncertain" in ticket.steps[5].verdict.sentence
        or "1 of 3" in ticket.steps[5].verdict.sentence
    )
    assert len(ticket.votes) == 3 and len(ticket.exports) == 1, (
        "the ZIP was exported before the check"
    )


@needs_git
def test_without_voters_the_change_needs_the_persons_own_review(tmp_path: Path) -> None:
    coder = FakeCoder(EditSet(files={"fan_ctl.py": "def parse(t):\n    return t\n"}))
    harness = Harness(tmp_path, coder=coder, checker=None)
    ticket = harness.kernel.store.load(harness.start())
    assert ticket.state is TicketState.NEEDS_REVIEW
    assert ticket.steps[5].verdict is not None
    assert ticket.steps[5].verdict.sentence.startswith(
        "Not cross-checked: no voters are configured"
    )


def test_executor_refuses_unknown_primitives_and_steps_out_of_order(tmp_path: Path) -> None:
    harness = Harness(tmp_path, coder=FakeCoder(), checker=None)
    context = ExecutionContext(
        ticket_id="T-coding-0001", job_id="job-1", agent="coding", user="pat"
    )
    with pytest.raises(Exception, match="primitive 'shell'"):
        harness.executor.execute(Step(id="s", n=1, primitive="shell", title="no"), context)
    with pytest.raises(RuntimeError, match="has no open sandbox"):
        harness.executor.execute(Step(id="c", n=1, primitive="commit", title="c"), context)
    assert harness.executor.session_for("T-coding-0001") is None
    assert isinstance(Session, type)


@needs_git
def test_the_feed_says_what_each_iteration_does_while_it_runs(tmp_path: Path) -> None:
    coder = FakeCoder(
        EditSet(
            approach=["Write the parser", "Return the text unchanged for now"],
            files={"fan_ctl.py": "class Config: ...\n"},
            note="first draft",
        ),
        EditSet(
            replacements=[
                Replacement(
                    path="fan_ctl.py",
                    find="class Config: ...",
                    replace="def parse(t):\n    return t",
                )
            ],
            note="added parse",
        ),
    )
    harness = Harness(
        tmp_path, coder=coder, checker=FakeCrossChecker(votes("approve"), agreed=True)
    )
    lines: list[tuple[str, str]] = []
    harness.executor.progress = lambda ticket_id, line: lines.append((ticket_id, line))
    ticket = harness.kernel.store.load(harness.start())
    said = [line for _, line in lines]
    assert said[0] == "Iteration 1 of 6: reading the project and writing the code…"
    assert said[1] == "Approach: Write the parser · Return the text unchanged for now"
    assert said[2] == "Changed fan_ctl.py: first draft Running the checks…"
    assert said[3].startswith("Iteration 1: test failed — test: FAILED test_parse")
    assert said[4] == "Iteration 2 of 6: fixing test…"
    assert said[5] == "Changed fan_ctl.py: added parse Running the checks…"
    assert {ticket_id for ticket_id, _ in lines} == {ticket.id}
    assert ticket.steps[2].status == "done"
    assert coder.requests[1].paths == ["fan_ctl.py"]
