"""只裁决来源使用资格；终态、UNKNOWN 和对账仍归原 Runtime Owner。"""

from core.runtime.memory_scope import MemoryContextError


class MemoryContextGuard:
    def __init__(self, repository, lease):
        self.repository, self.lease = repository, lease

    async def check_async(self, *, admission=None, seed_digest=None):
        try:
            await self.repository.guard(self.lease, admission=admission, seed_digest=seed_digest)
        except MemoryContextError:
            raise
        except Exception as exc:
            from core.runtime.run_control import OwnershipLost
            if isinstance(exc, OwnershipLost):
                raise
            raise MemoryContextError("MEMORY_CONTEXT_UNVERIFIABLE") from None

    def check(self, submit, *, admission=None, seed_digest=None):
        if not callable(submit):
            raise MemoryContextError("MEMORY_CONTEXT_UNVERIFIABLE")
        submit(self.check_async(admission=admission, seed_digest=seed_digest))
