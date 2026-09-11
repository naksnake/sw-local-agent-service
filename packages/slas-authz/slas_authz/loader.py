"""Loading `config/rbac-roles.yaml` and re-reading it when it changes (INV-9, ADR-0005).

The api mounts `${SLAS_DATA_ROOT}/config` read-only as a directory, so an edit on the host
lands as a new inode or a new mtime; `RolesLoader.current()` notices either and returns the
new configuration. A file that fails validation is reported and the last good configuration
stays in force, so a typo never locks everyone out.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from slas_schemas.errors import ThreePartError

from slas_authz.roles import RolesConfig


class RolesFileError(Exception):
    """The roles file is missing or invalid; `.error` says what to do."""

    def __init__(self, error: ThreePartError) -> None:
        super().__init__(error.what_happened)
        self.error = error


def parse_roles(data: object, *, source: str = "config/rbac-roles.yaml") -> RolesConfig:
    if not isinstance(data, Mapping):
        raise RolesFileError(
            ThreePartError(
                what_happened=f"{source} is not a mapping of version, capabilities and roles.",
                likely_cause="The file is empty or its top level is a list or a plain value.",
                what_to_do="Start from the shipped config/rbac-roles.yaml and edit the roles.",
            )
        )
    try:
        return RolesConfig.model_validate(dict(data))
    except ValidationError as exc:
        details = exc.errors()
        where = ".".join(str(p) for p in details[0]["loc"]) if details else ""
        where = where or "the file"
        message = str(details[0]["msg"]) if details else "invalid content"
        raise RolesFileError(
            ThreePartError(
                what_happened=f"{source} is not valid at {where}: {message}",
                likely_cause="A role names a capability that is not in the catalogue, or a "
                "name has the wrong shape.",
                what_to_do="Fix the entry and save; the api re-reads the file without a restart.",
            )
        ) from exc


def load_roles_text(text: str, *, source: str = "config/rbac-roles.yaml") -> RolesConfig:
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RolesFileError(
            ThreePartError(
                what_happened=f"{source} could not be read as YAML.",
                likely_cause=f"A syntax error: {str(exc).splitlines()[0]}",
                what_to_do="Fix the indentation or quoting and save the file.",
            )
        ) from exc
    return parse_roles(data, source=source)


def load_roles_file(path: Path) -> RolesConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RolesFileError(
            ThreePartError(
                what_happened=f"The roles file {path} does not exist.",
                likely_cause="The installation's config directory was not copied or mounted.",
                what_to_do="Run ./install.sh again, or restore config/rbac-roles.yaml from "
                "the bundle.",
            )
        ) from exc
    return load_roles_text(text, source=str(path))


@dataclass(frozen=True)
class _Stamp:
    inode: int
    mtime_ns: int
    size: int


def _stamp(path: Path) -> _Stamp | None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return _Stamp(st.st_ino, st.st_mtime_ns, st.st_size)


class RolesLoader:
    """Serves the current roles configuration, re-reading the file when it changes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stamp: _Stamp | None = None
        self._config: RolesConfig | None = None
        self.last_error: ThreePartError | None = None

    @property
    def path(self) -> Path:
        return self._path

    def current(self) -> RolesConfig:
        """The latest valid configuration. Raises RolesFileError only when none was ever valid."""
        stamp = _stamp(self._path)
        if self._config is None or stamp != self._stamp:
            try:
                self._config = load_roles_file(self._path)
                self._stamp = stamp
                self.last_error = None
            except RolesFileError as exc:
                self.last_error = exc.error
                if self._config is None:
                    raise
        return self._config
