"""The fence counter: one item in the lock table that mints every lease's fence.

Fences only grow. Each acquire bumps the counter atomically; a missing counter
(a recreated table) restarts above the highest fence the snapshot records.
"""

from __future__ import annotations

from botocore.exceptions import ClientError

from atlantide.state.s3 import limits
from atlantide.state.s3.context import S3Context
from atlantide.state.s3.dynamo import ddb_num, ddb_str

__all__ = ["FENCE_ITEM", "Fences"]

#: Reserved key in the lock table holding the monotonic fence counter. Node ids
#: are ``{stack}:{type}:{name}`` with no empty part, so none can collide with it.
FENCE_ITEM = "\x00atlantide-fence"


class Fences:
    """Mints fences from the counter, and reads it for the fold's ``max_fence``."""

    def __init__(self, ctx: S3Context) -> None:
        self._ctx = ctx

    def next_fence(self, *, above: int) -> int:
        """Mint the next fence with an atomic counter in the lock table.

        ``above`` is the highest fence the snapshot records. A counter found
        missing (a recreated lock table) is re-seeded :data:`limits.RESEED_GAP`
        past it: fences minted since the last fold are unknown, and one of them
        may still be held by a paused run whose commits must keep failing.
        """
        # Re-seed and bump in one atomic update: with two calls, a crash between
        # them could leave the counter below a fence a paused run still holds.
        fence = self.bump_fence(1, seed=above + limits.RESEED_GAP if above > 0 else 0)
        if fence <= above:  # a counter behind the snapshot (restored by hand)
            fence = self.bump_fence(above - fence + 1, seed=0)
        return fence

    def bump_fence(self, by: int, *, seed: int) -> int:
        """Add ``by`` to the counter (starting from ``seed`` if missing); return the new value."""
        ctx = self._ctx
        try:
            response = ctx.clients.ddb.update_item(
                TableName=ctx.lock_table,
                Key={"node_id": ddb_str(FENCE_ITEM)},
                UpdateExpression="SET fence = if_not_exists(fence, :seed) + :by",
                ExpressionAttributeValues={":by": ddb_num(by), ":seed": ddb_num(seed)},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            # The first call of an acquire: a missing or misconfigured lock table
            # surfaces here, before the lock write.
            raise ctx.lock_error(exc) from exc
        return int(response["Attributes"]["fence"]["N"])

    def read_fence_counter(self) -> int:
        ctx = self._ctx
        try:
            response = ctx.clients.ddb.get_item(
                TableName=ctx.lock_table, Key={"node_id": ddb_str(FENCE_ITEM)}, ConsistentRead=True
            )
        except ClientError:
            return 0
        return int(response.get("Item", {}).get("fence", {}).get("N", 0))
