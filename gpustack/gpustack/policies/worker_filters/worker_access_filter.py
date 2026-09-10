"""Scheduler-level worker access filter (user isolation, feature one).

Removes workers the model owner has not been granted: a user's model
instances may only land on nodes the admin explicitly opened to them
(via the ``worker_access`` grants). Platform-admin-owned and
system-owned models keep the full candidate pool.
"""

import logging
from typing import List, Optional, Tuple

from gpustack.policies.base import WorkerFilter
from gpustack.schemas.models import Model
from gpustack.schemas.workers import Worker

logger = logging.getLogger(__name__)


class WorkerAccessFilter(WorkerFilter):
    def __init__(self, model: Model, owner_worker_grant_ids: Optional[set] = None):
        self._model = model
        # Pre-resolved grant ids for the model's owner principal;
        # None means "owner is unrestricted" (admin / platform-owned).
        self._grant_ids = owner_worker_grant_ids

    async def filter(self, workers: List[Worker]) -> Tuple[List[Worker], List[str]]:
        if self._grant_ids is None:
            return workers, []

        candidates = [w for w in workers if w.id in self._grant_ids]
        return candidates, [
            f"Matched {len(candidates)} worker(s) by user isolation grants "
            f"(owner principal {getattr(self._model, 'owner_principal_id', None)})."
        ]
