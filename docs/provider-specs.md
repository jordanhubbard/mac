# Provider specs: adding your own capacity provider

mac talks to capacity providers through a **JSON description of a CLI**, not
through provider-specific Python. To add a provider, you write one file in your
own configuration directory. You do not fork or patch mac.

The reasoning is in [ADR 0028](adr/0028-a-provider-is-data-not-source.md); this
page is the how.

> `local` is not a provider spec. It is direct connectivity to a machine you
> already have — `user@machine:directory` over ssh — with no CLI to wrap and no
> lifecycle to drive. It is configured the way it always was.

## Where specs live

Search order, nearest wins:

| # | Location | For |
| --- | --- | --- |
| 1 | `$MAC_PROVIDER_SPEC_PATH` (colon-separated dirs) | operators, tests |
| 2 | `<mac home>/provider-specs/` | **your providers** |
| 3 | the templates shipped inside the mac package | examples to copy |

`<mac home>` is `~/.mac` unless `MAC_HOME` says otherwise.

Dropping `~/.mac/provider-specs/nvidia.json` overrides the shipped `nvidia`
template. You never edit a file mac ships, so upgrades never conflict with your
provider and never silently revert it.

Two rules keep this honest:

- **A spec's `name` must equal its filename stem.** Otherwise a file could
  shadow a provider name that `ls` does not show.
- **One broken file does not hide the directory.** An unparseable spec is
  skipped during discovery; `discover_specs(strict=True)` raises so you can see
  exactly what is wrong.

## Getting started: copy a template

Four templates ship with mac, in `src/mac/data/provider-specs/`:

