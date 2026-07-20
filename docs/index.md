# MAC: trustworthy work across an agent fleet

MAC coordinates projects, tasks, agents, reviews, publications, deployments,
and evidence across a heterogeneous fleet. This documentation is a book: begin
with the shared mental model, operate a local system, grow it into a fleet, and
finish with a complete request-to-production exercise.

## Choose a path

- **New to MAC:** read the book in order, beginning with
  [MAC as a System](book/01-system.md).
- **Fleet operator:** complete the foundations, then continue through fleet
  onboarding, security, operations, deployment, and cutover.
- **Integrator:** complete the foundations, then focus on repository contracts,
  APIs, AgentBus, Hermes, and external integrations.
- **Contributor:** run `make docs-check` before submitting documentation. Every
  published shell block is an executable contract.

The `dev` documentation follows the canonical `main` branch. Release selectors
point to immutable SemVer documentation built from the matching source tag.
