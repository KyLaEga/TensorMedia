# ============================================================
# MODULE: core/ml/faiss_manager.py
# ============================================================
import hashlib
import os
import shutil
import numpy as np
from pathlib import Path
from typing import Optional, Tuple

import faiss

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

# ── Калибровка и каналы сходства (визуальный режим) ──────────────────────────
# Работают ДВА независимых канала:
#  1) СЕМАНТИКА (SigLIP cosine, ЦЕНТРИРОВАННАЯ — см. USE_CENTERING): ползунок %
#     мапится В РАБОЧУЮ ПОЛОСУ центр. косинуса [VISUAL_SIM_FLOOR .. +SPAN], а НЕ
#     напрямую 0..1. Это поиск «ПОХОЖИХ». (Прямой 0..1 на центр. шкале не работал:
#     дефолт 0.88 выше даже дублей → находилось почти ничего.)
#  2) ДУБЛИ (dHash, структурный хэш): пара с Хэммингом ≤ DUP_HAMMING — это ДУБЛЬ
#     (пережатие/ресайз/вотермарка/лёгкий кроп) и кластеризуется ВСЕГДА, при любом
#     ползунке. Без этого «96% не находит даже дубли»: их косинус по цельному
#     кадру 0.85–0.94 — ниже бара, а dHash их ловит надёжно.
# ВНИМАНИЕ — анизотропия SigLIP: на НИЗКИХ порогах семантический канал тащит
# однокатегорийные ложняки. Радикальное лечение — центрирование эмбеддингов
# (см. ответ разработчика); по запросу включу флагом.
COVERAGE_PENALTY_WEIGHT = 0.25   # штраф семантики за неполное покрытие кадров видео
FRAME_MATCH_FIXED = 0.55         # кадр «присутствует» в др. файле при cos ≥ этого (ЦЕНТР. шкала!)
# ГЕЙТ опоры по кадрам: видеопара без структурного (dHash) подтверждения обязана
# иметь покрытие ≥ этого. Убивает «совпал ОДИН случайный кадр» — главную причину
# «непохожих файлов даже на 100%». Несвязанные пары имеют покрытие ≈0, реальные
# похожие — ≥0.3 (несколько совпавших кадров); структурные дубли освобождены.
MIN_FRAME_COVERAGE = 0.30
# dHash-подтверждение даёт СКИДКУ к порогу косинуса (структурный дубль почти
# идентичен, но по цельному кадру косинус лишь 0.85–0.94). ГРАДУИРОВАНО: сильное
# совпадение контуров (Хэмминг ≤ STRONG) — большая скидка (почти наверняка дубль);
# слабое (≤ WEAK) — малая (может быть СЛУЧАЙНЫМ совпадением градиентов, поэтому на
# высоком ползунке такие пары почти не проходят → нет «шлака» на 100%).
DUP_HAMMING_STRONG = 4
DUP_HAMMING_WEAK = 6
DUP_DISCOUNT_STRONG = 0.18
DUP_DISCOUNT_WEAK = 0.05
DHASH_BLANK_MIN_BITS = 5         # кадры с <5 или >59 бит — пустые/монотонные, пропуск
DHASH_BLANK_MAX_BITS = 59
# ── Центрирование эмбеддингов (коррекция анизотропии SigLIP) ─────────────────
# SigLIP-эмбеддинги лежат в узком конусе → косинус НЕсвязанных картинок высок
# («мусор до 0.95»), и порог почти не отличает «похожие» от «несвязанных».
# Центрирование вычитает СРЕДНИЙ вектор корпуса и пере-нормирует: конус
# «распрямляется», несвязанные уходят к ~0, похожие явно выделяются — порог
# становится по-настоящему различающим. Канал ДУБЛЕЙ (dHash) от этого не зависит.
# ВНИМАНИЕ: меняет ШКАЛУ косинуса — ползунок теперь = ЦЕНТРИРОВАННЫЙ косинус («чем
# выше, тем строже» сохраняется, но числа иные: похожие ≈ 0.35–0.65, дубли ≈ 0.7+).
# A/B: поставьте False, чтобы вернуть прежний «сырой» косинус.
USE_CENTERING = True

