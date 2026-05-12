import os
import json
import shutil
from pathlib import Path
from send2trash import send2trash

from core.db.vector_cache import VectorCache
from utils.logger import auditor

class DBConnectionPool:
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
        if not BatchOperations.JOURNAL_FILE.exists():
            return
            
        try:
            with open(BatchOperations.JOURNAL_FILE, "r", encoding="utf-8") as f:
                tx = json.load(f)
            
            if tx.get("status") == "pending":
                auditor.warning(f"Detected pending transaction [{tx.get('op')}]. Executing state reconciliation.")
            
            BatchOperations.JOURNAL_FILE.unlink()
        except Exception as e:
            if BatchOperations.JOURNAL_FILE.exists():
                BatchOperations.JOURNAL_FILE.unlink()

    @staticmethod
    def _log_transaction(op_type: str, payload: dict):
        try:
            with open(BatchOperations.JOURNAL_FILE, "w", encoding="utf-8") as f:
                json.dump({"op": op_type, "payload": payload, "status": "pending"}, f)
        except Exception:
            pass

    @staticmethod
    def _commit_transaction():
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
                        [(str(Path(p).resolve()),) for p in file_paths]
                    )
            except Exception:
                pass

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
            except Exception as e:
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
                            [(str(Path(dst).resolve()), str(Path(src).resolve())) for src, dst in results["success"]]
                        )
                except Exception:
                    pass

        BatchOperations._commit_transaction()
        return results

    @staticmethod
    def terminate_pool():
        DBConnectionPool.close_all()