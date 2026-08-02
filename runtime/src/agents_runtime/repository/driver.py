"""Database driver access — connection pools per role.

Two separate pools (ADR-11): `worker_role` for business tables and
`sender_role` for the outbox. Neither has BYPASSRLS nor owns the protected
tables; both set `app.tenant_id` per unit of work. Cross-tenant work happens
exclusively through SECURITY DEFINER claim functions.

Importing this module from a domain module is a build failure — see the
"only the repository layer reaches the database" contract in pyproject.toml.
"""
