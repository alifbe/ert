from __future__ import annotations

import os
from typing import Optional

from fastapi import Depends

from ert.dark_storage.security import security
from ert.storage import StorageReader, open_storage

__all__ = ["get_storage"]


_storage: Optional[StorageReader] = None

DEFAULT_SECURITY = Depends(security)


def get_storage() -> StorageReader:
    global _storage  # noqa: PLW0603e
    if _storage is None:
        return (_storage := open_storage(os.environ["ERT_STORAGE_ENS_PATH"]))
    _storage.refresh()
    return _storage
