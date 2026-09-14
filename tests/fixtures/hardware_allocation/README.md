# Hardware allocation fixtures

These six directories capture the execution boundaries used by hardware
detection tests: unconfined bare metal, a cgroup v2 CPU quota, cpusets, a
memory limit, one `1g.24gb` MIG slice, and unresolved/unlimited boundaries.
Files retain their kernel or `nvidia-smi` names so allocator and summary tests
can consume the same samples without translating them.

The `cpuset` corpus includes both contiguous and non-contiguous expressions.
The `mig_1g_24gb` sample is exercised with unrestricted visibility and with
`CUDA_VISIBLE_DEVICES=MIG-slice-24gb`. The unknown corpus is also exercised as
an absent cgroup tree and on a non-Linux host.
