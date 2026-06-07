# ============================================================
# MODULE: core/ml/faiss_manager.py
# ============================================================
import hashlib
import os
import shutil
from pathlib import Path
from typing import Optional, Tuple

import faiss
import numpy as np

from utils.env_config import get_data_dir

# Neighbours retained per item. The old k=min(10000, N) made the search return
# an N x k matrix (~1.6 GB of distances+keys at N=k=10^4, also written to disk)
# and an O(N*k) adjacency build -> OOM on large libraries. For near-duplicate
# clustering a small bounded fan-out is ample; raise this only if extremely
# large identical-file groups must stay in one cluster.
MAX_NEIGHBORS = 256
# Above this corpus size, brute-force IndexFlatIP (O(N^2)) becomes the dominant
# cost; switch to an approximate HNSW graph index.
ANN_THRESHOLD = 50_000


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

    @staticmethod
    def _atomic_save(path: Path, arr: np.ndarray) -> None:
        # np.save appends .npy unless given a file object; write to a sibling
        # temp via a file handle, then atomically rename. Guarantees a reader
        # never sees a half-written array.
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as fh:
            np.save(fh, arr)
        os.replace(tmp, path)

    def build_index_and_search(self, vectors: np.ndarray, k: int) -> Tuple[np.ndarray, np.ndarray]:
        vectors = np.ascontiguousarray(vectors.astype(np.float32))
        n, dim = vectors.shape
        # Vectors are L2-normalised upstream, so inner product == cosine for
        # both the exact and the HNSW path.
        if n > ANN_THRESHOLD:
            index = faiss.IndexHNSWFlat(dim, 32, faiss.METRIC_INNER_PRODUCT)
            index.hnsw.efSearch = max(64, k)
        else:
            index = faiss.IndexFlatIP(dim)
        index.add(vectors)
        distances, keys = index.search(vectors, k)
        return distances, keys

    def build_clusters(self, file_data: list, threshold: float, scan_mode: Optional[str] = None) -> list:
        clusters: list = []
        mode = (scan_mode or self._scan_mode or "visual")

        valid_file_data = []
        for item in file_data:
            vec = item.get("vector")
            if vec is not None and isinstance(vec, np.ndarray):
                # КРИТИЧЕСКИЙ ПАТЧ: Фильтрация NaN/Inf, которые ломают C++ SIMD циклы FAISS
                if np.isfinite(vec).all():
                    valid_file_data.append(item)

        if not valid_file_data or len(valid_file_data) < 2:
            return clusters

        state_str = "".join([f"{item['path']}_{item['size']}_{item['mtime']}" for item in valid_file_data])
        state_signature = hashlib.md5(state_str.encode("utf-8"), usedforsecurity=False).hexdigest()

        cache_dir = self.cache_dir()
        dist_file = cache_dir / f"{mode}_{state_signature}_dist.npy"
        keys_file = cache_dir / f"{mode}_{state_signature}_keys.npy"

        k = min(MAX_NEIGHBORS, len(valid_file_data))
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
            vectors_raw = np.vstack([item["vector"] for item in valid_file_data]).astype(np.float32)
            # КРИТИЧЕСКИЙ ПАТЧ: Принудительное выделение непрерывного блока памяти (C-Contiguous)
            vectors = np.ascontiguousarray(vectors_raw)
            
            distances, keys = self.build_index_and_search(vectors, k)

            # Only drop caches from *other* signatures of this mode; never glob
            # -nuke files a concurrent run may have just written for the current
            # signature. Also clear any leftover temp files.
            keep = {dist_file.name, keys_file.name}
            for old_file in list(cache_dir.glob(f"{mode}_*.npy")) + list(cache_dir.glob(f"{mode}_*.npy.tmp")):
                if old_file.name in keep:
                    continue
                try:
                    os.remove(old_file)
                except Exception as e:
                    from utils.logger import auditor
                    auditor.warning(f"Failed to remove old FAISS cache file {old_file}: {e}")

            self._atomic_save(dist_file, distances)
            self._atomic_save(keys_file, keys)

        sim_threshold = 1.0 - threshold
        if mode == "faces":
            sim_threshold = max(0.20, 0.85 - (threshold * 3.0))

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

                if mode != "faces":
                    if is_seq_i and ext_j in seq_exts:
                        sim = 1.0 - (1.0 - sim) * 1.45
                    
                    if is_doc_i and ext_j in doc_exts:
                        local_threshold = max(0.70, sim_threshold - 0.05)
                        if sim >= local_threshold:
                            adj[i].append((n_idx, sim))
                        continue

                if sim >= sim_threshold:
                    adj[i].append((n_idx, sim))

        # СВЯЗНЫЕ КОМПОНЕНТЫ через union-find. Прежняя жадная группировка брала
        # i + только его ПРЯМЫХ соседей и помечала их visited, из-за чего теряла
        # транзитивные дубли (A≈B, B≈C, но A⊀C → C осиротевал) и зависела от
        # порядка обхода (kNN-граф асимметричен: top-k у i мог не включать j,
        # хотя у j включал i). Union-find по тем же рёбрам adj даёт корректные
        # компоненты независимо от направления ребра и порядка.
        parent = list(range(len(valid_file_data)))

        def _find(x):
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:  # path compression
                parent[x], x = root, parent[x]
            return root

        def _union(a, b):
            ra, rb = _find(a), _find(b)
            if ra != rb:
                parent[rb] = ra

        for i in range(len(valid_file_data)):
            for n_idx, _ in adj[i]:
                _union(i, n_idx)

        components: dict = {}
        for i in range(len(valid_file_data)):
            components.setdefault(_find(i), []).append(i)

        for cluster_indices in components.values():
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