| Template | Wraps | Has `exec`? |
| --- | --- | --- |
| `nvidia.json` | NVIDIA Horde DGXC via `hgx` | yes |
| `gcp.json` | Google Compute Engine via `gcloud` | yes |
| `aws.json` | Amazon EC2 via `aws` | no — see [attestation](#attestation-is-a-capability) |
| `azure.json` | Azure VMs via `az` | no — see [attestation](#attestation-is-a-capability) |

`nvidia.json` is the one to read first: it is a real, working provider rather
than an illustration. Read `aws.json` next and diff them.

```bash
mkdir -p ~/.mac/provider-specs
cp "$(python3 -c 'import mac.provider_spec as m; print(m.SHIPPED_SPEC_DIR)')/gcp.json" \
   ~/.mac/provider-specs/mycloud.json
# edit it, and remember to change "name" to "mycloud" so it matches the filename
```

## Anatomy of a spec

```json
{
  "schema": "mac.provider_spec.v1",
  "name": "mycloud",
  "kind": "external",
  "description": "What this is and what to edit.",
  "binary": "mycloud",
  "timeout_seconds": 120,
  "env_passthrough": ["MYCLOUD_REGION"],
  "credential_env_var": "MYCLOUD_API_TOKEN",
  "parameters": {
    "instance_id": {"required": true, "pattern": "^vm-[0-9]{4}$"},
    "instance_name": {"pattern": "^[a-z0-9-]{1,32}$", "default": "mac-worker"},
    "flavor": {"required": true, "pattern": "^[a-z0-9-]{1,32}$"},
    "command": {"splat": true, "pattern": "^[^\\u0000\\n\\r]{1,128}$", "default": []}
  },
  "fields": {
    "id": ["uuid"],
    "name": ["label"],
    "flavor": ["size"],
    "state": ["phase"],
    "host": ["ipv4"],
    "user": ["login"]
  },
  "verbs": {
    "create": {
      "args": ["vm-create", "--label", "{instance_name}", "--size", "{flavor}"],
      "parse": {"format": "json", "select": "vm"}
    },
    "list":   {"args": ["vm-list"], "parse": {"format": "json", "select": "vms"}},
    "status": {"args": ["vm-show", "{instance_id}"], "parse": {"format": "json", "select": "vm"}},
    "delete": {"args": ["vm-destroy", "{instance_id}"], "parse": {"format": "none"}},
    "exec":   {"args": ["vm-run", "{instance_id}", "--", "{command...}"], "parse": {"format": "none"}}
  }
}
```

### Verbs

The vocabulary is closed: `create`, `list`, `status`, `update`, `delete`,
`stop`, `start`, `exec`. Anything else is a load error. Define only the verbs
your provider has; asking for one you did not define raises a named capability
error rather than doing nothing.

`args` is the argv **after** the binary. It is a list, always — there is no
shell, so nothing you write is re-split or re-interpreted.

### Parameters and substitution

`{param}` substitutes a declared parameter into a token. `{param...}` — only
ever as a whole token, and only for a parameter marked `"splat": true` —
expands a list into one argv item per element. That is how `exec` passes a
command through.

Every placeholder must name a declared parameter. Every value is checked against
that parameter's `pattern` before it reaches argv.

`required` is judged **per verb**, from the placeholders that verb actually
uses: `image_id` is required to create an instance and meaningless when deleting
one.

### Reading output back

`parse.format` is `json` or `none`. `parse.select` is a dotted path into the
payload (`"vm"`, `"data.instances"`). `fields` maps mac's model onto candidate
source keys, first non-empty wins:

| mac field | meaning |
| --- | --- |
| `id` | **required** — the immutable selector for every lifecycle call |
| `name` | human display label; never used to address an instance |
| `flavor`, `state` | reported as-is |
| `endpoint`, or `host` + `user` + `port` | composed into a validated ssh target |

Most cloud CLIs return deeply nested payloads. Flatten them with the vendor's
own projection flag rather than expecting mac to understand the shape — that is
what `--query` does in `aws.json` and `--format` does in `gcp.json`.

## Security model

A spec decides what runs, so the interpreter treats one as untrusted input.

- **`binary` is a bare command name.** No path, no `..`, no separator. Which
  binary that name resolves to is your `PATH`'s decision, not a downloaded
  file's.
- **argv is never a shell string.** Shell metacharacters in a value are inert
  bytes. The risk that *is* live for `execve` is a value the target tool re-reads
  as a **flag**, so a value starting with `-` is refused unless the parameter
  sets `"allow_leading_dash": true`.
- **Credentials never enter argv.** A spec names environment variables; it can
  never interpolate one. A parameter whose *name* looks like a credential
  (`token`, `api_key`, `password`, …) is rejected at load time.
- **The child environment is only what you declared.** `PATH`, `HOME`, `LANG`,
  `LC_ALL`, `TMPDIR`, plus `env_passthrough` and `credential_env_var`.
- **Secrets in output are scrubbed on the way in.** A `root_password` field in a
  provider record is recorded in `scrubbed_fields` by name; its value is never
  copied into a `ProviderInstance` and so never reaches a log or an evidence
  blob.
- **Everything is bounded.** ≤ 256 KiB per file, ≤ 32 verbs, ≤ 64 argv tokens
  per verb, ≤ 512 chars per token, ≤ 64 parameters, ≤ 1024 chars per value,
  ≤ 64 splat items, timeout 1–3600 s. A spec that cannot be proved within bounds
  does not load.

### Reviewing someone else's spec

Ask it what it would run, without running it:

```python
from mac.provider_spec import SpecProvider, load_spec

provider = SpecProvider(load_spec("mycloud"))
print(provider.build_argv("create", {"instance_name": "w1", "flavor": "medium"}))
# ['mycloud', 'vm-create', '--label', 'w1', '--size', 'medium']
```

`build_argv()` is public API precisely so this is a one-liner rather than an
exercise in reading a template and imagining the substitution.

## Attestation is a capability

`attest(instance_id)` proves a machine is really reachable: it runs
`printf <nonce>` over the provider's own transport and requires the exact line
back. A zero exit from `create` is never treated as readiness.

Attestation needs an `exec` verb. A spec without one **fails closed** —
`attest()` raises `ProviderCapabilityError` instead of returning success. Two
shipped templates are in that position on purpose:

- **EC2** has no synchronous non-interactive remote-exec CLI verb.
- **`az vm run-command invoke`** returns remote output wrapped inside a JSON
  envelope rather than on stdout, so it cannot satisfy a line-exact nonce check.

If you need attested capacity from those providers, add an ssh-based `exec` verb
of your own. An unattested provider is honest about what it does not know; a
provider that reports "ready" because a create call exited zero is not.

## Using a provider

```python
from mac.provider_spec import SpecProvider, discover_specs, load_spec

print(sorted(discover_specs()))          # what is on the search path

provider = SpecProvider(load_spec("mycloud"))
instance = provider.create(instance_name="mac-worker-1", flavor="medium")
provider.attest(instance.instance_id)     # raises unless really reachable
print(instance.observable())              # secret-free, safe for logs/evidence
provider.delete(instance.instance_id)
```

Addressing is by immutable ID everywhere. `resolve_instance_id(name)` maps a
display name to an ID and refuses when the name matches zero or more than one
instance — guessing there means operating on the wrong machine.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `no provider spec named 'x' on the search path: …` | file missing, or its `name` does not match the filename stem |
| `verb 'x' is not one of …` | verb outside the closed vocabulary |
| `verb 'x' references undeclared parameter 'y'` | add `y` to `parameters` |
| `value '…' starts with '-'` | set `allow_leading_dash` if the provider really expects a flag there |
| `does not match its declared pattern` | widen that parameter's `pattern` deliberately |
| `parameter 'x' looks like a credential` | pass it via `env_passthrough` / `credential_env_var` instead |
| `cannot be attested: its spec describes no 'exec' verb` | add an `exec` verb, or accept unattested capacity |
