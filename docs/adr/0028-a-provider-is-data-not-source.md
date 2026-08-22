# ADR 0028: A provider is data, not mac source

- Status: Proposed
- Date: 2026-08-22
- Decision owner: MAC fleet owner
- Related: [ADR 0005](0005-elastic-executor-tier-vs-static-fleet.md) — the
  elastic tier is the thing that needs providers at all;
  [ADR 0015](0015-macos-nodes-are-host-installs.md) — some capacity is not a
  provider API and must not be modelled as one;
  [ADR 0022](0022-a-gate-returns-a-named-decision-not-a-boolean.md) — a
  capability mac does not have must be named, not silently absent

## Context

mac's capacity providers are Python modules today, and one of them is a wrapper
around a CLI that only exists inside one company's network.

Measured in this repository on 2026-08-22:

| file | lines | what it hard-wires |
| --- | --- | --- |
| `src/mac/hgx_provider.py` | 594 | the `hgx` CLI's verbs, flags, JSON shapes |
| `src/mac/hgx_elastic_capacity.py` | 1251 | bounded planning over `HgxSession` |
| `src/mac/hgx_autoscaler.py` | 597 | demand → `hgx` create/retire |

`hgx` is NVIDIA's internal Horde DGXC tool. It is not on PyPI, not on GitHub, and
not installable by anyone outside that network. So mac — a product whose whole
claim is that it coordinates a fleet — currently ships 2,442 lines of provider
integration that **nobody outside one employer can execute**, and offers a user
on any other cloud exactly one way to add their own provider: fork mac and write
a fourth `*_provider.py`.

That is the wrong shape twice over.

**It is wrong for the outside user.** "Which cloud am I on" is a property of the
deployment, not of the software. Every other deployment-shaped fact in mac —
which hub, which fleet, which model router, which node — is already
configuration. Providers are the one that leaked into source.

**It is wrong for us.** The `hgx` wrapper is not clever code. Read it and it is
a table: verb → argv → how to read the output back. `create` is
`["--json", "create", "--type", flavor]`; `status` is
`["--json", "status", id]`; `delete` is `["delete", id]`. The interesting parts
— immutable-ID-only addressing, refusing an ambiguous name, never invoking
`hgx info` because it can echo a bootstrap password, proving reachability with a
nonce rather than an exit code — are **policies that are not specific to
`hgx` at all**. We wrote a generic controller and a vendor table, then compiled
them together and shipped the result as one module per vendor.

### The thing that is genuinely not a provider

`local` is direct connectivity to a machine the operator already has, expressed
as `user@machine:directory` plus ssh. There is no CLI to wrap, no lifecycle to
drive, nothing to create or destroy. Modelling it as a degenerate spec would
mean inventing a fake tool to describe. It stays exactly as it is.

## Decision

**mac core carries an interpreter. A provider is a JSON file.**

`src/mac/provider_spec.py` reads a provider description — which binary, which
argv per CRUD verb, how to map the output back onto mac's provider model — and
executes it. `aws`, `azure`, `gcp` and `nvidia` ship as templates in
`src/mac/data/provider-specs/`. `local` is untouched.

### 1. The vocabulary is CRUD plus a transport verb

`create`, `list`, `status`, `update`, `delete`, `stop`, `start`, `exec`. That is
the whole surface, and it is closed: a verb outside it is a load-time error, not
an extension point. A spec need not define all of them.

The closed list is the point. An open verb namespace would let a spec name
anything, and "anything" is how `hgx info` — the verb that prints a fallback
password — would come back. The current adapter bans that verb by name in
Python. Under the spec vocabulary there is simply no way to express it.

### 2. argv is built, never a shell string

Every invocation is an explicit argv list executed with `shell=False`. There is
no point at which a value is concatenated into a string that a shell later
re-splits.

