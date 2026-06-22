import unittest
import os
import time
import tempfile
import shutil
import numpy as np
from pathlib import Path

# Mock get_data_dir before importing VectorCache
import utils.env_config
test_data_dir = tempfile.mkdtemp()
os.makedirs(os.path.join(test_data_dir, "logs"), exist_ok=True)
utils.env_config.get_data_dir = lambda: Path(test_data_dir)
utils.env_config.get_logs_dir = lambda: Path(test_data_dir) / "logs"

from core.db.vector_cache import VectorCache

class TestVectorCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = test_data_dir

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.test_dir):
            shutil.rmtree(cls.test_dir)

    def setUp(self):
        self.test_db_name = "test_vectors.db"
        self.cache = VectorCache(self.test_db_name)
        self.db_path = self.cache.db_path

    def tearDown(self):
        self.cache.close()
        if self.db_path.exists():
            try:
                os.remove(self.db_path)
                # Also remove WAL/SHM files if they exist
                for suffix in ["-wal", "-shm"]:
                    p = Path(str(self.db_path) + suffix)
                    if p.exists():
                        os.remove(p)
            except Exception:
                pass

    def test_store_and_get_metadata(self):
        test_path = "/test/path/file.mp4"
        data = {
            "path": test_path,
            "size": 1024,
            "mtime": time.time(),
            "phash": "abc",
            "res": "1920x1080",
            "dur": 60.0,
            "codec": "h264",
            "sharpness": 50.0,
            "fps": 30.0,
            "vector": np.random.rand(128).astype(np.float32)
        }
        self.cache.store(data)
        self.cache.sync()
        
        meta = self.cache.get_metadata_for_paths([test_path])
        self.assertIn(test_path, meta)
        self.assertEqual(meta[test_path]["size"], 1024)
        self.assertEqual(meta[test_path]["res"], "1920x1080")

    def test_vector_storage(self):
        # Векторы теперь МНОГОКАДРОВЫЕ 2D (кадры × 768 для visual). На диске формат
        # бесшапочный (flat float32); форму get_vector восстанавливает по длине
        # блоба (кратно 3072 байт → reshape(-1, 768)). Тестируем реальный формат,
        # а не произвольную размерность.
        test_path = "/test/path/vec.jpg"
        vec = np.random.rand(3, 768).astype(np.float32)   # 3-кадровый визуальный вектор
        self.cache.store({"path": test_path, "vector": vec})
        self.cache.sync()

        stored_vec = self.cache.get_vector(test_path)
        self.assertIsNotNone(stored_vec)
        self.assertEqual(stored_vec.shape, (3, 768))
        np.testing.assert_array_almost_equal(vec, stored_vec)

    def test_delete_paths(self):
        test_path = "/test/to/delete.mp4"
        self.cache.store({"path": test_path, "size": 100})
        self.cache.sync()
        
        self.cache.delete_paths([test_path])
        meta = self.cache.get_metadata_for_paths([test_path])
        self.assertNotIn(test_path, meta)

    def test_maintenance_gc(self):
        # Create a record for a non-existent file
        test_path = "/non/existent/file.mp4"
        self.cache.store({"path": test_path, "size": 100})
        self.cache.sync()
        
        # Run maintenance
        self.cache.run_maintenance()
        
        # Record should be gone
        meta = self.cache.get_metadata_for_paths([test_path])
        self.assertNotIn(test_path, meta)

if __name__ == '__main__':
    unittest.main()
