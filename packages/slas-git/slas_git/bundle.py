"""`git bundle` in and out — the air-gap-native way to move history (CLAUDE.md §5.7)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import Field

from slas_git.workspace import GitError, GitWorkspace
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage


class BundleInfo(SlasModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    refs: list[str] = Field(default_factory=list)

    def sentence(self) -> str:
        count = len(self.refs)
        return (
            f"Bundle {Path(self.path).name}: {count} {'ref' if count == 1 else 'refs'}, "
            f"{self.size_bytes:,} bytes, sha256 {self.sha256[:12]}…"
        )


def export_bundle(
    workspace: GitWorkspace, out_path: Path, refs: list[str] | None = None
) -> BundleInfo:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wanted = refs or ["--all"]
    workspace.git("bundle", "create", str(out_path), *wanted)
    listed = workspace.git("bundle", "list-heads", str(out_path)).stdout
    names = [line.split(" ", 1)[1] for line in listed.splitlines() if " " in line]
    data = out_path.read_bytes()
    return BundleInfo(
        path=str(out_path),
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        refs=names,
    )


def verify_bundle(workspace: GitWorkspace, path: Path) -> list[str]:
    """Refs the bundle carries, if the repository can apply it; three-part error otherwise."""
    if not path.is_file():
        raise GitError(
            ThreePartMessage(
                f"{path} is not a file.",
                "A bundle is the .bundle file `git bundle create` wrote on the other site.",
                "Check the path and try again.",
            )
        )
    result = workspace.git("bundle", "verify", str(path), check=False)
    if result.exit_code != 0:
        raise GitError(
            ThreePartMessage(
                f"{path.name} cannot be applied to this repository.",
                result.stderr.strip().splitlines()[-1]
                if result.stderr.strip()
                else "git bundle verify failed.",
                "Export the bundle again from a repository that shares history with this one.",
            )
        )
    listed = workspace.git("bundle", "list-heads", str(path)).stdout
    return [line.split(" ", 1)[1] for line in listed.splitlines() if " " in line]


def import_bundle(workspace: GitWorkspace, path: Path, *, prefix: str = "bundle") -> list[str]:
    """Fetch every branch in the bundle under refs/remotes/<prefix>/; nothing is merged."""
    refs = verify_bundle(workspace, path)
    workspace.git("fetch", "--quiet", str(path), f"+refs/heads/*:refs/remotes/{prefix}/*")
    return [ref.removeprefix("refs/heads/") for ref in refs if ref.startswith("refs/heads/")]
