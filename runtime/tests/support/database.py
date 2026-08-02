"""Where the test database lives.

One constant, imported by every level that talks to Postgres, so the `db`,
`rls` and `pipeline` suites can never drift into pointing at different
databases — which would show up as a passing suite that tested nothing.
"""

import os

# The port the Supabase CLI binds locally (supabase/config.toml [db].port).
DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"


def dsn_from_env() -> str:
    """The DSN of the database under test — CI overrides it, locally it is the CLI's."""
    return os.environ.get("SUPABASE_DB_URL", DEFAULT_DSN)
