"""The only layer allowed to contain SQL.

Enforced two ways: the import-linter contract forbidding every domain module
from importing `repository.driver`, and the SQL lint that fails when a query
appears outside this package.
"""
