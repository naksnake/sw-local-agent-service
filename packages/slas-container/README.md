# slas-container

One client for the container runtime socket the compose file mounts into `model-manager`
and `sandbox-manager` (`SLAS_RUNTIME_SOCKET`, default `/run/podman/podman.sock`; a Docker
host sets it to `/var/run/docker.sock`). It speaks the Docker Engine API, which Podman
serves as its compat API, so one code path starts vLLM instances and sandboxes on either.

| Module | What it gives |
|---|---|
| `slas_container.spec` | `CreateSpec`: the container to create, as this project describes one (argv, env, mounts, tmpfs, runtime, user, caps, limits, GPU ids, IPC, shm) and `to_body(engine)`: the Engine API request body. GPUs become a `DeviceRequests` entry for the nvidia driver on Docker and CDI device names (`nvidia.com/gpu=N`) on Podman. |
| `slas_container.api` | `ContainerApi(socket_path)`: `ping`, `list`, `inspect`, `create`, `start`, `stop`, `remove`, `exec` (argv only, demultiplexed stdout/stderr, exit code), `logs`, `image_present`. Every failure is a three-part `ContainerError`. |
| `slas_container.fake` | `FakeContainerApi`: the same surface in memory for tests, with scripted `exec` results. |

No `podman`/`docker` binary is needed inside the service images; the socket is the only
thing that crosses the container boundary, and a sandbox never sees it (INV-4, `spec.py` of
the sandbox manager refuses the mount).
