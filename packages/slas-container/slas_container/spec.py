"""`CreateSpec`: a container as this project describes one, and its Engine API body.

The two engines differ in how a GPU is attached: Docker uses `HostConfig.DeviceRequests`
with the nvidia driver (the NVIDIA Container Toolkit configured as a Docker runtime);
Podman uses CDI device names in `HostConfig.Devices` (`nvidia-ctk cdi generate`). Everything
else is the same compat body.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, model_validator

from slas_schemas.common import SlasModel

Engine = Literal["docker", "podman"]
Restart = Literal["no", "unless-stopped", "on-failure"]

_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Mount(SlasModel):
    source: str = Field(min_length=1, description="Host path (or a named volume)")
    target: str = Field(pattern=r"^/")
    read_only: bool = False

    def bind(self) -> str:
        return f"{self.source}:{self.target}:{'ro' if self.read_only else 'rw'}"


class CreateSpec(SlasModel):
    name: str = Field(pattern=_NAME.pattern)
    image: str = Field(min_length=1)
    argv: list[str] = Field(default_factory=list, description="Cmd; argv only, never a shell line")
    entrypoint: list[str] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    #: "none", or the name of an existing network (compose names it `<project>_<network>`).
    network: str = Field(default="none", min_length=1)
    network_aliases: list[str] = Field(default_factory=list)
    mounts: list[Mount] = Field(default_factory=list)
    #: target path → mount options (e.g. "rw,nosuid,nodev,noexec,size=512m").
    tmpfs: dict[str, str] = Field(default_factory=dict)
    runtime: str | None = Field(default=None, description="runsc, kata-fc, runc, nvidia…")
    user: str | None = None
    workdir: str | None = None
    read_only_rootfs: bool = False
    cap_drop_all: bool = True
    no_new_privileges: bool = True
    security_opt: list[str] = Field(default_factory=list)
    pids_limit: int | None = Field(default=None, ge=1)
    memory_bytes: int | None = Field(default=None, ge=1)
    nano_cpus: int | None = Field(default=None, ge=1)
    shm_size_bytes: int | None = Field(default=None, ge=1)
    ipc_host: bool = False
    gpu_ids: list[int] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    restart: Restart = "no"
    stop_timeout_s: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _keys_are_plain(self) -> CreateSpec:
        bad = [key for key in self.env if not _ENV_KEY.match(key)]
        if bad:
            raise ValueError(f"environment names must be identifiers, not {', '.join(bad)}")
        return self

    # --- body -------------------------------------------------------------------------

    def to_body(self, engine: Engine) -> dict[str, Any]:
        host: dict[str, Any] = {
            "Binds": [mount.bind() for mount in self.mounts],
            "NetworkMode": self.network,
            "RestartPolicy": {"Name": self.restart},
            "SecurityOpt": list(self.security_opt),
        }
        if self.no_new_privileges:
            host["SecurityOpt"].append("no-new-privileges")
        if self.cap_drop_all:
            host["CapDrop"] = ["ALL"]
        if self.read_only_rootfs:
            host["ReadonlyRootfs"] = True
        if self.tmpfs:
            host["Tmpfs"] = dict(self.tmpfs)
        if self.runtime is not None:
            host["Runtime"] = self.runtime
        if self.pids_limit is not None:
            host["PidsLimit"] = self.pids_limit
        if self.memory_bytes is not None:
            host["Memory"] = self.memory_bytes
        if self.nano_cpus is not None:
            host["NanoCpus"] = self.nano_cpus
        if self.shm_size_bytes is not None:
            host["ShmSize"] = self.shm_size_bytes
        if self.ipc_host:
            host["IpcMode"] = "host"
        if self.gpu_ids:
            ids = [str(i) for i in self.gpu_ids]
            if engine == "docker":
                host["DeviceRequests"] = [
                    {"Driver": "nvidia", "DeviceIDs": ids, "Capabilities": [["gpu"]]}
                ]
            else:
                host["Devices"] = [
                    {
                        "PathOnHost": f"nvidia.com/gpu={i}",
                        "PathInContainer": "",
                        "CgroupPermissions": "",
                    }
                    for i in ids
                ]
        body: dict[str, Any] = {
            "Image": self.image,
            "Cmd": list(self.argv),
            "Env": [f"{key}={value}" for key, value in self.env.items()],
            "Labels": dict(self.labels),
            "HostConfig": host,
            "StopTimeout": self.stop_timeout_s,
        }
        if self.entrypoint is not None:
            body["Entrypoint"] = list(self.entrypoint)
        if self.user is not None:
            body["User"] = self.user
        if self.workdir is not None:
            body["WorkingDir"] = self.workdir
        if self.network != "none" and self.network_aliases:
            body["NetworkingConfig"] = {
                "EndpointsConfig": {self.network: {"Aliases": list(self.network_aliases)}}
            }
        return body
