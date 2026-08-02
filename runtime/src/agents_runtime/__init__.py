"""Runtime for the WhatsApp AI agents platform.

A single asyncio process (ADR-1, ADR-4): workers, coalescer, scheduler, senders
and judges are all tasks of this process. The per-tenant semaphore is only
correct under that premise — going multi-process requires migrating it to a
distributed lease first.

Module responsibilities and their boundaries are defined in
`core/arquitetura-plataforma-agentes-whatsapp.md` §3 and enforced by the
import-linter contracts in `pyproject.toml`.
"""
