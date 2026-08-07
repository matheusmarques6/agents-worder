"""`get_order` and `get_tracking` — the two questions a contact actually asks.

These are the first tools the MODEL chooses (E3 S9). Everything the E2 built
holds unchanged, and one thing is added that the E2 tools never needed: those
two answered about the conversation itself, so the conversation id in the
context was the whole scope. These answer about ORDERS, and orders belong to a
customer, so the scope is narrower than the tenant:

  * the tenant comes from the job (RLS), never from the arguments;
  * the CONTACT comes from the conversation the job is about, never from the
    arguments either;
  * the order number in the arguments is the only thing the model supplies, and
    it can only ever select among orders that already passed both.

An `order_id` the contact does not own therefore reads exactly like an order
that does not exist — which is the correct answer to give a stranger asking
about somebody else's parcel, and the reason a "not found" here is a success
rather than an error.
"""

from collections.abc import Mapping
from typing import Any

import psycopg

from agents_runtime.agent_core.llm import ToolSpec
from agents_runtime.repository import consent as consent_repo
from agents_runtime.repository import orders as orders_repo
from agents_runtime.repository.scope import scope_to_tenant
from agents_runtime.tools.base import ToolContext, ToolResult

#: What the model may name. `order_id` is the number the STORE gave the customer
#: (`orders.external_id`) — the only order identifier a contact has ever seen.
ORDER_ARGUMENT = "order_id"

NOT_OURS = "conversation not found for this tenant"


def _order_spec(name: str, description: str) -> ToolSpec:
    """`order_id` is OPTIONAL, and the description says what leaving it out
    means. A model that had to supply one would invent one — and an invented
    order number is a lookup that answers about somebody else's parcel."""
    return ToolSpec(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": {
                ORDER_ARGUMENT: {
                    "type": "string",
                    "description": (
                        "O número do pedido, como o cliente o informou. Omita "
                        "para consultar o pedido mais recente dele."
                    ),
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    )


def _requested_order_id(arguments: Mapping[str, Any]) -> str | None:
    """Optional, and strictly parsed when present.

    Absent means "the most recent one". A wrong TYPE is not absence — coercing
    `42` to `"42"` would turn a malformed call into a plausible lookup, which is
    the webhook doctrine applied to the model.
    """
    if ORDER_ARGUMENT not in arguments or arguments[ORDER_ARGUMENT] is None:
        return None
    value = arguments[ORDER_ARGUMENT]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{ORDER_ARGUMENT}: expected a non-empty string, got {type(value).__name__}"
        )
    return value.strip()


async def _lookup(
    conn: psycopg.AsyncConnection,
    context: ToolContext,
    external_id: str | None,
) -> tuple[bool, orders_repo.MirroredOrder | None]:
    """(is this conversation ours?, the order if there is one).

    The two questions are separated because their answers are different facts:
    a stranger's conversation is a refusal, an absent order is an answer.
    """
    async with conn.transaction():
        await scope_to_tenant(conn, context.tenant_id)
        contact_id = await consent_repo.contact_of_conversation(conn, context.conversation_id)
        if contact_id is None:
            return False, None
        order = await orders_repo.load_order(conn, contact_id=contact_id, external_id=external_id)
    return True, order


def _rendered(order: orders_repo.MirroredOrder) -> dict[str, Any]:
    """What the model is allowed to see, in shapes jsonb and a prompt survive.

    Money is text and dates are ISO strings: the output is stored as jsonb and
    read by a model, and a Decimal through a float turns 199.90 into
    199.89999999999998 in both places. `id` (our uuid) is deliberately absent —
    the customer's identifier for an order is the store's number, and handing
    the model an internal key only creates something for it to repeat.
    """
    return {
        "order_id": order.external_id,
        "financial_status": order.financial_status,
        "status": order.status,
        "total": None if order.total is None else f"{order.total:.2f}",
        "currency": order.currency,
        "items": order.items,
        "placed_at": (order.platform_created_at.isoformat() if order.platform_created_at else None),
    }


class GetOrder:
    name = "get_order"
    spec = _order_spec(
        "get_order",
        "Consulta um pedido deste cliente: situação do pagamento, valor, itens e "
        "data. Só enxerga pedidos do próprio contato desta conversa.",
    )

    async def __call__(
        self,
        conn: psycopg.AsyncConnection,
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        external_id = _requested_order_id(arguments)

        ours, order = await _lookup(conn, context, external_id)
        if not ours:
            return ToolResult(tool=self.name, success=False, error=NOT_OURS)

        if order is None:
            # A success. The tool worked and the answer is no — and an order
            # this contact does not own has to be indistinguishable from an
            # order that does not exist, or the reply becomes an oracle.
            return ToolResult(tool=self.name, success=True, output={"found": False})

        return ToolResult(
            tool=self.name, success=True, output={"found": True, "order": _rendered(order)}
        )


class GetTracking:
    name = "get_tracking"
    spec = _order_spec(
        "get_tracking",
        "Consulta o código e o status de rastreio de um pedido deste cliente. "
        "Devolve vazio quando o pedido existe mas ainda não foi despachado — "
        "nesse caso, não invente prazo.",
    )

    async def __call__(
        self,
        conn: psycopg.AsyncConnection,
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        external_id = _requested_order_id(arguments)

        ours, order = await _lookup(conn, context, external_id)
        if not ours:
            return ToolResult(tool=self.name, success=False, error=NOT_OURS)

        if order is None:
            return ToolResult(tool=self.name, success=True, output={"found": False})

        # The order was found and the code may still be absent — the mirror only
        # holds what the platform sent. NULL travels as null rather than as an
        # empty string: "not shipped yet" is a fact, and a carrier API is
        # pendência nº 3 of the plan, not this tool.
        return ToolResult(
            tool=self.name,
            success=True,
            output={
                "found": True,
                "order_id": order.external_id,
                "tracking_code": order.tracking_code,
                "tracking_status": order.tracking_status,
            },
        )
