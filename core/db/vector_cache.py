import sqlite3
import numpy as np
from pathlib import Path
from utils.env_config import get_app_data_dir

class VectorCache:
    """Локальная СУБД для кэширования визуальных векторов (Persistent Connection)."""
    
    def __init__(self, db_name="tensor_cache.db"):
        app_dir = get_app_data_dir() / "db"
        app_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = app_dir / db_name
        
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS media_cache (
                    path TEXT PRIMARY KEY,
                    mtime REAL,
                    size INTEGER,
                    phash TEXT,
                    vector BLOB
                )
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_phash ON media_cache(phash)")

    def get_cached_data(self, path: str, current_mtime: float, current_size: int):
        cursor = self.conn.execute("SELECT mtime, size, phash, vector FROM media_cache WHERE path = ?", (path,))
        row = cursor.fetchone()
        if row and row[0] == current_mtime and row[1] == current_size:
            vector = np.frombuffer(row[3], dtype=np.float32) if row[3] else None
            return row[2], vector
        return None, None

    def save_data(self, path: str, mtime: float, size: int, phash: str, vector: np.ndarray):
        v_blob = vector.tobytes() if vector is not None else None
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO media_cache (path, mtime, size, phash, vector)
                VALUES (?, ?, ?, ?, ?)
            """, (path, mtime, size, phash, v_blob))

    def close(self):
        self.conn.close()