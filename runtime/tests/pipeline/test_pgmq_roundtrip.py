"""E0-08 · The queue, proven on the smallest thing it has to do.

`send` → `read(vt)` → `archive` is the whole contract the runtime depends on:
a job is handed over exactly once at a time, and every read ends somewhere.
The invariant this level exists to protect is that pgmq is never left in
limbo — so the assertions are about what is IN the queue, not only about what
a reader happens to see.
"""

from agents_runtime.repository.queue import PgmqQueue

JOB = {"conversation_id": "0b7f1a5e-1f2e-4a5b-8c7d-9e0f1a2b3c4d", "generation": 1, "target_seq": 7}


async def test_a_sent_message_comes_back_with_its_payload(queue: PgmqQueue) -> None:
    await queue.send(JOB)

    message = await queue.read(visibility_timeout=30)

    assert message is not None
    assert message.payload == JOB


async def test_a_read_message_is_invisible_to_the_next_reader(queue: PgmqQueue) -> None:
    await queue.send(JOB)

    first = await queue.read(visibility_timeout=30)
    second = await queue.read(visibility_timeout=30)

    assert first is not None
    assert second is None, "the visibility timeout is what makes a job exclusive"


async def test_an_empty_queue_reads_as_nothing(queue: PgmqQueue) -> None:
    assert await queue.read(visibility_timeout=30) is None


async def test_archiving_removes_the_message_instead_of_hiding_it(
    queue: PgmqQueue, queue_length
) -> None:
    await queue.send(JOB)
    message = await queue.read(visibility_timeout=30)
    assert message is not None

    assert await queue_length() == 1, "reading only hides the message; it is still queued"

    await queue.archive(message.id)

    assert await queue_length() == 0
