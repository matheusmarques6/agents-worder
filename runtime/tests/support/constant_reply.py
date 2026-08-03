"""The E1 constant reply, as an explicit `AGENTS_RESPONDER` factory.

The engine scenarios answer with a constant on purpose: while the reply is
fixed, every difference they observe belongs to the engine. That was never in
question — what changed is that they used to get the constant by INHERITING a
production default, and the production default is exactly what let a runtime
with no `AGENTS_RESPONDER` answer real customers without passing Judge 1.

So the harness names this the way it already names the fake channel. Same
scenarios, same behaviour, one fewer way for production to reach the constant.

`fixed_responder` cannot be pointed at directly: the factory contract is
`callable(dsn)` and its first parameter is the reply TEXT, so a spec of
`...:fixed_responder` would make every scenario answer with the database URL.
"""

from agents_runtime.agent_core.responder import Responder, fixed_responder


def create_responder(dsn: str) -> Responder:
    return fixed_responder()
