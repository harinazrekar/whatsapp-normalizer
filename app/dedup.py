from .config import settings
from .redis_client import get_redis


async def is_duplicate(message_id: str) -> bool:
    """
    Atomically claims message_id. Returns True if it was ALREADY claimed
    (i.e. this is a duplicate delivery) and False if this call just claimed it.
    """
    key = f"wa:dedup:{message_id}"
    was_newly_set = await get_redis().set(key, "1", ex=settings.DEDUP_TTL_SECONDS, nx=True)
    return was_newly_set is None
