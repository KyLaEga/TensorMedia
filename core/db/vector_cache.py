import sqlite3
import numpy as np
import threading
import queue
import time
from pathlib import Path

from utils.env_config import get_data_dir
from utils.logger import auditor

class VectorCache:
    def __init__(self, db_name="vectors.db"):
        self.db_path = get_data_dir() / db_name
        self.db_lock = threading.RLock()
        
        self._init_db()
        
        self.write_queue = queue.Queue(maxsize=5000)
        self.shutdown_event = threading.Event()
        self.is_running = True
        
        self.worker_thread = threading.Thread(target=self._writer_worker, daemon=True)
        self.worker_thread.start()

    def _init_db(self):
        with self.db_lock:
            try:
                with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                    conn.execute("PRAGMA journal_mode = WAL")
                    conn.execute("PRAGMA synchronous = NORMAL")
                    conn.execute("PRAGMA cache_size = -10000") 
                    conn.execute("PRAGMA foreign_keys = ON")
                    
                    conn.execute('''
                        CREATE TABLE IF NOT EXISTS metadata (
                            path TEXT PRIMARY KEY,
                            size INTEGER,
                            mtime REAL,
                            phash TEXT,
                            resolution TEXT,
                            duration REAL,
                            codec TEXT,
                            sharpness REAL,
                            fps REAL,
                            vector BLOB,
                            faces BLOB,
                            last_scan REAL
                        )
                    ''')
                    conn.commit()
            except Exception as e:
                auditor.error(f"Failed to initialize database: {e}")

    def _writer_worker(self):
        conn = None
        transaction_counter = 0
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=60.0)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check")
            if cursor.fetchone()[0] != "ok":
                auditor.critical("SQLite Database integrity compromised. Forcing purge.")
                self._execute_purge(conn)
            
            batch = []
            while True:
                try:
                    item = self.write_queue.get(timeout=1.0)
                    if item == "FLUSH_AND_EXIT":
                        if batch:
                            self._execute_batch(conn, batch)
                            transaction_counter += len(batch)
                            batch.clear()
                        # Финальное слияние и транкация WAL-журнала перед выходом
                        try: conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        except sqlite3.Error as e:
                            auditor.warning(f"Failed to truncate WAL on exit: {e}")
                        break
                        
                    if item == "PURGE_SIGNAL":
                        self._execute_batch(conn, batch)
                        batch.clear()
                        self._execute_purge(conn)
                        transaction_counter = 0
                        continue

                    batch.append(item)
                    if len(batch) >= 50 or self.write_queue.empty():
                        self._execute_batch(conn, batch)
                        transaction_counter += len(batch)
                        batch.clear()
                        
                        # Предотвращение фрагментации диска: сброс журнала каждые 5000 записей
                        if transaction_counter >= 5000:
                            try:
                                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                                transaction_counter = 0
                            except sqlite3.Error as e:
                                auditor.warning(f"WAL Checkpoint delayed: {e}")
                        
                except queue.Empty:
                    if not self.is_running and self.write_queue.empty():
                        break
                    if self.shutdown_event.is_set() and self.write_queue.empty():
                        break
                    continue
                except Exception as e:
                    auditor.error(f"VectorCache worker exception: {e}")
                    
            if batch:
                self._execute_batch(conn, batch)
                try: conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error as e:
                    auditor.warning(f"Failed to truncate WAL after batch: {e}")
                
        except Exception as global_e:
             auditor.error(f"VectorCache fatal worker error: {global_e}")
        finally:
            if conn:
                try: conn.close()
                except Exception as e:
                    auditor.warning(f"Failed to close SQLite connection: {e}")

    def _execute_batch(self, conn, batch):
        if not batch: return
        with self.db_lock:
            try:
                cursor = conn.cursor()
                cursor.executemany('''
                    INSERT OR REPLACE INTO metadata 
                    (path, size, mtime, phash, resolution, duration, codec, sharpness, fps, vector, faces, last_scan)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', batch)
                conn.commit()
            except sqlite3.Error as e:
                auditor.error(f"SQLite batch execution error: {e}")

    def _execute_purge(self, conn):
        with self.db_lock:
            try:
                conn.execute("DELETE FROM metadata")
                conn.execute("VACUUM")
                conn.commit()
                auditor.info(f"SQLite database {self.db_path.name} physically purged and vacuumed.")
            except sqlite3.Error as e:
                auditor.error(f"SQLite purge error: {e}")

    def purge_cache(self):
        if not self.is_running: return
        with self.write_queue.mutex:
            self.write_queue.queue.clear()
        self.write_queue.put("PURGE_SIGNAL")
        self.sync()

    def save_batch(self, batch_tuples: list):
        if not batch_tuples: return
        for t in batch_tuples:
            path, size, mtime, phash, res, dur, codec, sharpness, fps, vector = t
            self.store({
                "path": path, "size": size, "mtime": mtime, "phash": phash,
                "res": res, "dur": dur, "codec": codec, "sharpness": sharpness,
                "fps": fps, "vector": vector, "faces": None
            })
            
    def store(self, file_data: dict):
        if not self.is_running: return
            
        vector_blob = file_data.get("vector")
        if isinstance(vector_blob, np.ndarray):
            vector_blob = vector_blob.astype(np.float32).tobytes()
            
        faces_blob = file_data.get("faces")
        if isinstance(faces_blob, np.ndarray):
            faces_blob = faces_blob.astype(np.float32).tobytes()
            
        record = (
            file_data.get("path"), file_data.get("size"), file_data.get("mtime"),
            file_data.get("phash"), file_data.get("res"), file_data.get("dur"),
            file_data.get("codec"), file_data.get("sharpness", 0.0), file_data.get("fps"),
            vector_blob, faces_blob, time.time()
        )
        
        try:
            self.write_queue.put(record, timeout=10.0)
        except queue.Full:
            auditor.warning("VectorCache write queue saturated. Pausing to avoid OOM.")

    def get_metadata_for_paths(self, paths: list):
        self.sync()
        meta = {}
        if not paths: return meta
        with self.db_lock:
            try:
                with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                    cursor = conn.cursor()
                    chunk_size = 900
                    for i in range(0, len(paths), chunk_size):
                        chunk = paths[i:i+chunk_size]
                        placeholders = ','.join(['?'] * len(chunk))
                        cursor.execute(f"SELECT path, size, mtime, phash, resolution, duration, codec, sharpness, fps FROM metadata WHERE path IN ({placeholders})", chunk) # nosec B608
                        for row in cursor.fetchall():
                            meta[row[0]] = {
                                "size": row[1], "mtime": row[2], "phash": row[3],
                                "res": row[4], "dur": row[5], "codec": row[6],
                                "sharpness": row[7], "fps": row[8]
                            }
            except Exception as e:
                auditor.error(f"Failed to read metadata from cache: {e}")
        return meta

    def get_vector(self, path: str):
        self.sync()
        with self.db_lock:
            try:
                with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT vector FROM metadata WHERE path = ? AND vector IS NOT NULL", (path,))
                    row = cursor.fetchone()
                    if row and row[0]:
                        return np.frombuffer(row[0], dtype=np.float32)
            except Exception as e:
                auditor.error(f"Critical failure in get_vector for {path}: {e}", exc_info=True)
        return None

    def sync(self):
        while not self.write_queue.empty():
            time.sleep(0.01)

    def delete_paths(self, paths: list[str]):
        self.sync()
        if not paths:
            return
        with self.db_lock:
            try:
                with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                    cursor = conn.cursor()
                    cursor.executemany(
                        "DELETE FROM metadata WHERE path = ?",
                        [(p,) for p in paths],
                    )
                    conn.commit()
            except Exception as e:
                auditor.error(f"Failed to delete cache entries: {e}")

    def move_paths(self, src_dst_pairs: list[tuple[str, str]]):
        """
        Update cached metadata to track moved files.
        Keeps vectors/faces intact by rewriting the primary key `path`.
        """
        self.sync()
        if not src_dst_pairs:
            return
        with self.db_lock:
            try:
                with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                    cursor = conn.cursor()
                    cursor.executemany(
                        "UPDATE metadata SET path = ? WHERE path = ?",
                        [(dst, src) for (src, dst) in src_dst_pairs],
                    )
                    conn.commit()
            except Exception as e:
                auditor.error(f"Failed to move cache entries: {e}")

    def run_maintenance(self):
        """
        Выполняет очистку базы данных от записей, файлы которых больше не существуют на диске,
        и дефрагментирует файл БД (VACUUM).
        """
        if not self.is_running: return
        
        auditor.info("Starting database maintenance (GC & Vacuum)...")
        start_time = time.time()
        
        orphans = []
        with self.db_lock:
            try:
                with sqlite3.connect(str(self.db_path), timeout=60.0) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT path FROM metadata")
                    all_paths = [row[0] for row in cursor.fetchall()]
                    
                    # Проверка существования файлов (I/O операция)
                    import os
                    for p in all_paths:
                        if not os.path.exists(p):
                            orphans.append((p,))
                    
                    if orphans:
                        auditor.info(f"Maintenance: Found {len(orphans)} orphaned records. Deleting...")
                        cursor.executemany("DELETE FROM metadata WHERE path = ?", orphans)
                        conn.commit()
                    
                    # Дефрагментация
                    auditor.info("Maintenance: Running VACUUM...")
                    conn.execute("VACUUM")
                    conn.commit()
                    
            except Exception as e:
                auditor.error(f"Database maintenance failed: {e}")
                return

        duration = time.time() - start_time
        auditor.info(f"Database maintenance completed in {duration:.2f}s. Removed {len(orphans)} orphans.")

    def close(self):
        self.is_running = False
        self.shutdown_event.set()
        if self.worker_thread.is_alive():
            self.write_queue.put("FLUSH_AND_EXIT")
            self.worker_thread.join(timeout=10.0)