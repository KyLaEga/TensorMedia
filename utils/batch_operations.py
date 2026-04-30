import os
import shutil
import send2trash
from pathlib import Path

class BatchOperations:
    @staticmethod
    def safe_delete(file_paths: list) -> dict:
        """Перемещение в системную корзину."""
        res = {"deleted": 0, "failed": 0}
        for p in file_paths:
            try:
                if os.path.exists(p):
                    send2trash.send2trash(p)
                    res["deleted"] += 1
            except: res["failed"] += 1
        return res

    @staticmethod
    def hard_delete(file_paths: list) -> dict:
        """Абсолютное уничтожение файлов (в обход корзины). Полезно для внешних дисков."""
        res = {"deleted": 0, "failed": 0}
        for p in file_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
                    res["deleted"] += 1
            except: res["failed"] += 1
        return res

    @staticmethod
    def move_files(file_paths: list, dest_dir: str) -> dict:
        """Перемещение файлов в другой каталог."""
        res = {"moved": 0, "failed": 0}
        os.makedirs(dest_dir, exist_ok=True)
        for p in file_paths:
            try:
                if os.path.exists(p):
                    target = os.path.join(dest_dir, Path(p).name)
                    if os.path.exists(target):
                        base, ext = os.path.splitext(Path(p).name)
                        target = os.path.join(dest_dir, f"{base}_moved{ext}")
                    shutil.move(p, target)
                    res["moved"] += 1
            except: res["failed"] += 1
        return res