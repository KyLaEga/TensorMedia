import os
import json
import shutil
import threading
from pathlib import Path
from send2trash import send2trash

from core.db.vector_cache import VectorCache
from utils.env_config import get_data_dir
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

class BatchOperations:
    @staticmethod
    def _journal_path() -> Path:
        return get_data_dir() / "fs_transactions.journal"

    @staticmethod
    def check_and_recover_pending_transactions():
        journal = BatchOperations._journal_path()
        if not journal.exists():
            return

        try:
            # JSONL journal: first line is the header record, subsequent lines
            # are {"completed_move": [src, dst]} appended one per committed move.
            # Parsed line-by-line so a torn final line (crash mid-write) is
            # tolerated rather than discarding the whole journal.
            header = None
            completed = []
            with open(journal, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(rec, dict) and "op" in rec:
                        header = rec
                        # Back-compat: legacy single-object journals stored the
                        # completed list inside the header payload.
                        legacy = (rec.get("payload") or {}).get("completed_moves") or []
                        for pair in legacy:
                            completed.append(tuple(pair))
                    elif isinstance(rec, dict) and "completed_move" in rec:
                        completed.append(tuple(rec["completed_move"]))

            if header and header.get("status") == "pending" and header.get("op") == "move":
                auditor.warning("Detected pending transaction [move]. Executing state reconciliation.")
                rollbacks = 0
                for src, dst in completed:
                    try:
                        src_path = Path(src)
                        dst_path = Path(dst)
                        if dst_path.exists() and not src_path.exists():
                            src_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(dst_path), str(src_path))
                            rollbacks += 1
                    except Exception as e:
                        auditor.error(f"Rollback failed for {dst} -> {src}: {e}")

                if rollbacks > 0:
                    auditor.info(f"Transaction Rollback Complete: {rollbacks} files restored.")
        except Exception as e:
            auditor.error(f"Journal recovery failed: {e}")
        finally:
            # Guarantee the journal is removed even when parsing raised (e.g. an
            # empty/corrupt JSON). Otherwise the same poison journal would fail
            # recovery on every subsequent launch and never clear.
            try:
                journal.unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                auditor.error(f"Failed to remove journal after recovery: {e}")

    @staticmethod
    def _log_transaction(op_type: str, payload: dict):
        # Writes (truncating) the header line of a JSONL journal.
        try:
            with open(BatchOperations._journal_path(), "w", encoding="utf-8") as f:
                json.dump({"op": op_type, "payload": payload, "status": "pending"}, f)
                f.write("\n")
        except Exception as e:
            # A lost journal header means a crash mid-move has no recovery
            # point -> silent data loss. This must never be swallowed.
            auditor.error(f"Failed to write TX journal header [{op_type}]: {e}", exc_info=True)

    @staticmethod
    def _append_journal(record: dict):
        # Appends one JSONL line; O(1) per move instead of rewriting the whole
        # (growing) journal on every iteration.
        try:
            with open(BatchOperations._journal_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
                f.flush()
        except Exception as e:
            # A dropped per-move line means that move can't be rolled back
            # after a crash. Surface it instead of hiding the inconsistency.
            auditor.error(f"Failed to append TX journal record: {e}", exc_info=True)

    @staticmethod
    def _commit_transaction():
        journal = BatchOperations._journal_path()
        if journal.exists():
            try:
                journal.unlink()
            except Exception as e:
                # A stale journal triggers a spurious rollback on next launch
                # (files moved back unexpectedly). Log so it can be cleaned up.
                auditor.warning(f"Failed to commit (unlink) TX journal: {e}", exc_info=True)

    @staticmethod
    def _invalidate_cache(file_paths: list, scan_mode: str = None):
        if not file_paths:
            return
        
        modes = [scan_mode] if scan_mode else ["visual", "faces"]
        norm_paths = [str(Path(p).resolve()) for p in file_paths]
        
        for mode in modes:
            db_name = f"meta_v2_{mode}.db"
            cache = DBConnectionPool.get_connection(db_name)
            try:
                cache.delete_paths(norm_paths)
            except Exception as e:
                auditor.warning(f"Failed to invalidate cache for paths: {e}")

    @staticmethod
    def delete_files(file_paths: list, scan_mode: str = None) -> dict:
        results = {"success": [], "failed": []}
        BatchOperations._log_transaction("delete", {"targets": file_paths})
        
        for path in file_paths:
            try:
                p = Path(path).resolve()
                if p.exists() or p.is_symlink():
                    try:
                        send2trash(str(p))
                        results["success"].append(path)
                    except Exception as e:
                        os.remove(str(p)) 
                        results["success"].append(path)
            except Exception as e:
                results["failed"].append((path, str(e)))
        
        if results["success"]:
            BatchOperations._invalidate_cache(results["success"], scan_mode)
            
        BatchOperations._commit_transaction()
        return results

    @staticmethod
    def move_files(file_paths: list, target_dir: str, scan_mode: str = None) -> dict:
        results = {"success": [], "failed": []}
        target = Path(target_dir).resolve()

        BatchOperations._log_transaction("move", {"targets": file_paths, "destination": str(target)})

        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)

        for path in file_paths:
            try:
                src = Path(path).resolve()
                if src.exists():
                    dst = target / src.name
                    if dst.exists():
                        raise FileExistsError("Target file already exists")

                    shutil.move(str(src), str(dst))
                    results["success"].append((path, str(dst)))

                    # Append a single constant-size line per committed move
                    # (O(N) total) instead of rewriting the whole journal (O(N^2)).
                    BatchOperations._append_journal({"completed_move": [str(src), str(dst)]})
            except Exception as e:
                results["failed"].append((path, str(e)))

        if results["success"]:
            modes = [scan_mode] if scan_mode else ["visual", "faces"]
            for mode in modes:
                db_name = f"meta_v2_{mode}.db"
                cache = DBConnectionPool.get_connection(db_name)
                try:
                    cache.move_paths(
                        [(str(Path(src).resolve()), str(Path(dst).resolve())) for src, dst in results["success"]]
                    )
                except Exception as e:
                    auditor.warning(f"Failed to move cache paths: {e}")

        BatchOperations._commit_transaction()
        return results

    @staticmethod
    def terminate_pool():
        DBConnectionPool.close_all()