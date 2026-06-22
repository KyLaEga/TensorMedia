import threading

from core.db.vector_cache import VectorCache
from utils.logger import auditor

class DBConnectionPool:
    _instances = {}
    _lock = threading.Lock()

    @classmethod
    def get_connection(cls, db_name: str) -> VectorCache:
        # Guard the check-then-act: the scan thread, MaintenanceWorker and the
        # UI/BatchOperations thread can all hit the first access concurrently.
        # Without this lock two VectorCache instances (=> two writer threads and
        # two WAL connections on one file) get created -> "database is locked".
        with cls._lock:
            if db_name not in cls._instances:
                cls._instances[db_name] = VectorCache(db_name)
            return cls._instances[db_name]

    @classmethod
    def close_all(cls):
        with cls._lock:
            for db in cls._instances.values():
                db.close()
            cls._instances.clear()

    @classmethod
    def run_maintenance_all(cls):
        for mode in ["visual", "faces"]:
            cls.get_connection(f"meta_v2_{mode}.db")

        with cls._lock:
            snapshot = list(cls._instances.items())

        for db_name, db in snapshot:
            try:
                db.run_maintenance()
            except Exception as e:
                auditor.error(f"Maintenance failed for {db_name}: {e}")

    @classmethod
    def purge_all(cls):
        # DESTRUCTIVE: drops every indexed record from all vector stores. Ensures
        # the canonical caches exist first so a purge right after launch (before
        # any scan instantiated them) still wipes the on-disk databases.
        for mode in ["visual", "faces"]:
            cls.get_connection(f"meta_v2_{mode}.db")

        with cls._lock:
            snapshot = list(cls._instances.items())

        for db_name, db in snapshot:
            try:
                db.purge_cache()
            except Exception as e:
                auditor.error(f"Purge failed for {db_name}: {e}")