# Калибровка ползунка для ЦЕНТРИРОВАННОЙ шкалы (эмпирика по реальному корпусу,
# 740 файлов): несвязанные пары ≈ 0.14 (p95≈0.41), близкие дубли ≈ 0.82+. Рабочая
# полоса — НЕ [0..1], а ≈[0.30..0.75]. Ползунок мапится в неё линейно:
#   sim_threshold = VISUAL_SIM_FLOOR + (ползунок/100) * VISUAL_SIM_SPAN
# Контрольные точки: 0%→0.30 (всё родственное), 35%→0.46 (похожие, без ложняков),
# 65%→0.59 (дубли+близкие, дефолт), 92%→0.71 (почти идентичные).
VISUAL_SIM_FLOOR = 0.30
VISUAL_SIM_SPAN = 0.45

# Faces (FaceNet/vggface2) — ОТДЕЛЬНАЯ шкала, но ТЕПЕРЬ ТОЖЕ центрированная
# (см. USE_CENTERING ниже). Эмпирика: без центрирования AUC «тот же/разные»≈0.38
# (хуже случайного — анизотропия FaceNet на этом контенте), с центрированием≈0.625.
# В ЦЕНТР. шкале: «разные» peak p95≈0.40/p99≈0.52, «тот же» p50≈0.40.
# Бар 0.42 (0%) … 0.72 (100%): дефолт (65%)→0.615 заметно режет ложняки.
FACE_SIM_FLOOR = 0.42
FACE_SIM_SPAN = 0.30


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

    def build_index_and_search(self, vectors: np.ndarray, k: int, mode: str) -> Tuple[np.ndarray, np.ndarray]:
        vectors = np.ascontiguousarray(vectors)
        n, dim = vectors.shape
        vectors = vectors.astype(np.float32)
        if n > ANN_THRESHOLD:
            index = faiss.IndexHNSWFlat(dim, 32, faiss.METRIC_INNER_PRODUCT)
            index.hnsw.efSearch = max(64, k)
        else:
            index = faiss.IndexFlatIP(dim)
        index.add(vectors)
        distances, keys = index.search(vectors, k)
        return distances, keys

    @staticmethod
    def _center_norm(vecs, mean_vec):
        """Центрирование (вычесть mean_vec, если задан) + L2-перенормировка.
        mean_vec=None → только перенормировка (поведение без центрирования)."""
        v = vecs.astype(np.float32)
        if mean_vec is not None:
            v = v - mean_vec
        n = np.linalg.norm(v, axis=1, keepdims=True)
        n[n == 0] = 1.0
        return (v / n).astype(np.float32)

    @staticmethod
    def _min_dhash_distance(dhash_i, dhash_j):
        """Мин. расстояние Хэмминга между ПРИГОДНЫМИ dHash-кадрами двух файлов.
        None — пригодных пар нет (нет хэшей либо все кадры монотонные/пустые)."""
        if dhash_i is None or dhash_j is None or len(dhash_i) == 0 or len(dhash_j) == 0:
            return None
        usable_i = [d for d in dhash_i
                    if DHASH_BLANK_MIN_BITS <= int(np.unpackbits(d).sum()) <= DHASH_BLANK_MAX_BITS]
        usable_j = [d for d in dhash_j
                    if DHASH_BLANK_MIN_BITS <= int(np.unpackbits(d).sum()) <= DHASH_BLANK_MAX_BITS]
        if not usable_i or not usable_j:
            return None
        best = 64
        for da in usable_i:
            for db in usable_j:
                hd = int(np.unpackbits(da ^ db).sum())
                if hd < best:
                    best = hd
        return best

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

        # Инкрементальный md5 без построения единой мульти-мегабайтной строки на
        # больших библиотеках (utf-8 конкатенации байт-идентична join+encode →
        # дисковый кэш FAISS прежних подписей остаётся валидным).
        _sig = hashlib.md5(usedforsecurity=False)
        for item in valid_file_data:
            _sig.update(f"{item['path']}_{item['size']}_{item['mtime']}".encode("utf-8"))
        state_signature = _sig.hexdigest()
        
        cache_dir = self.cache_dir()
        # Центрирование меняет дистанции FAISS → тег в имени кэша разделяет
        # центрированные ('c1') и «сырые» ('raw') подписи (смена флага → свежий кэш).
        # Теперь центрируем И лица: эмпирически это поднимает разделимость
        # «тот же/разные» (AUC 0.38→0.625) — см. FACE_SIM_FLOOR.
        do_center = USE_CENTERING
        tag = "c1" if do_center else "raw"
        dist_file = cache_dir / f"{mode}_{tag}_{state_signature}_dist.npy"
        keys_file = cache_dir / f"{mode}_{tag}_{state_signature}_keys.npy"
        mean_file = cache_dir / f"{mode}_{tag}_{state_signature}_mean.npy"

        n_items = len(valid_file_data)
        file_mapping = []
        flat_vectors = []
        
        for i, item in enumerate(valid_file_data):
            v = item["vector"]
            if v is not None and len(v) > 0:
                for row_idx in range(len(v)):
                    flat_vectors.append(v[row_idx])
                    file_mapping.append(i)
                    
        if not flat_vectors:
            return clusters

        k = min(MAX_NEIGHBORS, len(flat_vectors))
        cache_valid = False
        distances = keys = None
        mean_vec = None   # средний вектор корпуса (для центрирования матрицы ниже)

        if dist_file.exists() and keys_file.exists():
            try:
                tmp_keys = np.load(keys_file)
                if tmp_keys.shape[1] >= k:
                    distances = np.load(dist_file)[:, :k]
                    keys = tmp_keys[:, :k]
                    if do_center and mean_file.exists():
                        mean_vec = np.load(mean_file)
                    cache_valid = (not do_center) or (mean_vec is not None)
            except Exception as e:
                from utils.logger import auditor
                auditor.warning(f"Failed to load FAISS cache: {e}")

        if not cache_valid:
            flat = np.stack(flat_vectors).astype(np.float32)
            if do_center:
                # Средний вектор корпуса = ось анизотропного конуса; вычитаем его.
                mean_vec = flat.mean(axis=0, keepdims=True).astype(np.float32)
            vectors = np.ascontiguousarray(self._center_norm(flat, mean_vec))
            distances, keys = self.build_index_and_search(vectors, k, mode)

            keep = {dist_file.name, keys_file.name, mean_file.name}
            for old_file in list(cache_dir.glob(f"{mode}_*.npy")) + list(cache_dir.glob(f"{mode}_*.npy.tmp")):
                if old_file.name in keep: continue
                try: os.remove(old_file)
                except Exception: pass

            self._atomic_save(dist_file, distances)
            self._atomic_save(keys_file, keys)
            if do_center and mean_vec is not None:
                self._atomic_save(mean_file, mean_vec)

        # ── Порог и радар кандидатов ──────────────────────────────────────
        if mode == "faces":
            sim_threshold = FACE_SIM_FLOOR + threshold * FACE_SIM_SPAN
            prefilter_sim = sim_threshold
        else:
            # ЦЕНТРИРОВАННАЯ шкала: ползунок мапится в рабочую полосу
            # [FLOOR .. FLOOR+SPAN]. В сыром режиме (A/B, do_center=False) остаётся
            # прежняя прямая шкала (ползунок = косинус напрямую 0..1).
            if do_center:
                sim_threshold = VISUAL_SIM_FLOOR + threshold * VISUAL_SIM_SPAN
            else:
                sim_threshold = threshold
            # Кандидат имеет шанс пройти, только если его косинус не ниже
            # (порог − максимальная dHash-скидка). Ниже рассматривать незачем.
            prefilter_sim = max(0.0, sim_threshold - DUP_DISCOUNT_STRONG)

        # Stage 2: покадровый кросс-матчинг FAISS → множество кандидатных пар.
        candidate_pairs = set()
        for frame_idx in range(len(file_mapping)):
            file_i = file_mapping[frame_idx]
            for j in range(k):
                n_frame_idx = keys[frame_idx, j]
                if n_frame_idx == -1:
                    continue
                file_j = file_mapping[n_frame_idx]
                if file_i == file_j:
                    continue
                if distances[frame_idx, j] < prefilter_sim:
                    continue
                candidate_pairs.add(tuple(sorted((file_i, file_j))))

        # Stage 3: ребро — если пара семантически похожа (косинус ≥ ползунок) ИЛИ
        # дубль, подтверждённый dHash со скидкой к порогу (градуированно, см. ниже).
        adj = {i: set() for i in range(n_items)}
        edge_weights = {}
        for (i, j) in candidate_pairs:
            vecs_i = valid_file_data[i].get("vector")
            vecs_j = valid_file_data[j].get("vector")
            if vecs_i is None or vecs_j is None or len(vecs_i) == 0 or len(vecs_j) == 0:
                continue

            # Те же центрирование+нормировка, что и для FAISS-индекса (mean_vec
            # общий), иначе матрица и радар жили бы в разных шкалах косинуса.
            ni = self._center_norm(vecs_i, mean_vec)
            nj = self._center_norm(vecs_j, mean_vec)
            sim_matrix = np.dot(ni, nj.T)
            peak_sim = float(sim_matrix.max())           # лучшая пара кадров

            # Покрытие — для ВИДЕО (visual, многокадровые): доля кадров короткого
            # файла, нашедших пару с cos ≥ FRAME_MATCH_FIXED. Штраф за неполное
            # покрытие давит видеопары, совпавшие лишь одним «случайным» кадром.
            # Картинки (1 кадр) и ЛИЦА покрытия не считают — там решает peak.
            is_multi = mode == "visual" and not (len(vecs_i) == 1 and len(vecs_j) == 1)
            if is_multi:
                mi = np.max(sim_matrix, axis=1)
                mj = np.max(sim_matrix, axis=0)
                matched = min(int((mi >= FRAME_MATCH_FIXED).sum()),
                              int((mj >= FRAME_MATCH_FIXED).sum()))
                coverage = matched / max(1, min(len(vecs_i), len(vecs_j)))
                semantic_sim = peak_sim - (1.0 - coverage) * COVERAGE_PENALTY_WEIGHT
            else:
                coverage = 1.0
                semantic_sim = peak_sim

            # dHash считаем ЗАРАНЕЕ — нужен и для гейта опоры, и для скидки.
            mh = (self._min_dhash_distance(valid_file_data[i].get("dhash"),
                                           valid_file_data[j].get("dhash"))
                  if mode == "visual" else None)
            structural = mh is not None and mh <= DUP_HAMMING_STRONG

            # ГЕЙТ ОПОРЫ: видеопара БЕЗ структурного подтверждения обязана иметь
            # покрытие ≥ MIN_FRAME_COVERAGE. Это и убирает «непохожие на 100%»
            # (совпал один кадр → покрытие ~0 → отказ), не трогая реальные похожие
            # (несколько совпавших кадров) и структурные дубли (освобождены).
            if is_multi and not structural and coverage < MIN_FRAME_COVERAGE:
                accept = False
            elif semantic_sim >= sim_threshold:
                accept = True
            elif structural:
                accept = semantic_sim >= sim_threshold - DUP_DISCOUNT_STRONG
            elif mh is not None and mh <= DUP_HAMMING_WEAK:
                accept = semantic_sim >= sim_threshold - DUP_DISCOUNT_WEAK
            else:
                accept = False

            if accept:
                edge_weights[(i, j)] = semantic_sim
                edge_weights[(j, i)] = semantic_sim
                adj[i].add(j)
                adj[j].add(i)

        # Stage 4: ЛИДЕР-КЛАСТЕРИЗАЦИЯ (без транзитивных цепочек).
        # Кластер = эталон (наибольший по размеру неназначенный файл) + его ПРЯМЫЕ
        # соседи по графу. Это исключает перколяцию single-linkage: гигантский
        # кластер мог собраться из цепочки A≈B≈C≈… с НЕсвязанными концами (отсюда
        # «на 82% вдруг 200–300 объектов»). Здесь член кластера обязан быть похож
        # НА ЭТАЛОН, а не транзитивно через цепочку.
        assigned = set()
        refined_clusters = []
        for base_idx in sorted(range(n_items),
                               key=lambda idx: valid_file_data[idx]["size"], reverse=True):
            if base_idx in assigned:
                continue
            members = [base_idx]
            assigned.add(base_idx)
            for n_idx in adj[base_idx]:
                if n_idx not in assigned:
                    members.append(n_idx)
                    assigned.add(n_idx)
            if len(members) < 2:
                continue
            cluster = []
            for idx in members:
                item = valid_file_data[idx]
                item["similarity"] = 1.0 if idx == base_idx else float(edge_weights.get((base_idx, idx), 0.0))
                cluster.append(item)
            cluster.sort(key=lambda x: x["similarity"], reverse=True)
            refined_clusters.append(cluster)

        return refined_clusters