This changes what "injection" even means here, and it is worth being precise,
because the obvious instinct — ban `;` and `$(` and backticks — defends against
the wrong thing. With `execve` and no shell, shell metacharacters are ordinary
bytes in an argument. The real residual risk is **argument** injection: a value
that begins with `-` and is re-read by the target tool as a flag. A provider ID
of `--output-file=/etc/passwd` is the attack; `; rm -rf /` is not.

So the interpreter refuses a leading `-` by default, and a spec that genuinely
needs one says `"allow_leading_dash": true` on that parameter and thereby
declares it in a file a reviewer reads.

### 3. Every substituted value is declared and shape-checked

A verb's argv template may only reference parameters the spec declares, each
with a regex. An undeclared placeholder fails at load time, so a spec cannot
smuggle an unvalidated value into argv. Values default to a conservative
character class; widening it is an explicit, reviewable edit to the spec.

### 4. Credentials never enter argv

A spec names environment variables (`env_passthrough`, `credential_env_var`); it
can never interpolate one. The child process gets `PATH` and exactly the
variables the spec declared — nothing else — so adding a provider does not
silently widen what a subprocess can read.

The consequence worth stating plainly: a credential cannot leak through the
process table, a command log, or an error that echoes argv, because it was never
in argv. That is stronger than redacting argv after the fact, and it is why the
validator **rejects** a parameter whose name looks like a credential rather than
accepting it and scrubbing later.

### 5. Output parsing is declarative and secret-free on the way out

`parse.select` walks into the payload; `fields` maps mac's model
(`id`, `name`, `flavor`, `state`, `host`, `user`, `port`, `endpoint`) onto
candidate source keys. Model field names are a closed set too.

Provider records routinely carry a `root_password` or a `token`. Those field
*names* are recorded in `scrubbed_fields`; their values are never copied into a
`ProviderInstance`, which is the same contract
`mac.hgx_provider.HgxSession.observable()` already keeps and the same one
`mac.agent_provider.ProviderDecision` keeps.

### 6. Readiness stays a capability, and it fails closed

The nonce attestation is expressible: a provider that describes `exec` can be
attested, because attestation is "run `printf <nonce>` over the transport and
require the exact line back".

A provider that describes no `exec` verb **cannot** be attested, and `attest()`
raises a named capability error rather than returning success. This matters more
than it looks: two of the four shipped templates are in exactly that position.
EC2 has no synchronous non-interactive remote-exec verb, and `az vm run-command
invoke` wraps remote output inside a JSON envelope rather than putting it on
stdout. Both templates therefore ship *without* `exec`, and the honest outcome
is a provider that says "I cannot prove this machine is reachable" — not one
that quietly treats a successful `create` as readiness.

This is [ADR 0022](0022-a-gate-returns-a-named-decision-not-a-boolean.md)
applied to capacity: the absent capability is named.

### 7. Specs live in user config; shipped files are templates

Search order, nearest wins:

1. `$MAC_PROVIDER_SPEC_PATH` (colon-separated) — operators and tests
2. `<mac home>/provider-specs/` — **the user's own providers**
3. the shipped templates inside the installed package

A user overrides a shipped template by dropping a same-named file into their own
directory. They never edit anything mac ships, so an upgrade never conflicts
with their provider and never silently reverts it.

Two rules keep precedence honest. A spec's `name` must equal its filename stem,
so a file cannot shadow a name that `ls` does not show. And one unparseable file
does not hide the rest of the directory — a bad third-party spec must not make
every provider disappear.

### 8. Everything is bounded, because a spec file is an execution surface

Binary must be a bare command name (no path, no `..`, no separator) — *which*
binary a name resolves to is the operator's `PATH` decision, not a downloaded
file's. Spec ≤ 256 KiB, ≤ 32 verbs, ≤ 64 argv tokens per verb, ≤ 512 chars per
token, ≤ 64 parameters, ≤ 1024 chars per value, ≤ 64 splat items, timeout in
1..3600s. A spec that cannot be proved within bounds does not load at all.

