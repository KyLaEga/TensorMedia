import unittest
import os
import shutil
import tempfile
from pathlib import Path

# Mock get_data_dir before importing SafeFSExecutor
import utils.env_config
test_data_dir = tempfile.mkdtemp()
os.makedirs(os.path.join(test_data_dir, "logs"), exist_ok=True)
utils.env_config.get_data_dir = lambda: Path(test_data_dir)
utils.env_config.get_logs_dir = lambda: Path(test_data_dir) / "logs"

from core.services.fs_service import SafeFSExecutor

class TestSafeFSExecutor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.global_test_dir = test_data_dir

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.global_test_dir):
            shutil.rmtree(cls.global_test_dir)

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.src_file = os.path.join(self.test_dir, "test_file.txt")
        with open(self.src_file, "w") as f:
            f.write("test content")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_move_files(self):
        dest_dir = os.path.join(self.test_dir, "dest")
        os.makedirs(dest_dir)
        
        res = SafeFSExecutor.move_files([self.src_file], dest_dir)
        
        self.assertEqual(res["moved"], 1)
        self.assertFalse(os.path.exists(self.src_file))
        self.assertTrue(os.path.exists(os.path.join(dest_dir, "test_file.txt")))

    def test_hard_delete(self):
        res = SafeFSExecutor.hard_delete([self.src_file])
        
        self.assertEqual(res["deleted"], 1)
        self.assertFalse(os.path.exists(self.src_file))

    def test_move_non_existent(self):
        non_existent = os.path.join(self.test_dir, "ghost.txt")
        dest_dir = os.path.join(self.test_dir, "dest")
        os.makedirs(dest_dir)
        
        res = SafeFSExecutor.move_files([non_existent], dest_dir)
        self.assertEqual(res["failed"], 1)

    def test_safe_delete(self):
        # Create a file to trash
        trash_file = os.path.join(self.test_dir, "to_trash.txt")
        with open(trash_file, "w") as f:
            f.write("trash me")
            
        res = SafeFSExecutor.safe_delete([trash_file])
        
        # On some CI environments, send2trash might fail if there's no real trash
        # but in our local test it should work or at least not crash.
        self.assertTrue(res["deleted"] == 1 or res["failed"] == 1)
        self.assertFalse(os.path.exists(trash_file))

if __name__ == '__main__':
    unittest.main()
