import os
import gc
import sqlite3
import time
import zipfile
import io
import numpy as np
import torch
import cv2
import faiss
import concurrent.futures
import blake3
from PIL import Image
from pathlib import Path
from transformers import AutoProcessor, SiglipVisionModel

from utils.env_config import get_app_data_dir, get_models_dir
from utils.i18n import translator

# Блокировка скрытых мультипроцессов для защиты от Semaphore Leak
os.environ["LOKY_MAX_CPU_COUNT"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"

class SmartClusterEngine:
    def __init__(self):
        self.device = self._detect_device()
        self.scan_mode = None
        self.processor = None
        self.model = None
        self.mtcnn = None
        self.resnet = None
        self.current_file_data = []
        self.is_paused = False
        self.is_stopped = False

    def _detect_device(self) -> str:
        """Универсальный сканер аппаратного ускорения."""
        if torch.cuda.is_available():
            # Покрывает NVIDIA (Windows/Linux) и AMD ROCm (Linux)
            return "cuda"
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            # Покрывает Apple Silicon (macOS)
            return "mps"
        # Для AMD на Windows требуется torch-directml, иначе безопасный фоллбэк на CPU
        return "cpu"

    def _clear_vram(self):
        """Кросс-платформенная очистка видеопамяти."""
        if self.device == "cuda":
            torch.cuda.empty_cache()
            if hasattr(torch.cuda, 'ipc_collect'):
                torch.cuda.ipc_collect()
        elif self.device == "mps":
            torch.mps.empty_cache()
        gc.collect()

    def load_models(self, mode="visual"):
        if self.scan_mode == mode:
            return
            
        self.scan_mode = mode
        
        if self.processor is not None: del self.processor
        if self.model is not None: del self.model
        if self.mtcnn is not None: del self.mtcnn
        if self.resnet is not None: del self.resnet
        
        self.processor = None
        self.model = None
        self.mtcnn = None
        self.resnet = None
        
        self._clear_vram()

        if mode == "visual":
            siglip_local_path = str(get_models_dir() / "siglip-base-patch16-224")
            self.processor = AutoProcessor.from_pretrained(siglip_local_path, local_files_only=True)
            try:
                self.model = SiglipVisionModel.from_pretrained(siglip_local_path, local_files_only=True).to(self.device)
            except Exception:
                self.device = "cpu"
                self.model = SiglipVisionModel.from_pretrained(siglip_local_path, local_files_only=True).to(self.device)
                
        elif mode == "faces":
            os.environ["TORCH_HOME"] = str(get_models_dir() / "torch")
            try:
                from facenet_pytorch import MTCNN, InceptionResnetV1
                self.mtcnn = MTCNN(keep_all=False, device='cpu')
                self.resnet = InceptionResnetV1(pretrained='vggface2').eval().to(self.device)
            except ImportError:
                print("[FATAL] Модуль facenet-pytorch не установлен.")
                self.scan_mode = "error"

    def _compute_fast_hash(self, file_path: Path) -> str:
        try:
            with open(file_path, 'rb') as f:
                b3 = blake3.blake3()
                chunk = f.read(1024 * 1024)
                if not chunk: return "EMPTY"
                b3.update(chunk)
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size > 1024 * 1024:
                    f.seek(-1024 * 1024, os.SEEK_END)
                    b3.update(f.read(1024 * 1024))
                return b3.hexdigest()
        except Exception:
            return f"FAIL_{file_path.name}"

    def _compute_vector_batch(self, images):
        if not images: return []
        
        if self.scan_mode == "faces":
            results = [None] * len(images)
            if self.mtcnn is None or self.resnet is None: return results
            
            try:
                for i, img in enumerate(images):
                    try:
                        face = self.mtcnn(img)
                        if face is not None:
                            with torch.no_grad():
                                emb = self.resnet(face.unsqueeze(0).to(self.device))
                                emb_norm = torch.nn.functional.normalize(emb, p=2, dim=-1)
                                results[i] = emb_norm.cpu().numpy().astype(np.float32)[0]
                    except Exception: pass
            except Exception as e:
                print(f"[FACE ERROR] Ошибка генерации эмбеддинга лица: {e}")
            return results
            
        elif self.scan_mode == "visual":
            try:
                def run_on_device(dev):
                    all_f_norms = []
                    chunk_size = 32 
                    for i in range(0, len(images), chunk_size):
                        chunk = images[i:i+chunk_size]
                        inputs = self.processor(images=chunk, return_tensors="pt")
                        inputs = {k: v.to(dev) for k, v in inputs.items()}
                        with torch.no_grad():
                            f = self.model.to(dev)(**inputs).pooler_output
                            f_norm = torch.nn.functional.normalize(f, p=2, dim=-1)
                            all_f_norms.extend(f_norm.cpu().numpy().astype(np.float32))
                    return all_f_norms

                try:
                    return run_on_device(self.device)
                except Exception as e:
                    print(f"[NPU ERROR] Сбой H/W: {e}. Переход на CPU...")
                    self.device = "cpu"
                    return run_on_device("cpu")
            except Exception as e:
                print(f"[FATAL NPU ERROR] {e}")
                return [None] * len(images)
        
        return [None] * len(images)

    def simple_search(self, query: str):
        if not self.current_file_data: return []
        query = query.lower()
        results = []
        for item in self.current_file_data:
            name = Path(item['path']).name.lower()
            if query in name:
                res_item = item.copy()
                res_item['similarity'] = 1.0
                results.append(res_item)
        return results

    def _calculate_optical_sharpness(self, frame):
        try:
            if len(frame.shape) == 3: gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            else: gray = frame
            lap = cv2.Laplacian(gray, cv2.CV_64F)
            h, w = lap.shape
            grid_size = 4
            bh, bw = max(1, h // grid_size), max(1, w // grid_size)
            max_var = 0.0
            for i in range(grid_size):
                for j in range(grid_size):
                    block = lap[i*bh:(i+1)*bh, j*bw:(j+1)*bw]
                    var = float(block.var()) 
                    if var > max_var: max_var = var
            return float(max_var)
        except Exception:
            return 0.0

    def _process_file_io(self, args):
        while self.is_paused and not self.is_stopped: time.sleep(0.1)
        if self.is_stopped: return None

        file_path, size, file_hash, vector = args
        res, dur, codec, sharpness, fps_val = "", 0.0, "", 0.0, 0.0
        img_for_model = [] 
        ext = file_path.suffix.lower()

        target_size = (512, 512) if self.scan_mode == "faces" else (224, 224)

        try:
            if ext in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}:
                cap = cv2.VideoCapture(str(file_path), cv2.CAP_AVFOUNDATION)
                if not cap.isOpened():
                    cap = cv2.VideoCapture(str(file_path)) 
                
                if cap.isOpened():
                    res = f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}"
                    fps_val = float(cap.get(cv2.CAP_PROP_FPS))
                    total_frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                    if fps_val > 0: dur = float(total_frames / fps_val)
                    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
                    codec = "".join([chr((fourcc >> 8 * i) & 0xFF) for i in range(4)]).strip().lower()
                    
                    check_points = [0.20, 0.40, 0.60, 0.80]
                    max_sharp = 0.0
                    for cp in check_points:
                        target = int(total_frames * cp) if total_frames > 0 else 0
                        cap.set(cv2.CAP_PROP_POS_FRAMES, target)
                        ret, frame = cap.read()
                        if ret and frame.mean() > 15.0:
                            if vector is None:
                                pil_img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                                img_for_model.append(pil_img.resize(target_size, Image.Resampling.BICUBIC))
                            h, w = frame.shape[:2]
                            if max(w, h) > 256:
                                scale = 256.0 / max(w, h)
                                frame_sm = cv2.resize(frame, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                            else: frame_sm = frame
                            sharp = self._calculate_optical_sharpness(cv2.cvtColor(frame_sm, cv2.COLOR_BGR2GRAY))
                            if sharp > max_sharp: max_sharp = sharp
                    sharpness = float(max_sharp)
                cap.release()
                    
            elif ext in {'.jpg', '.png', '.webp', '.bmp', '.heic', '.jpeg'}:
                with Image.open(file_path) as img: 
                    res = f"{img.width}x{img.height}"
                    if vector is None: 
                        img_for_model.append(img.convert("RGB").resize(target_size, Image.Resampling.BICUBIC))
                    
                    img.thumbnail((256, 256))
                    sharpness = self._calculate_optical_sharpness(np.array(img.convert('L')))
            
            elif ext == '.gif':
                try:
                    with Image.open(file_path) as img:
                        tot_frames = getattr(img, "n_frames", 1)
                        if tot_frames > 1:
                            check_points = [0.20, 0.40, 0.60, 0.80] if tot_frames > 3 else [0.0]
                            max_sharp = 0.0
                            for cp in check_points:
                                target_frame = min(max(0, int(tot_frames * cp)), tot_frames - 1)
                                img.seek(target_frame)
                                frame_pil = img.convert("RGB")
                                if not res: res = f"{frame_pil.width}x{frame_pil.height}"
                                if vector is None: 
                                    img_for_model.append(frame_pil.resize(target_size, Image.Resampling.BICUBIC))
                                frame_pil.thumbnail((256, 256))
                                sharp = self._calculate_optical_sharpness(np.array(frame_pil.convert('L')))
                                if sharp > max_sharp: max_sharp = sharp
                            sharpness = float(max_sharp)
                        else:
                            frame_pil = img.convert("RGB")
                            res = f"{frame_pil.width}x{frame_pil.height}"
                            if vector is None: 
                                img_for_model.append(frame_pil.resize(target_size, Image.Resampling.BICUBIC))
                            frame_pil.thumbnail((256, 256))
                            sharpness = self._calculate_optical_sharpness(np.array(frame_pil.convert('L')))
                except Exception: pass
            
            elif ext == '.cbz':
                with zipfile.ZipFile(file_path, 'r') as z:
                    names = sorted([n for n in z.namelist() if n.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
                    if names:
                        total_pages = len(names)
                        check_points = [0.0, 0.10, 0.30, 0.50] if total_pages > 4 else [0.0]
                        max_sharp = -1.0
                        best_img_for_model = None
                        
                        for cp in check_points:
                            idx = int(total_pages * cp) if total_pages > 0 else 0
                            with z.open(names[idx]) as f:
                                img = Image.open(io.BytesIO(f.read())).convert("RGB")
                                if not res: res = f"{img.width}x{img.height}"
                                
                                img_thumb = img.copy()
                                img_thumb.thumbnail((256, 256))
                                sharp = self._calculate_optical_sharpness(np.array(img_thumb.convert('L')))
                                
                                if sharp > max_sharp: 
                                    max_sharp = sharp
                                    best_img_for_model = img.resize(target_size, Image.Resampling.BICUBIC)
                        
                        if best_img_for_model and vector is None:
                            img_for_model.append(best_img_for_model)
                        sharpness = float(max_sharp)

            elif ext == '.pdf':
                try:
                    import fitz 
                    with fitz.open(str(file_path)) as doc:
                        total_pages = len(doc)
                        check_points = [0.0, 0.30, 0.60] if total_pages > 3 else [0.0]
                        max_sharp = -1.0
                        best_img_for_model = None
                        
                        for cp in check_points:
                            page_num = int(total_pages * cp)
                            if page_num >= total_pages: page_num = total_pages - 1
                            
                            page = doc.load_page(page_num)
                            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                            
                            if not res: res = f"{img.width}x{img.height}"
                            
                            img_thumb = img.copy()
                            img_thumb.thumbnail((256, 256))
                            sharp = self._calculate_optical_sharpness(np.array(img_thumb.convert('L')))
                            
                            if sharp > max_sharp: 
                                max_sharp = sharp
                                best_img_for_model = img.resize(target_size, Image.Resampling.BICUBIC)
                        
                        if best_img_for_model and vector is None:
                            img_for_model.append(best_img_for_model)
                        sharpness = float(max_sharp)
                except Exception: pass
        except: pass

        return {
            "path": str(file_path), "size": size, "phash": file_hash, "vector": vector, 
            "img_for_model": img_for_model, "res": res, "dur": dur, 
            "codec": codec, "sharpness": sharpness, "fps": fps_val
        }

    def extract_features(self, target_dirs: list, allowed_exts: set = None, progress_callback=None):
        self.is_paused = False
        self.is_stopped = False
        self.current_file_data = []
        
        files = []
        for d in target_dirs:
            p_dir = Path(d)
            if p_dir.is_dir():
                c_files = [f for f in p_dir.rglob("*") if f.is_file() and not f.name.startswith('.')]
                if allowed_exts: c_files = [f for f in c_files if f.suffix.lower() in allowed_exts]
                files.extend(c_files)
                
        if not files: return self.current_file_data
        
        app_dir = get_app_data_dir() / "db"
        app_dir.mkdir(parents=True, exist_ok=True)
        db_path = app_dir / f"meta_{self.scan_mode}.db"
        
        meta_cache = {}
        try:
            conn = sqlite3.connect(str(db_path), timeout=10.0)
            conn.execute('''CREATE TABLE IF NOT EXISTS meta 
                            (phash TEXT PRIMARY KEY, size INTEGER, res TEXT, dur REAL, codec TEXT, sharpness REAL, fps REAL, vector BLOB)''')
            
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM meta")
            for row in cursor.fetchall():
                vec_blob = row[7] if len(row) > 7 else None
                vec = np.frombuffer(vec_blob, dtype=np.float32) if vec_blob else None
                if vec is not None and vec.size == 0: vec = None
                meta_cache[row[0]] = {
                    'size': row[1], 'res': row[2], 'dur': row[3], 
                    'codec': row[4], 'sharpness': row[5], 'fps': row[6],
                    'vector': vec
                }
        except Exception as e:
            print(f"Ошибка загрузки локальной БД: {e}")

        if progress_callback: progress_callback(0, len(files), translator.tr("scan_io"))
        
        tasks = []
        all_results = []
        
        for idx, file_path in enumerate(files):
            if self.is_stopped: break
            try:
                size = file_path.stat().st_size
                if size == 0: continue
                
                file_hash = self._compute_fast_hash(file_path)
                vector = meta_cache.get(file_hash, {}).get('vector')
                file_str = str(file_path)
                
                if vector is not None and file_hash in meta_cache:
                    c_m = meta_cache[file_hash]
                    if c_m.get('size') == size:
                        all_results.append({
                            "path": file_str, "size": size, "phash": file_hash, "vector": vector,
                            "img_for_model": [], "res": c_m.get('res', ''), "dur": c_m.get('dur', 0.0),
                            "codec": c_m.get('codec', ''), "sharpness": c_m.get('sharpness', 0.0), 
                            "fps": c_m.get('fps', 0.0)
                        })
                        if progress_callback and idx % 10 == 0:
                            progress_callback(len(all_results), len(files), translator.tr("scan_cache"))
                        continue 
                tasks.append((file_path, size, file_hash, vector))
            except Exception: continue

        chunk_size = 256
        batch_size = 32 
        bypassed_count = len(all_results)
        
        for chunk_start in range(0, len(tasks), chunk_size):
            if self.is_stopped: break
            chunk_tasks = tasks[chunk_start : chunk_start + chunk_size]
            chunk_results = []
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                futures = {executor.submit(self._process_file_io, task): task for task in chunk_tasks}
                for future in concurrent.futures.as_completed(futures):
                    while self.is_paused and not self.is_stopped: time.sleep(0.1)
                    if self.is_stopped: 
                        for f in futures: f.cancel() 
                        break
                    res = future.result()
                    if res: chunk_results.append(res)
                    if progress_callback:
                        current_prog = bypassed_count + chunk_start + len(chunk_results)
                        progress_callback(current_prog, len(files), f"{translator.tr('scan_npu')}{Path(res['path']).name}")

            if self.is_stopped: break
            needs_vector = [r for r in chunk_results if r['vector'] is None and len(r['img_for_model']) > 0]
            
            for i in range(0, len(needs_vector), batch_size):
                while self.is_paused and not self.is_stopped: time.sleep(0.1)
                if self.is_stopped: break
                
                batch = needs_vector[i:i+batch_size]
                flat_images = []
                counts = []
                for b in batch:
                    imgs = b['img_for_model']
                    flat_images.extend(imgs)
                    counts.append(len(imgs))
                
                flat_vectors = self._compute_vector_batch(flat_images)
                
                idx = 0
                for b, count in zip(batch, counts):
                    file_vecs = flat_vectors[idx:idx+count]
                    idx += count
                    valid_vecs = [v for v in file_vecs if v is not None]
                    if valid_vecs:
                        avg_vec = np.mean(valid_vecs, axis=0)
                        avg_vec = avg_vec / np.linalg.norm(avg_vec)
                        b['vector'] = avg_vec
                    else: b['vector'] = None
                
                del flat_images
                del batch
                
            self._clear_vram()
            for r in chunk_results: r['img_for_model'] = [] 
            all_results.extend(chunk_results)

        if self.is_stopped: return []
        if progress_callback: progress_callback(len(files), len(files), translator.tr("scan_faiss"))

        for r in all_results:
            if r['vector'] is not None:
                self.current_file_data.append({
                    "path": r['path'], "phash": r['phash'], "vector": r['vector'],
                    "size": r['size'], "resolution": r['res'],
                    "duration": r['dur'], "codec": r['codec'], "sharpness": r['sharpness'], 
                    "fps": r['fps'], "mtime": 0 
                })

        try:
            conn = sqlite3.connect(str(db_path), timeout=10.0)
            cursor = conn.cursor()
            for r in all_results:
                if r['vector'] is not None:
                    vec_blob = r['vector'].tobytes()
                    cursor.execute("INSERT OR REPLACE INTO meta VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                        (str(r['phash']), int(r['size']), str(r['res']), float(r['dur']), 
                         str(r['codec']), float(r['sharpness']), float(r['fps']), vec_blob))
            conn.commit()
            conn.close()
        except Exception as e: pass
        
        return self.current_file_data

    def build_clusters(self, threshold: float):
        clusters = []
        file_data = self.current_file_data.copy()
        if not file_data: return clusters

        if len(file_data) > 1:
            vectors = np.vstack([item["vector"] for item in file_data]).astype(np.float32)
            faiss.normalize_L2(vectors)
            
            dim = vectors.shape[1]
            index = faiss.IndexFlatIP(dim) 
            index.add(vectors)
            
            sim_threshold = 1.0 - threshold
            lims, D, I = index.range_search(vectors, float(sim_threshold))
            
            seq_exts = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v', '.gif'}
            doc_exts = {'.cbz', '.pdf'}
            adj = {i: [] for i in range(len(file_data))}
            
            for i in range(len(file_data)):
                ext_i = Path(file_data[i]["path"]).suffix.lower()
                is_seq_i = ext_i in seq_exts
                is_doc_i = ext_i in doc_exts
                start, end = lims[i], lims[i+1]
                
                for j in range(start, end):
                    n_idx = I[j]
                    if i == n_idx: continue
                    sim = float(D[j])
                    ext_j = Path(file_data[n_idx]["path"]).suffix.lower()
                    is_seq_j = ext_j in seq_exts
                    is_doc_j = ext_j in doc_exts
                    
                    if is_seq_i and is_seq_j: sim = 1.0 - (1.0 - sim) * 1.45
                    
                    if is_doc_i and is_doc_j:
                        local_threshold = min(0.98, sim_threshold + 0.05)
                        if sim > 0.80: sim = (sim - 0.80) / 0.20
                        else: sim = 0.0
                        if sim >= local_threshold: adj[i].append((n_idx, sim))
                        continue

                    if sim >= sim_threshold: adj[i].append((n_idx, sim))

            visited = set()
            for i in range(len(file_data)):
                if i not in visited:
                    cluster_indices = [i]
                    visited.add(i)
                    for n_idx, _ in adj[i]:
                        if n_idx not in visited:
                            visited.add(n_idx)
                            cluster_indices.append(n_idx)
                    
                    if len(cluster_indices) > 1:
                        clusters.append([file_data[idx] for idx in cluster_indices])
                
        refined_clusters = []
        for cluster in clusters:
            base_item = max(cluster, key=lambda x: x['size'])
            base_vec = base_item['vector']
            for item in cluster:
                if item == base_item: item['similarity'] = 1.0
                else:
                    sim = np.dot(base_vec, item['vector'])
                    item['similarity'] = max(0.0, float(sim))
            cluster.sort(key=lambda x: x['similarity'], reverse=True)
            refined_clusters.append(cluster)
            
        return refined_clusters