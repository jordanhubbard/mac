# Connect precis-mcp to PostgreSQL

`precis-mcp` needs PostgreSQL with the `pgvector` extension. A fleet node can
use either a shared precis database or a local database. Do not reuse the MAC
control-plane database: precis owns and migrates a separate schema.

## Shared instance

No public precis endpoint or credentials are published by MAC. An operator of
an existing instance must provide all of the following through the fleet secret
delivery mechanism, not in Git, task text, chat logs, or command-line arguments:

- a PostgreSQL DSN for a dedicated precis database and role;
- network authorization from the consuming fleet node;
- TLS requirements and the trusted CA, when applicable;
- a web URL and a separately created web account, if the web UI is running.

Set the delivered DSN only in the process environment:

```sh
export PRECIS_DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/precis?sslmode=require'
precis doctor
```

Do not infer the web endpoint from the database host. The web UI is a separate
service and its HTTP Basic credentials are not PostgreSQL credentials.

## Local instance

Install PostgreSQL and pgvector using the platform's normal package mechanism.
Then create an isolated role and database as a PostgreSQL administrator. The
example prompts for the password so it does not enter shell history:

```sh
read -r -s -p 'precis database password: ' PRECIS_DB_PASSWORD
printf '\n'
psql postgres -v ON_ERROR_STOP=1 \
  -v password="$PRECIS_DB_PASSWORD" <<'SQL'
CREATE ROLE precis LOGIN PASSWORD :'password';
CREATE DATABASE precis OWNER precis;
SQL
psql -d precis -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION vector;'
```

Use a percent-encoded password in a URL. For a database on the same node:

```sh
export PRECIS_DATABASE_URL='postgresql://precis:PERCENT_ENCODED_PASSWORD@127.0.0.1:5432/precis'
export PRECIS_EMBEDDER=mock
precis migrate
precis doctor
```

`precis migrate` applies the schema bundled with the installed precis version,
including the `se` kind migrations. Run it after every precis upgrade before
starting an older-schema-refusing daemon. `PRECIS_EMBEDDER=mock` is sufficient
for initial structural-envelope work without downloading an embedding model.

## Optional web UI

The web UI requires the `web` extra. Create its account interactively before
starting it; a new database has no usable web credentials:

```sh
precis users add FLEET_LOGIN --abbrev fl
precis web --host 127.0.0.1 --port 9100
```

Keep the default loopback binding. For fleet access, publish it through the
approved authenticated proxy or private network and record that resulting URL
in the fleet secret/configuration system. Never commit the account password.

## Verify the `se` surface

After migration, verify the database connection and ask the installed build for
its authoritative capability list:

```sh
precis doctor
precis eval "get(kind='skill', id='precis-help')"
precis eval "get(kind='skill', id='precis-se-help')"
```

Both commands must succeed before agents create structural-envelope designs.
If `precis doctor` reports a pending schema, rerun `precis migrate` with the
same `PRECIS_DATABASE_URL`.
