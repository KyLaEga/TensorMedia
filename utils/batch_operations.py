import os
import json
import shutil
from pathlib import Path
from send2trash import send2trash

from core.db.vector_cache import VectorCache
from utils.env_config import get_data_dir
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

    @classmethod
    def run_maintenance_all(cls):
        for mode in ["visual", "faces"]:
            cls.get_connection(f"meta_v2_{mode}.db")
            
        for db_name, db in cls._instances.items():
            try:
                db.run_maintenance()
            except Exception as e:
                auditor.error(f"Maintenance failed for {db_name}: {e}")

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
            with open(journal, "r", encoding="utf-8") as f:
                tx = json.load(f)
            
            if tx.get("status") == "pending":
                op = tx.get("op")
                auditor.warning(f"Detected pending transaction [{op}]. Executing state reconciliation.")
                
                if op == "move":
                    payload = tx.get("payload", {})
                    completed = payload.get("completed_moves", [])
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
            
            journal.unlink()
        except Exception as e:
            auditor.error(f"Journal recovery failed: {e}")
            if journal.exists():
                journal.unlink()

    @staticmethod
    def _log_transaction(op_type: str, payload: dict):
        try:
            with open(BatchOperations._journal_path(), "w", encoding="utf-8") as f:
                json.dump({"op": op_type, "payload": payload, "status": "pending"}, f)
        except Exception:
            pass

    @staticmethod
    def _commit_transaction():
        journal = BatchOperations._journal_path()
        if journal.exists():
            try:
                journal.unlink()
            except Exception:
                pass

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
        completed_moves = []
        
        BatchOperations._log_transaction("move", {"targets": file_paths, "destination": str(target), "completed_moves": completed_moves})
        
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
                    completed_moves.append((str(src), str(dst)))
                    results["success"].append((path, str(dst)))
                    
                    BatchOperations._log_transaction("move", {"targets": file_paths, "destination": str(target), "completed_moves": completed_moves})
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