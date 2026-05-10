# ============================================================
# MODULE: core/ml/cluster_engine.py
# ============================================================
import os
import time
import zipfile
import numpy as np
import torch
import cv2
import faiss
import hashlib
import blake3
import psutil
from PIL import Image
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import shared_memory

cv2.setNumThreads(0)

from transformers import AutoProcessor, SiglipVisionModel
from utils.env_config import get_app_data_dir, get_models_dir
from utils.logger import auditor
from core.profiler import HardwareProfiler
from core.db.vector_cache import VectorCache

os.environ["LOKY_MAX_CPU_COUNT"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE" 

def calculate_optical_sharpness(frame: np.ndarray) -> float:
    try:
        if len(frame.shape) == 3: 
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        else: 
            gray = frame
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
    except Exception as e:
        auditor.warning(f"Worker Sharpness calculation error: {e}")
        return 0.0

def process_single_file_io(task_data: tuple) -> dict:
    file_path, size, mtime, file_hash, vector, scan_mode = task_data
    res, dur, codec, sharpness, fps_val = "", 0.0, "", 0.0, 0.0
    shm_blocks = [] 
    ext = file_path.suffix.lower()

    target_size = (512, 512) if scan_mode == "faces" else (224, 224)

    def _allocate_shm(img_obj):
        arr = np.array(img_obj)
        try:
            shm = shared_memory.SharedMemory(create=True, size=arr.nbytes)
            shm_arr = np.ndarray(arr.shape, dtype=arr.dtype, buffer=shm.buf)
            np.copyto(shm_arr, arr)
            shm_blocks.append({
                "name": shm.name,
                "shape": arr.shape,
                "dtype": str(arr.dtype),
                "is_shm": True
            })
            shm.close()
        except OSError:
            shm_blocks.append({
                "shape": arr.shape,
                "dtype": str(arr.dtype),
                "data": arr.tobytes(),
                "is_shm": False
            })

    try:
        if ext in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}:
            try:
                cap = cv2.VideoCapture(str(file_path), cv2.CAP_AVFOUNDATION, [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY])
                if not cap.isOpened(): cap = cv2.VideoCapture(str(file_path)) 
            except Exception:
                cap = cv2.VideoCapture(str(file_path))

            try:
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
                                h_fr, w_fr = frame.shape[:2]
                                scale_fr = min(target_size[0]/w_fr, target_size[1]/h_fr)
                                if scale_fr < 1.0:
                                    frame_res = cv2.resize(frame, (int(w_fr * scale_fr), int(h_fr * scale_fr)), interpolation=cv2.INTER_AREA)
                                else:
                                    frame_res = frame
                                _allocate_shm(cv2.cvtColor(frame_res, cv2.COLOR_BGR2RGB))

                            h, w = frame.shape[:2]
                            if max(w, h) > 256:
                                scale = 256.0 / max(w, h)
                                frame_sm = cv2.resize(frame, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                            else: frame_sm = frame
                            sharp = calculate_optical_sharpness(cv2.cvtColor(frame_sm, cv2.COLOR_BGR2GRAY))
                            if sharp > max_sharp: max_sharp = sharp
                    sharpness = float(max_sharp)
            finally:
                cap.release()
                
        elif ext in {'.jpg', '.png', '.webp', '.bmp', '.heic', '.jpeg'}:
            with Image.open(file_path) as img: 
                res = f"{img.width}x{img.height}"
                if vector is None: 
                    _allocate_shm(img.convert("RGB").resize(target_size, Image.Resampling.BICUBIC))
                img.thumbnail((256, 256))
                sharpness = calculate_optical_sharpness(np.array(img.convert('L')))
        
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
                                _allocate_shm(frame_pil.resize(target_size, Image.Resampling.BICUBIC))
                            frame_pil.thumbnail((256, 256))
                            sharp = calculate_optical_sharpness(np.array(frame_pil.convert('L')))
                            if sharp > max_sharp: max_sharp = sharp
                        sharpness = float(max_sharp)
                    else:
                        frame_pil = img.convert("RGB")
                        res = f"{frame_pil.width}x{frame_pil.height}"
                        if vector is None: 
                            _allocate_shm(frame_pil.resize(target_size, Image.Resampling.BICUBIC))
                        frame_pil.thumbnail((256, 256))
                        sharpness = calculate_optical_sharpness(np.array(frame_pil.convert('L')))
            except Exception as e: 
                auditor.warning(f"Worker GIF error {file_path}: {e}")
        
        elif ext == '.cbz':
            with zipfile.ZipFile(file_path, 'r') as z:
                names = sorted([n for n in z.namelist() if n.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
                if names:
                    if len(names) > 50:
                        step = max(1, len(names) // 50)
                        names = names[::step][:50]
                    total_pages = len(names)
                    check_points = [0.0, 0.10, 0.30, 0.50] if total_pages > 4 else [0.0]
                    max_sharp = -1.0
                    best_img_for_model = None
                    
                    for cp in check_points:
                        idx = int(total_pages * cp) if total_pages > 0 else 0
                        with z.open(names[idx]) as f:
                            with Image.open(f) as img:
                                img_rgb = img.convert("RGB")
                                if not res: res = f"{img_rgb.width}x{img_rgb.height}"
                                img_thumb = img_rgb.copy()
                                img_thumb.thumbnail((256, 256))
                                sharp = calculate_optical_sharpness(np.array(img_thumb.convert('L')))
                                if sharp > max_sharp: 
                                    max_sharp = sharp
                                    best_img_for_model = img_rgb.resize(target_size, Image.Resampling.BICUBIC)
                    
                    if best_img_for_model and vector is None:
                        _allocate_shm(best_img_for_model)
                    sharpness = float(max_sharp)

        elif ext == '.pdf':
            try:
                import fitz 
                with fitz.open(str(file_path)) as doc:
                    total_pages = min(len(doc), 50)
                    check_points = [0.0, 0.30, 0.60] if total_pages > 3 else [0.0]
                    max_sharp = -1.0
                    best_img_for_model = None
                    
                    for cp in check_points:
                        page_num = int(total_pages * cp)
                        if page_num >= total_pages: page_num = total_pages - 1
                        page = doc.load_page(page_num)
                        pix = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5))
                        with Image.frombytes("RGB", [pix.width, pix.height], pix.samples) as img:
                            if not res: res = f"{img.width}x{img.height}"
                            img_thumb = img.copy()
                            img_thumb.thumbnail((256, 256))
                            sharp = calculate_optical_sharpness(np.array(img_thumb.convert('L')))
                            if sharp > max_sharp: 
                                max_sharp = sharp
                                best_img_for_model = img.resize(target_size, Image.Resampling.BICUBIC)
                    if best_img_for_model and vector is None:
                        _allocate_shm(best_img_for_model)
                    sharpness = float(max_sharp)
            except Exception as e: 
                auditor.warning(f"Worker PDF error {file_path}: {e}")

    except Exception as e:
        auditor.warning(f"Worker I/O Error for {file_path}: {e}")
        shm_blocks.clear()

    return {
        "path": str(file_path), "size": size, "mtime": mtime, "phash": file_hash, 
        "vector": vector, "shm_blocks": shm_blocks, "res": res, 
        "dur": dur, "codec": codec, "sharpness": sharpness, "fps": fps_val
    }

class SmartClusterEngine:
    def __init__(self):
        self.device = HardwareProfiler.get_device()
        self.scan_mode = None
        self.processor = None
        self.model = None
        self.mtcnn = None
        self.resnet = None
        self.current_file_data = []
        self.is_paused = False
        self.is_stopped = False

    def unload_models(self):
        if self.processor is not None: del self.processor
        if self.model is not None: del self.model
        if self.mtcnn is not None: del self.mtcnn
        if self.resnet is not None: del self.resnet
        
        self.processor = None
        self.model = None
        self.mtcnn = None
        self.resnet = None
        self.scan_mode = None
        
        HardwareProfiler.enforce_garbage_collection(threshold_mb=0.0, force=True) 
        auditor.info(f"Models unloaded. Device ({self.device.type}) resources released.")

    def load_models(self, mode="visual"):
        if self.scan_mode == mode and self.model is not None: return
        
        self.scan_mode = mode
        self.unload_models()
        self.scan_mode = mode

        auditor.info(f"Loading weights into {self.device.type} for mode: {mode}")

        if mode == "visual":
            siglip_local_path = str(get_models_dir() / "siglip-base-patch16-224")
            self.processor = AutoProcessor.from_pretrained(siglip_local_path, local_files_only=True)
            target_dtype = torch.float16 if self.device.type in ("cuda", "mps") else torch.float32
            
            try:
                self.model = SiglipVisionModel.from_pretrained(
                    siglip_local_path, 
                    local_files_only=True,
                    torch_dtype=target_dtype
                ).to(self.device)
            except Exception as e:
                auditor.error(f"Failed to load to {self.device.type} with {target_dtype}: {e}. Falling back to CPU.")
                self.device = torch.device("cpu")
                self.model = SiglipVisionModel.from_pretrained(
                    siglip_local_path, 
                    local_files_only=True,
                    torch_dtype=torch.float32
                ).to(self.device)
            self.model.eval()
                
        elif mode == "faces":
            os.environ["TORCH_HOME"] = str(get_models_dir() / "torch")
            try:
                from facenet_pytorch import MTCNN, InceptionResnetV1
                self.mtcnn = MTCNN(keep_all=False, device='cpu')
                self.resnet = InceptionResnetV1(pretrained='vggface2').eval().to(self.device)
            except ImportError:
                auditor.critical("Module facenet-pytorch missing.")
                self.scan_mode = "error"

    def _compute_fast_hash(self, file_path: Path) -> str:
        try:
            h = blake3.blake3()
            stat = file_path.stat()
            size = stat.st_size
            
            with open(file_path, 'rb') as f:
                if size <= 100 * 1024 * 1024:
                    while chunk := f.read(1024 * 1024): h.update(chunk)
                else:
                    step = size // 10
                    for i in range(10):
                        f.seek(i * step, os.SEEK_SET)
                        chunk = f.read(1024 * 1024)
                        if not chunk: break
                        h.update(chunk)
                    meta_str = f"{size}_{stat.st_mtime}_{file_path.suffix}"
                    h.update(meta_str.encode('utf-8'))
            return h.hexdigest()
        except Exception as e:
            return f"FAIL_{file_path.name}_{e}"

    def _compute_vector_batch(self, images: list) -> list:
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
                auditor.error(f"Face vector extraction failed: {e}")
            return results
            
        elif self.scan_mode == "visual":
            try:
                def run_on_device(dev):
                    all_f_norms = []
                    chunk_size = 32 
                    for i in range(0, len(images), chunk_size):
                        chunk = images[i:i+chunk_size]
                        if self.processor is None or self.model is None:
                            raise RuntimeError("NPU Engine not initialized")
                            
                        inputs = self.processor(images=chunk, return_tensors="pt")
                        pixel_values = inputs["pixel_values"]
                        
                        target_dtype = torch.float16 if dev.type in ("cuda", "mps") else torch.float32
                        
                        if dev.type == "cuda":
                            pixel_values = pixel_values.pin_memory().to(dev, non_blocking=True, dtype=target_dtype)
                        else:
                            pixel_values = pixel_values.to(dev, dtype=target_dtype)
                
                        with torch.no_grad():
                            outputs = self.model(pixel_values=pixel_values)
                            f = outputs.pooler_output
                            f_norm = torch.nn.functional.normalize(f, p=2, dim=-1)
                            all_f_norms.extend(f_norm.cpu().numpy().astype(np.float32))
                        
                        del inputs, pixel_values, outputs, f, f_norm
                    return all_f_norms

                try:
                    return run_on_device(self.device)
                except Exception as e:
                    auditor.error(f"H/W NPU Fail: {e}. Fallback to CPU execution.")
                    self.device = torch.device("cpu")
                    self.model = self.model.to("cpu")
                    return run_on_device(self.device)
            except Exception as e:
                auditor.critical(f"FATAL NPU ERROR in vectorization: {e}")
                raise e 
        return [None] * len(images)

    def extract_features(self, target_dirs: list, allowed_exts: set = None, progress_callback=None) -> list:
        from utils.i18n import translator
        self.is_paused = False
        self.is_stopped = False
        self.current_file_data = []
        
        if progress_callback: progress_callback(0, 0, "Indexing disk...")
            
        def fast_scandir(directory):
            discovered = []
            try:
                for entry in os.scandir(directory):
                    if self.is_stopped: break
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith('.'):
                        discovered.extend(fast_scandir(entry.path))
                    elif entry.is_file(follow_symlinks=False) and not entry.name.startswith('.'):
                        ext = os.path.splitext(entry.name)[1].lower()
                        if allowed_exts and ext not in allowed_exts: continue
                        discovered.append(Path(entry.path))
            except PermissionError: pass
            return discovered

        files = []
        for d in target_dirs:
            p_dir = Path(d)
            if p_dir.is_dir():
                files.extend(fast_scandir(d))
                
        if not files: return self.current_file_data
        
        if progress_callback: progress_callback(0, 0, f"Found: {len(files)} files...")
        
        db_name = f"meta_v2_{self.scan_mode}.db"
        cache_db = VectorCache(db_name)
        
        file_strs = [str(f) for f in files]
        meta_cache = cache_db.get_metadata_for_paths(file_strs)

        if progress_callback: progress_callback(0, len(files), translator.tr("scan_io"))
        
        tasks, all_results = [], []
        
        for idx, file_path in enumerate(files):
            if self.is_stopped: break
            try:
                stat = file_path.stat()
                size, mtime = stat.st_size, stat.st_mtime
                if size == 0: continue
                
                file_str = str(file_path)
                c_m = meta_cache.get(file_str)
                
                if c_m and c_m['size'] == size and c_m['mtime'] == mtime:
                    vec = cache_db.get_vector(file_str)
                    if vec is not None:
                        all_results.append({
                            "path": file_str, "size": size, "mtime": mtime, "phash": c_m['phash'], 
                            "vector": vec, "shm_blocks": [], "res": c_m.get('res', ''), 
                            "dur": c_m.get('dur', 0.0), "codec": c_m.get('codec', ''), 
                            "sharpness": c_m.get('sharpness', 0.0), "fps": c_m.get('fps', 0.0)
                        })
                        if progress_callback and idx % 10 == 0:
                            progress_callback(len(all_results), len(files), translator.tr("scan_cache"))
                        continue 
                
                file_hash = self._compute_fast_hash(file_path)
                tasks.append((file_path, size, mtime, file_hash, None, self.scan_mode))
            except Exception: continue

        vram_gb = 0
        if self.device.type == "cuda":
            try: vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024**3)
            except Exception: pass

        chunk_size = 256
        batch_size = 64 if vram_gb >= 8 else 32
        if self.device.type == "cpu": batch_size = 16
            
        bypassed_count = len(all_results)
        available_ram_mb = psutil.virtual_memory().available / (1024 * 1024)
        safe_workers = max(1, int(available_ram_mb // 1500))
        max_workers = min(max(1, os.cpu_count() - 1 if os.cpu_count() else 1), safe_workers)
        
        # Checkpointing: Инкрементальное сохранение батчей
        for chunk_start in range(0, len(tasks), chunk_size):
            if self.is_stopped: break
            chunk_tasks = tasks[chunk_start : chunk_start + chunk_size]
            chunk_results = []
            
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(process_single_file_io, task): task for task in chunk_tasks}
                for future in as_completed(futures):
                    while self.is_paused and not self.is_stopped: time.sleep(0.1)
                    if self.is_stopped: 
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    res = future.result()
                    if res: chunk_results.append(res)
                    if progress_callback:
                        progress_callback(bypassed_count + chunk_start + len(chunk_results), len(files), f"{translator.tr('scan_npu')}{Path(res['path']).name}")

            if self.is_stopped: break
                
            needs_vector = [r for r in chunk_results if r['vector'] is None and len(r.get('shm_blocks', [])) > 0]
            
            for i in range(0, len(needs_vector), batch_size):
                while self.is_paused and not self.is_stopped: time.sleep(0.1)
                if self.is_stopped: break
                
                batch = needs_vector[i:i+batch_size]
                flat_images, counts = [], []
                for b in batch:
                    imgs = []
                    for shm_meta in b['shm_blocks']:
                        if shm_meta.get("is_shm"):
                            try:
                                shm = shared_memory.SharedMemory(name=shm_meta['name'])
                                arr = np.ndarray(shm_meta['shape'], dtype=shm_meta['dtype'], buffer=shm.buf)
                                imgs.append(Image.fromarray(arr.copy()))
                                shm.close()
                                shm.unlink()
                            except Exception as e:
                                auditor.error(f"SHM Read Fault: {e}")
                                try: shm.unlink() 
                                except: pass
                        else:
                            try:
                                arr = np.frombuffer(shm_meta['data'], dtype=shm_meta['dtype']).reshape(shm_meta['shape'])
                                imgs.append(Image.fromarray(arr.copy()))
                            except Exception: pass
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
                        b['vector'] = avg_vec / np.linalg.norm(avg_vec)
                    else: b['vector'] = None
                
                del flat_images, flat_vectors, batch
                
            for r in chunk_results: 
                r['shm_blocks'] = []
            
            # Сохранение промежуточных результатов конвейера на диск (Защита от потери данных при краше)
            insert_batch = []
            for r in chunk_results:
                if r['vector'] is not None:
                    insert_batch.append((
                        str(r['path']), int(r['size']), float(r['mtime']), str(r['phash']), 
                        str(r['res']), float(r['dur']), str(r['codec']), float(r['sharpness']), 
                        float(r['fps']), r['vector']
                    ))
            if insert_batch: cache_db.save_batch(insert_batch)
            all_results.extend(chunk_results)

        if self.is_stopped: 
            cache_db.close()
            return []
            
        if progress_callback: progress_callback(len(files), len(files), translator.tr("scan_faiss"))

        for r in all_results:
            if r['vector'] is not None:
                self.current_file_data.append({
                    "path": r['path'], "phash": r['phash'], "vector": r['vector'],
                    "size": r['size'], "resolution": r['res'], "duration": r['dur'], 
                    "codec": r['codec'], "sharpness": r['sharpness'], "fps": r['fps'], "mtime": r['mtime'] 
                })

        cache_db.close()
        return self.current_file_data

    def build_clusters(self, threshold: float) -> list:
        clusters = []
        file_data = self.current_file_data.copy()
        if not file_data or len(file_data) < 2: return clusters

        state_str = "".join([f"{item['path']}_{item['size']}_{item['mtime']}" for item in file_data])
        state_signature = hashlib.md5(state_str.encode('utf-8')).hexdigest()
        
        cache_dir = get_app_data_dir() / "faiss_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        dist_file = cache_dir / f"{self.scan_mode}_{state_signature}_dist.npy"
        keys_file = cache_dir / f"{self.scan_mode}_{state_signature}_keys.npy"
        
        k = min(10000, len(file_data))
        cache_valid = False
        
        if dist_file.exists() and keys_file.exists():
            try:
                tmp_keys = np.load(keys_file)
                if tmp_keys.shape[1] >= k:
                    distances = np.load(dist_file)[:, :k]
                    keys = tmp_keys[:, :k]
                    cache_valid = True
            except Exception: pass
                
        if not cache_valid:
            vectors = np.vstack([item["vector"] for item in file_data]).astype(np.float32)
            index = faiss.IndexFlatIP(vectors.shape[1])
            index.add(vectors)
            distances, keys = index.search(vectors, k)
            
            for old_file in cache_dir.glob(f"{self.scan_mode}_*.npy"):
                try: os.remove(old_file)
                except Exception: pass
                
            np.save(dist_file, distances)
            np.save(keys_file, keys)

        sim_threshold = 1.0 - threshold
        seq_exts = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v', '.gif'}
        doc_exts = {'.cbz', '.pdf'}
        adj = {i: [] for i in range(len(file_data))}
        
        for i in range(len(file_data)):
            ext_i = Path(file_data[i]["path"]).suffix.lower()
            is_seq_i = ext_i in seq_exts
            is_doc_i = ext_i in doc_exts
            
            for j in range(k):
                n_idx = int(keys[i][j])
                if n_idx == i or n_idx == -1: continue
                
                sim = float(distances[i][j])
                ext_j = Path(file_data[n_idx]["path"]).suffix.lower()
                
                if is_seq_i and ext_j in seq_exts: sim = 1.0 - (1.0 - sim) * 1.45
                if is_doc_i and ext_j in doc_exts:
                    local_threshold = min(0.98, sim_threshold + 0.05)
                    sim = (sim - 0.80) / 0.20 if sim > 0.80 else 0.0
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
                item['similarity'] = 1.0 if item == base_item else max(0.0, float(np.dot(base_vec, item['vector'])))
            cluster.sort(key=lambda x: x['similarity'], reverse=True)
            refined_clusters.append(cluster)
            
        return refined_clusters