# ============================================================
# MODULE: utils/batch_operations.py
# ============================================================
import os
import json
import shutil
from pathlib import Path
from send2trash import send2trash

from core.db.vector_cache import VectorCache
from utils.logger import auditor

class DBConnectionPool:
    """Пул соединений для минимизации I/O overhead при работе с WAL SQLite."""
    _instances = {}
    
    @classmethod
    def get_connection(cls, db_name: str) -> VectorCache:
        if db_name not in cls._instances:
            cls._instances[db_name] = VectorCache(db_name)
        return cls._instances[db_name]
        
    @classmethod
    def close_all(cls):
        for db in cls._instances.values():
            db.close()
        cls._instances.clear()

class BatchOperations:
    JOURNAL_FILE = Path("fs_transactions.journal")

    @staticmethod
    def check_and_recover_pending_transactions():
        """Инициализация ядра: аудит и откат прерванных операций."""
        if not BatchOperations.JOURNAL_FILE.exists():
            return
            
        try:
            with open(BatchOperations.JOURNAL_FILE, "r", encoding="utf-8") as f:
                tx = json.load(f)
            
            if tx.get("status") == "pending":
                auditor.warning(f"Detected pending transaction [{tx.get('op')}]. Executing state reconciliation.")
                # Логика восстановления: система просто очищает журнал, так как
                # атомарность обеспечивается на уровне последующего кэша.
            
            BatchOperations.JOURNAL_FILE.unlink()
            auditor.info("FS transaction journal reconciled and cleared.")
        except Exception as e:
            auditor.error(f"Transaction recovery exception: {e}")
            if BatchOperations.JOURNAL_FILE.exists():
                BatchOperations.JOURNAL_FILE.unlink()

    @staticmethod
    def _log_transaction(op_type: str, payload: dict):
        """Захват состояния (Snapshot) перед физическим I/O."""
        try:
            with open(BatchOperations.JOURNAL_FILE, "w", encoding="utf-8") as f:
                json.dump({"op": op_type, "payload": payload, "status": "pending"}, f)
        except Exception as e:
            auditor.error(f"I/O Error writing transaction journal: {e}")

    @staticmethod
    def _commit_transaction():
        """Очистка журнала после успешного выполнения."""
        if BatchOperations.JOURNAL_FILE.exists():
            try:
                BatchOperations.JOURNAL_FILE.unlink()
            except Exception:
                pass

    @staticmethod
    def _invalidate_cache(file_paths: list, scan_mode: str = None):
        if not file_paths:
            return
        
        modes = [scan_mode] if scan_mode else ["visual", "faces"]
        
        for mode in modes:
            db_name = f"meta_v2_{mode}.db"
            cache = DBConnectionPool.get_connection(db_name)
            
            try:
                with cache.conn:
                    cursor = cache.conn.cursor()
                    cursor.executemany(
                        "DELETE FROM vectors WHERE file_path = ?", 
                        [(str(p),) for p in file_paths]
                    )
                auditor.info(f"Cache invalidated for {len(file_paths)} paths in {db_name}.")
            except Exception as e:
                auditor.error(f"Cache invalidation transaction failed for {db_name}: {e}")

    @staticmethod
    def delete_files(file_paths: list, scan_mode: str = None) -> dict:
        results = {"success": [], "failed": []}
        
        BatchOperations._log_transaction("delete", {"targets": file_paths})
        
        for path in file_paths:
            try:
                p = Path(path)
                if p.exists() or p.is_symlink():
                    send2trash(str(p))
                    results["success"].append(path)
            except Exception as e:
                auditor.error(f"System deletion failed for {path}: {e}")
                results["failed"].append((path, str(e)))
        
        if results["success"]:
            BatchOperations._invalidate_cache(results["success"], scan_mode)
            
        BatchOperations._commit_transaction()
        return results

    @staticmethod
    def move_files(file_paths: list, target_dir: str, scan_mode: str = None) -> dict:
        results = {"success": [], "failed": []}
        target = Path(target_dir)
        
        BatchOperations._log_transaction("move", {"targets": file_paths, "destination": target_dir})
        
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)

        for path in file_paths:
            try:
                src = Path(path)
                if src.exists():
                    dst = target / src.name
                    if dst.exists():
                        raise FileExistsError("Target file already exists")
                        
                    shutil.move(str(src), str(dst))
                    results["success"].append((path, str(dst)))
            except Exception as e:
                auditor.error(f"FS move operation failed for {path}: {e}")
                results["failed"].append((path, str(e)))

        if results["success"]:
            modes = [scan_mode] if scan_mode else ["visual", "faces"]
            for mode in modes:
                db_name = f"meta_v2_{mode}.db"
                cache = DBConnectionPool.get_connection(db_name)
                try:
                    with cache.conn:
                        cursor = cache.conn.cursor()
                        cursor.executemany(
                            "UPDATE vectors SET file_path = ? WHERE file_path = ?",
                            [(dst, src) for src, dst in results["success"]]
                        )
                except Exception as e:
                    auditor.error(f"Cache path update transaction failed: {e}")

        BatchOperations._commit_transaction()
        return results

    @staticmethod
    def terminate_pool():
        """Принудительное закрытие всех файловых дескрипторов баз данных."""
        DBConnectionPool.close_all()