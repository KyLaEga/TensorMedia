# ============================================================
# MODULE: core/ml/faiss_manager.py — FAISS index, search, disk cache, clustering graph
# ============================================================
import hashlib
import os
import shutil
from pathlib import Path
from typing import Optional, Tuple

import faiss
import numpy as np

from utils.env_config import get_data_dir


class FaissManager:
    def __init__(self, scan_mode: str = "visual"):
        self._scan_mode = scan_mode or "visual"

    def set_scan_mode(self, scan_mode: str) -> None:
        if scan_mode:
            self._scan_mode = scan_mode

    @property
    def scan_mode(self) -> str:
        return self._scan_mode

    def cache_dir(self) -> Path:
        path = get_data_dir() / "faiss_cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def purge_disk_cache() -> None:
        path = get_data_dir() / "faiss_cache"
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, exist_ok=True)

    def build_index_and_search(self, vectors: np.ndarray, k: int) -> Tuple[np.ndarray, np.ndarray]:
        vectors = vectors.astype(np.float32)
        dim = vectors.shape[1]
        index = faiss.IndexFlatIP(dim)
        index.add(vectors)
        distances, keys = index.search(vectors, k)
        return distances, keys

    def build_clusters(self, file_data: list, threshold: float, scan_mode: Optional[str] = None) -> list:
        clusters: list = []
        mode = (scan_mode or self._scan_mode or "visual")

        valid_file_data = []
        for item in file_data:
            if item.get("vector") is not None and isinstance(item["vector"], np.ndarray):
                valid_file_data.append(item)

        if not valid_file_data or len(valid_file_data) < 2:
            return clusters

        state_str = "".join([f"{item['path']}_{item['size']}_{item['mtime']}" for item in valid_file_data])
        state_signature = hashlib.md5(state_str.encode("utf-8"), usedforsecurity=False).hexdigest()

        cache_dir = self.cache_dir()
        dist_file = cache_dir / f"{mode}_{state_signature}_dist.npy"
        keys_file = cache_dir / f"{mode}_{state_signature}_keys.npy"

        k = min(10000, len(valid_file_data))
        cache_valid = False
        distances = keys = None

        if dist_file.exists() and keys_file.exists():
            try:
                tmp_keys = np.load(keys_file)
                if tmp_keys.shape[1] >= k:
                    distances = np.load(dist_file)[:, :k]
                    keys = tmp_keys[:, :k]
                    cache_valid = True
            except Exception as e:
                from utils.logger import auditor
                auditor.warning(f"Failed to load FAISS cache: {e}")

        if not cache_valid:
            vectors = np.vstack([item["vector"] for item in valid_file_data]).astype(np.float32)
            distances, keys = self.build_index_and_search(vectors, k)

            for old_file in cache_dir.glob(f"{mode}_*.npy"):
                try:
                    os.remove(old_file)
                except Exception as e:
                    from utils.logger import auditor
                    auditor.warning(f"Failed to remove old FAISS cache file {old_file}: {e}")

            np.save(dist_file, distances)
            np.save(keys_file, keys)

        sim_threshold = 1.0 - threshold
        seq_exts = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
        doc_exts = {".cbz", ".pdf", ".gif"}
        adj = {i: [] for i in range(len(valid_file_data))}

        for i in range(len(valid_file_data)):
            ext_i = Path(valid_file_data[i]["path"]).suffix.lower()
            is_seq_i = ext_i in seq_exts
            is_doc_i = ext_i in doc_exts

            for j in range(k):
                n_idx = int(keys[i][j])
                if n_idx == i or n_idx == -1:
                    continue

                sim = float(distances[i][j])
                ext_j = Path(valid_file_data[n_idx]["path"]).suffix.lower()

                if is_seq_i and ext_j in seq_exts:
                    sim = 1.0 - (1.0 - sim) * 1.45
                
                if is_doc_i and ext_j in doc_exts:
                    local_threshold = max(0.70, sim_threshold - 0.05)
                    if sim >= local_threshold:
                        adj[i].append((n_idx, sim))
                    continue

                if sim >= sim_threshold:
                    adj[i].append((n_idx, sim))

        visited = set()
        for i in range(len(valid_file_data)):
            if i not in visited:
                cluster_indices = [i]
                visited.add(i)
                for n_idx, _ in adj[i]:
                    if n_idx not in visited:
                        visited.add(n_idx)
                        cluster_indices.append(n_idx)

                if len(cluster_indices) > 1:
                    clusters.append([valid_file_data[idx] for idx in cluster_indices])

        refined_clusters = []
        for cluster in clusters:
            base_item = max(cluster, key=lambda x: x["size"])
            base_vec = base_item["vector"]
            for item in cluster:
                item["similarity"] = (
                    1.0
                    if item == base_item
                    else max(0.0, float(np.dot(base_vec, item["vector"])))
                )
            cluster.sort(key=lambda x: x["similarity"], reverse=True)
            refined_clusters.append(cluster)

        return refined_clusters