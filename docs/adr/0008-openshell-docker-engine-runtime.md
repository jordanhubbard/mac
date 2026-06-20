# ADR 0008 - OpenShell uses Docker Engine/Moby as its only container runtime

- Status: **Accepted**
- Date: 2026-06-20
- Decision owner: `<user>`

## Context

OpenShell can run sandboxes through more than one container driver, and MAC had
started to encode that as host-specific behavior: some fleet nodes used Podman,
others used Docker. That split creates different image stores, gateway configs,
GPU/CDI behavior, service dependencies, and debugging paths. It also caused a
real validation failure: an image that existed in one runtime path was stale or
unusable in another.

The runtime must be the same on bare metal, VMs, and containerized deployments
where nested Docker is supported. It also must avoid Docker Desktop licensing or
desktop-runtime behavior.

## Decision

MAC/OpenShell standardizes on **Docker Engine/Moby** through OpenShell's Docker
driver.

Production fleet nodes must not use Docker Desktop, Podman, or `podman-docker`
for OpenShell execution. The `docker` CLI must point at a real Docker
Engine/Moby daemon. The OpenShell gateway config must advertise only:

```toml
compute_drivers = ["docker"]

[openshell.drivers.docker]
default_image = "localhost/mac-hermes:net"
```

The bootstrap script owns this contract:

- install/start the OSS Docker Engine/Moby daemon on apt-based Linux hosts when
  missing;
- reject `OSH_DRIVER=podman` or any `docker` command that is actually Podman
  emulation;
- build `localhost/mac-hermes:net` with the same Docker daemon the gateway uses;
- write only the Docker driver section in `~/.mac/openshell/gateway.toml`.

## Consequences

The fleet has one image store, one OpenShell driver path, and one debugging
model. Containerized environments use the same path by providing a nested Docker
daemon or mounted Docker socket. Non-Linux developer machines must validate in a
Linux VM/container with Docker Engine/Moby; Docker Desktop is not a production
dependency.

Podman can remain installed on a host for unrelated work, but it is not part of
MAC/OpenShell runtime selection and must not be exposed as `docker` via
`podman-docker`.
