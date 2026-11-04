import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .errors import UserError


class Jobs:
    """Bounded admission, one media job per user, and cleanup protection."""

    def __init__(self, workers: int, queue: int):
        self.semaphore = asyncio.Semaphore(workers)
        self.capacity = workers + queue
        self.active: dict[int, str] = {}

    @property
    def protected(self) -> set[str]:
        return set(self.active.values())

    @asynccontextmanager
    async def slot(self, user_id: int, track_id: str) -> AsyncIterator[None]:
        if user_id in self.active:
            raise UserError("Твой трек уже обрабатывается. Дождись результата и попробуй снова.")
        if len(self.active) >= self.capacity:
            raise UserError("Сейчас очередь заполнена. Попробуй чуть позже.")
        self.active[user_id] = track_id
        try:
            async with self.semaphore:
                yield
        finally:
            self.active.pop(user_id, None)