**The review story for a third-party spec is `build_argv()`.** It returns the
exact argv a verb would execute without executing it, so "what does this file
actually run" is answerable by a reviewer in one call rather than by reading a
template and imagining the substitution.

### 9. The hgx modules become consumers; the migration is additive first

`ProviderInstance` deliberately mirrors `HgxSession`'s shape, and
`hgx_elastic_capacity` already talks to a `_Provider` **Protocol** rather than to
`HgxProvider` directly. The seam exists.

So the order is: interpreter and `nvidia.json` land first and are proved to
build byte-identical argv to the hard-wired adapter; then the capacity
controller and autoscaler are re-pointed at a spec-driven provider; then
`hgx_provider.py` is deleted. Each step is separately revertible, and at no
point is NVIDIA capacity broken to make external capacity work.

`hgx_provider.py` is **deprecated on landing, not removed** — removing it in the
same change would couple a refactor of 2,442 lines to a new execution surface's
first day.

## Consequences

- mac becomes usable outside NVIDIA. A user with any CLI-driven cloud writes one
  JSON file in their own config and gets create/list/status/exec/delete with
  mac's addressing, redaction and attestation policies applied for free.
- The interesting policies stop being per-vendor. Immutable-ID addressing,
  ambiguous-name refusal, credential scrubbing and nonce attestation are
  implemented once and inherited by every provider, including ones we never see.
- mac gains a **data-driven execution surface**, which is a real new risk and
  the reason §2–§4 and §8 exist rather than being left to reviewer discipline. A
  spec is still code in the sense that matters: it decides what runs.
- A spec cannot express everything a hand-written adapter can. Retries,
  pagination, multi-call verbs and nested-payload gymnastics are all outside the
  vocabulary today. Some providers will need `--query`/`--format` projections to
  flatten their output, which is exactly what the `aws`, `gcp` and `azure`
  templates demonstrate — and some will need a capability interface instead.
  That is the trade for a declarative surface, and it should be met by adding a
  *named capability*, never by adding a general escape hatch.
- Provider bugs move from mac's issue tracker to the user's spec file. That is
  the right place for them and a worse debugging experience, which is why
  `build_argv()` is public API rather than an internal helper.
- We now maintain four vendor templates we cannot integration-test against the
  real clouds. They are labelled TEMPLATE, they say what to edit, and the tests
  assert their *shape* — that they parse, that they can create/list/status/
  delete, that `nvidia` matches the adapter it replaces. Nothing claims they are
  verified against a live account.

## Alternatives considered

**Keep a Python module per provider, just write more of them.** Rejected: it
does not solve the problem for anyone who is not us. A user cannot add a
provider without forking mac, and every new module re-implements the addressing
and redaction policy, differently.

**A plugin entry point — users install a Python package implementing a
Provider protocol.** Not rejected on capability; it is strictly more expressive.
Rejected on the security and operations trade. A plugin is arbitrary code in
mac's process with mac's credentials and no bounds at all, and "add a provider"
becomes "publish and install a package". A JSON file that can only build argv
for one named binary is a surface a reviewer can actually read. If a provider
genuinely needs code, that is a signal for a named capability interface in mac,
not for a general plugin loader.

**Embed a small expression language for output parsing.** Rejected: it grows
until it is a language, and the flattening it would do is already available in
every one of these CLIs as `--query` / `--format`, evaluated by the vendor's own
tool rather than by ours.

**Adopt an existing multi-cloud abstraction (libcloud, Pulumi, Terraform).**
Rejected for this layer. They are strong at declarative *desired state* and
heavy as a dependency; mac needs imperative, bounded, attestable single-machine
lifecycle calls it can run from a controller loop. They also would not cover
`hgx`, which is the one provider we actually have to keep working.

**Ship no templates; make every user author a spec from scratch.** Rejected:
the templates are the documentation. The correct way to learn this vocabulary is
to read `nvidia.json` — a real, working provider — beside `aws.json`, and diff
them.
