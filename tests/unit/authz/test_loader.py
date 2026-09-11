"""RolesLoader re-reads a changed file and keeps the last good configuration on a bad edit."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from slas_authz import RolesFileError, RolesLoader

GOOD = """
version: 1
capabilities:
  users:manage: { description: "People." }
  settings:manage: { description: "Settings." }
  settings:read: { description: "Read settings." }
roles:
  administrator:
    label: Administrator
    description: d
    capabilities: [users:manage, settings:manage, settings:read]
  viewer: { label: {label}, description: d, capabilities: [settings:read] }
"""


def write(path: Path, label: str, mtime_ns: int) -> None:
    path.write_text(GOOD.replace("{label}", label), encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_loader_reads_once_and_reloads_on_change(tmp_path: Path) -> None:
    path = tmp_path / "rbac-roles.yaml"
    write(path, "Viewer", 1_000_000_000)
    loader = RolesLoader(path)
    assert loader.current().label_for("viewer") == "Viewer"
    assert loader.current() is loader.current(), "unchanged file → same object, no re-read"
    write(path, "Viewer (read only)", 2_000_000_000)
    assert loader.current().label_for("viewer") == "Viewer (read only)"
    assert loader.last_error is None


def test_loader_reloads_when_the_file_is_replaced_by_a_new_inode(tmp_path: Path) -> None:
    path = tmp_path / "rbac-roles.yaml"
    write(path, "Viewer", 1_000_000_000)
    loader = RolesLoader(path)
    assert loader.current().label_for("viewer") == "Viewer"
    replacement = tmp_path / "rbac-roles.yaml.new"
    write(replacement, "Watcher", 1_000_000_000)  # same mtime and size class, different inode
    os.replace(replacement, path)
    assert loader.current().label_for("viewer") == "Watcher"


def test_bad_edit_keeps_last_good_and_records_the_error(tmp_path: Path) -> None:
    path = tmp_path / "rbac-roles.yaml"
    write(path, "Viewer", 1_000_000_000)
    loader = RolesLoader(path)
    loader.current()
    path.write_text("version: 1\nroles: [broken\n", encoding="utf-8")
    os.utime(path, ns=(3_000_000_000, 3_000_000_000))
    assert loader.current().label_for("viewer") == "Viewer"
    assert loader.last_error is not None
    assert "could not be read as YAML" in loader.last_error.what_happened
    write(path, "Viewer again", 4_000_000_000)
    assert loader.current().label_for("viewer") == "Viewer again"
    assert loader.last_error is None


def test_missing_file_with_no_good_copy_raises(tmp_path: Path) -> None:
    loader = RolesLoader(tmp_path / "absent.yaml")
    with pytest.raises(RolesFileError) as info:
        loader.current()
    assert "does not exist" in info.value.error.what_happened
    assert "install.sh" in info.value.error.what_to_do
