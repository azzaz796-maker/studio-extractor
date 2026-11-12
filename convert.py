"""File conversion jobs for studio-extractor. Kept separate from download flow."""
import os, time, uuid, threading, shutil, subprocess, glob

CJOBS = {}
CLOCK = threading.Lock()
CSEM = threading.Semaphore(1)  # one convert at a time on free tier
MAX_UPLOAD = 800 * 1024 * 1024  # soft cap; disk space is the hard limit
TTL = 15 * 60
ROOT = "/tmp/cjobs"

MIME = {
    "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
    "mkv": "video/x-matroska", "avi": "video/x-msvideo", "gif": "image/gif",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav",
    "ogg": "audio/ogg", "aac": "audio/aac", "flac": "audio/flac",
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "heic": "image/heic", "heif": "image/heif",
    "pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain", "odt": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf",
}

VIDEO = {"mp4", "mov", "webm", "mkv", "avi", "m4v"}
AUDIO = {"mp3", "m4a", "wav", "ogg", "aac", "flac"}
IMAGE = {"jpg", "jpeg", "png", "webp", "heic", "heif", "gif", "bmp"}
DOC = {"pdf", "docx", "txt", "odt", "rtf"}

TARGETS = {
    "video": ["mp4", "mov", "webm", "mkv", "avi", "gif", "mp3", "m4a", "wav"],
    "audio": ["mp3", "m4a", "wav", "ogg", "aac", "flac"],
    "image": ["jpg", "png", "webp", "gif", "pdf"],
    "pdf": ["png", "jpg", "docx"],
    "docx": ["pdf", "txt"],
    "txt": ["pdf", "docx"],
    "odt": ["pdf", "docx"],
    "rtf": ["pdf", "docx"],
}


def kind_of(ext):
    e = (ext or "").lower().lstrip(".")
    if e in VIDEO: return "video"
    if e in AUDIO: return "audio"
    if e in IMAGE: return "image"
    if e == "pdf": return "pdf"
    if e == "docx": return "docx"
    if e == "txt": return "txt"
    if e == "odt": return "odt"
    if e == "rtf": return "rtf"
    return None


def targets_for(ext):
    k = kind_of(ext)
    if not k: return []
    src = (ext or "").lower().lstrip(".")
    if src == "jpeg": src = "jpg"
    return [t for t in TARGETS[k] if t != src and not (src == "jpg" and t == "jpeg")]


# ——— video enhancement (ffmpeg only; free-tier CPU) ———
ENH = {"hd720": 720, "hd1080": 1080, "hdsame": 0}
# measured on Render free: ~6.5x realtime for 720p output, ~12x for 1080p output
ENH_MAX_SEC = {"hd720": 120, "hd1080": 60, "hdsame": 120}


def enh_limit(info, choice):
    ow, oh = enh_plan(info, choice)
    return 60 if min(ow, oh) > 720 else 120
ENH_TIMEOUT = 45 * 60


def is_enh(dst):
    return (dst or "").lower() in ENH


def valid_target(src_ext, dst_ext):
    if is_enh(dst_ext):
        return kind_of(src_ext) == "video"
    return bool(kind_of(src_ext)) and dst_ext in targets_for(src_ext)


def probe_video(path):
    import json as _j
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate,color_transfer:format=duration",
                        "-of", "json", path], capture_output=True, timeout=60)
    d = _j.loads((r.stdout or b"{}").decode("utf-8", "ignore") or "{}")
    st = (d.get("streams") or [{}])[0]
    w, h = int(st.get("width") or 0), int(st.get("height") or 0)
    if not w or not h:
        raise ValueError("not_video")
    try:
        a, b = (st.get("r_frame_rate") or "30/1").split("/")
        fps = float(a) / float(b or 1)
    except Exception:
        fps = 30.0
    try:
        dur = float((d.get("format") or {}).get("duration") or 0)
    except Exception:
        dur = 0.0
    return {"w": w, "h": h, "fps": fps, "dur": dur, "trc": st.get("color_transfer") or ""}


def enh_plan(info, choice):
    """Return (out_w, out_h) on the short side target; never > 2x source or 1080."""
    w, h = info["w"], info["h"]
    short = min(w, h)
    want = ENH.get(choice, 0) or short
    tgt = min(want, short * 2, 1080)
    if short >= tgt:
        # no upscale; shrink only >1080 (or >720 when the user picked 720p, the fast option)
        tgt = min(short, 720 if choice == "hd720" else 1080)
    k = tgt / float(short)
    ow = int(round(w * k / 2)) * 2
    oh = int(round(h * k / 2)) * 2
    return max(2, ow), max(2, oh)


def enhance_video(inp, out, choice, job=None):
    info = probe_video(inp)
    ow, oh = enh_plan(info, choice)
    up = max(ow, oh) > max(info["w"], info["h"])
    vf = []
    if info["trc"] in ("arib-std-b67", "smpte2084"):
        # iPhone HDR → SDR so colours are not washed out
        vf.append("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
                  "zscale=t=bt709:m=bt709:r=tv")
    if info["fps"] > 31:
        vf.append("fps=30")
    vf.append("hqdn3d=1.5:1.5:6:6")
    vf.append(f"scale={ow}:{oh}:flags=lanczos")
    vf.append("unsharp=5:5:0.9:5:5:0.0" if up else "unsharp=5:5:0.6:5:5:0.0")
    vf.append("eq=contrast=1.04:saturation=1.10")
    vf.append("format=yuv420p")
    dur = info["dur"] or 1.0

    def attempt(filters):
        cmd = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1", "-threads", "1", "-i", inp,
               "-map", "0:v:0", "-map", "0:a:0?", "-vf", ",".join(filters),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-profile:v", "high",
               "-pix_fmt", "yuv420p", "-threads", "1", "-c:a", "aac", "-b:a", "160k", "-ac", "2",
               "-movflags", "+faststart", "-map_metadata", "-1", "-tag:v", "avc1", out]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        t0 = time.time()
        import threading as _t
        errbuf = []
        _t.Thread(target=lambda: errbuf.append(p.stderr.read()), daemon=True).start()
        for line in p.stdout:
            if time.time() - t0 > ENH_TIMEOUT:
                p.kill(); raise RuntimeError("timeout")
            line = line.decode("utf-8", "ignore").strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    sec = int(line.split("=", 1)[1]) / 1e6
                except Exception:
                    continue
                if job is not None and sec > 0:
                    frac = max(0.0, min(1.0, sec / dur))
                    el = time.time() - t0
                    job["progress"] = int(10 + frac * 85)
                    job["eta"] = int(el / frac - el) if frac > 0.02 else None
        p.wait()
        if p.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            raise RuntimeError((b"".join(errbuf) or b"ffmpeg failed").decode("utf-8", "ignore")[-400:])

    if job is not None:
        job.update(src_w=info["w"], src_h=info["h"], out_w=ow, out_h=oh, duration=round(dur, 1))
    try:
        attempt(vf)
    except RuntimeError as e:
        if str(e) == "timeout" or not vf[0].startswith("zscale"):
            raise
        attempt(vf[1:])  # zscale unavailable → plain path
    return out


def _run(cmd, timeout=600):
    return subprocess.run(cmd, timeout=timeout, capture_output=True)


def _have_soffice():
    return shutil.which("soffice") or shutil.which("libreoffice")


def cleanup_c():
    now = time.time()
    with CLOCK:
        for jid, j in list(CJOBS.items()):
            if now - j.get("at", 0) > TTL:
                d = j.get("dir")
                if d and os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
                CJOBS.pop(jid, None)


def disk_free(path="/tmp"):
    return shutil.disk_usage(path).free


def need_bytes(size):
    # input + converted output + ffmpeg overhead
    return int(size) * 2 + 80 * 1024 * 1024


def ensure_disk(size):
    if disk_free() < need_bytes(size):
        raise ValueError("no_space")


def _ff(inp, out, *extra, timeout=1800):
    cmd = ["ffmpeg", "-y", "-v", "error", "-threads", "1", "-i", inp, "-threads", "1", *extra, out]
    r = _run(cmd, timeout=timeout)
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        err = (r.stderr or b"").decode("utf-8", "ignore")[-400:]
        raise RuntimeError(err or "ffmpeg failed")
    return out


def convert_video(inp, out, fmt):
    if fmt in ("mp3", "m4a", "wav"):
        if fmt == "mp3":
            return _ff(inp, out, "-vn", "-acodec", "libmp3lame", "-q:a", "4")
        if fmt == "m4a":
            return _ff(inp, out, "-vn", "-acodec", "aac", "-b:a", "160k")
        return _ff(inp, out, "-vn", "-acodec", "pcm_s16le")
    if fmt == "gif":
        # short, scaled GIF to keep memory down
        return _ff(inp, out, "-t", "8", "-vf", "fps=8,scale=480:-1:flags=lanczos", "-an")
    vcodec = {"mp4": "libx264", "mov": "libx264", "webm": "libvpx-vp9", "mkv": "libx264", "avi": "mpeg4"}
    acodec = {"mp4": "aac", "mov": "aac", "webm": "libopus", "mkv": "aac", "avi": "mp3"}
    extra = ["-c:v", vcodec[fmt], "-preset", "veryfast", "-crf", "28", "-c:a", acodec[fmt], "-movflags", "+faststart"]
    if fmt == "webm":
        extra = ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "35", "-c:a", "libopus", "-b:a", "96k", "-row-mt", "1"]
    try:
        return _ff(inp, out, *extra)
    except Exception:
        # remux / copy when possible
        return _ff(inp, out, "-c", "copy")


def convert_audio(inp, out, fmt):
    m = {
        "mp3": ["-acodec", "libmp3lame", "-q:a", "4"],
        "m4a": ["-acodec", "aac", "-b:a", "160k"],
        "wav": ["-acodec", "pcm_s16le"],
        "ogg": ["-acodec", "libvorbis", "-q:a", "4"],
        "aac": ["-acodec", "aac", "-b:a", "160k"],
        "flac": ["-acodec", "flac"],
    }
    return _ff(inp, out, *m[fmt])


def _open_image(path):
    from PIL import Image
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    im = Image.open(path)
    if im.mode in ("RGBA", "P") and path.lower().endswith((".jpg", ".jpeg")) is False:
        pass
    return im


def convert_image(inp, out, fmt):
    from PIL import Image
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    if fmt == "pdf":
        import img2pdf
        # heic → jpeg bytes first if needed
        if inp.lower().endswith((".heic", ".heif", ".webp", ".gif", ".bmp")):
            im = Image.open(inp).convert("RGB")
            tmp = out + ".jpg"
            im.save(tmp, "JPEG", quality=90)
            with open(out, "wb") as f:
                f.write(img2pdf.convert(tmp))
            os.remove(tmp)
        else:
            with open(out, "wb") as f:
                f.write(img2pdf.convert(inp))
        return out
    im = Image.open(inp)
    if fmt in ("jpg", "jpeg"):
        im = im.convert("RGB")
        im.save(out, "JPEG", quality=90)
    elif fmt == "png":
        im.save(out, "PNG")
    elif fmt == "webp":
        im.convert("RGB").save(out, "WEBP", quality=85)
    elif fmt == "gif":
        im.convert("P", palette=Image.ADAPTIVE).save(out, "GIF")
    else:
        raise RuntimeError("unsupported image target")
    return out


def convert_pdf(inp, out, fmt):
    import fitz
    if fmt in ("png", "jpg"):
        doc = fitz.open(inp)
        if len(doc) == 0:
            raise RuntimeError("empty pdf")
        page = doc.load_page(0)
        mat = fitz.Matrix(1.5, 1.5)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        if fmt == "png":
            pix.save(out)
        else:
            pix.save(out, output="jpeg", jpg_quality=90)
        doc.close()
        return out
    if fmt == "docx":
        from pdf2docx import Converter
        cv = Converter(inp)
        try:
            cv.convert(out, start=0, end=40)
        finally:
            cv.close()
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            raise RuntimeError("pdf2docx empty")
        return out
    raise RuntimeError("unsupported pdf target")


def _lo_convert(inp, out_dir, target_ext):
    bin_ = shutil.which("soffice") or shutil.which("libreoffice")
    if not bin_:
        raise RuntimeError("libreoffice unavailable")
    env_dir = os.path.join(out_dir, "lo-profile")
    os.makedirs(env_dir, exist_ok=True)
    r = _run([bin_, "--headless", f"-env:UserInstallation=file://{env_dir}",
              "--convert-to", target_ext, "--outdir", out_dir, inp], timeout=180)
    # find produced file
    base = os.path.splitext(os.path.basename(inp))[0]
    cand = glob.glob(os.path.join(out_dir, base + ".*"))
    cand = [c for c in cand if c.lower().endswith("." + target_ext)]
    if not cand:
        err = (r.stderr or b"").decode("utf-8", "ignore")[-300:]
        raise RuntimeError(err or "libreoffice failed")
    return cand[0]


def convert_docx(inp, out, fmt):
    if fmt == "txt":
        from docx import Document
        doc = Document(inp)
        text = "\n".join(p.text for p in doc.paragraphs)
        open(out, "w", encoding="utf-8").write(text)
        return out
    if fmt == "pdf":
        try:
            produced = _lo_convert(inp, os.path.dirname(out), "pdf")
            if produced != out:
                shutil.move(produced, out)
            return out
        except Exception as e1:
            # lightweight fallback: text-only PDF
            try:
                from docx import Document
                from reportlab.pdfgen import canvas
                from reportlab.lib.pagesizes import A4
                from reportlab.pdfbase import pdfmetrics
                from reportlab.pdfbase.ttfonts import TTFont
                doc = Document(inp)
                lines = []
                for p in doc.paragraphs:
                    lines.append(p.text)
                c = canvas.Canvas(out, pagesize=A4)
                w, h = A4
                y = h - 40
                c.setFont("Helvetica", 11)
                for line in lines:
                    for chunk in _wrap(line or " ", 95):
                        # reportlab Helvetica can't do Arabic well; still produce a PDF
                        try:
                            c.drawString(40, y, chunk)
                        except Exception:
                            c.drawString(40, y, chunk.encode("latin-1", "replace").decode("latin-1"))
                        y -= 14
                        if y < 40:
                            c.showPage()
                            c.setFont("Helvetica", 11)
                            y = h - 40
                c.save()
                return out
            except Exception as e2:
                raise RuntimeError(f"docx→pdf failed: {e1} | {e2}")
    raise RuntimeError("unsupported docx target")


def _wrap(s, n):
    if not s:
        return [""]
    out = []
    while len(s) > n:
        out.append(s[:n])
        s = s[n:]
    out.append(s)
    return out


def convert_txt(inp, out, fmt):
    text = open(inp, "rb").read().decode("utf-8", "ignore")
    if fmt == "docx":
        from docx import Document
        d = Document()
        for line in text.splitlines() or [""]:
            d.add_paragraph(line)
        d.save(out)
        return out
    if fmt == "pdf":
        try:
            produced = _lo_convert(inp, os.path.dirname(out), "pdf")
            if produced != out:
                shutil.move(produced, out)
            return out
        except Exception:
            from reportlab.pdfgen import canvas
            from reportlab.lib.pagesizes import A4
            c = canvas.Canvas(out, pagesize=A4)
            w, h = A4
            y = h - 40
            c.setFont("Helvetica", 11)
            for line in text.splitlines() or [""]:
                for chunk in _wrap(line, 95):
                    try:
                        c.drawString(40, y, chunk)
                    except Exception:
                        c.drawString(40, y, chunk.encode("latin-1", "replace").decode("latin-1"))
                    y -= 14
                    if y < 40:
                        c.showPage()
                        c.setFont("Helvetica", 11)
                        y = h - 40
            c.save()
            return out
    raise RuntimeError("unsupported txt target")


def convert_office_via_lo(inp, out, fmt):
    produced = _lo_convert(inp, os.path.dirname(out), fmt)
    if produced != out:
        shutil.move(produced, out)
    return out


def do_convert(inp, out, src_ext, dst_ext):
    src = src_ext.lower().lstrip(".")
    dst = dst_ext.lower().lstrip(".")
    if src == "jpeg": src = "jpg"
    if dst == "jpeg": dst = "jpg"
    k = kind_of(src)
    if not k or dst not in targets_for(src):
        raise RuntimeError("unsupported conversion")
    if k == "video":
        return convert_video(inp, out, dst)
    if k == "audio":
        return convert_audio(inp, out, dst)
    if k == "image":
        return convert_image(inp, out, dst)
    if k == "pdf":
        return convert_pdf(inp, out, dst)
    if k == "docx":
        return convert_docx(inp, out, dst)
    if k == "txt":
        return convert_txt(inp, out, dst)
    if k in ("odt", "rtf"):
        return convert_office_via_lo(inp, out, dst)
    raise RuntimeError("unsupported")


def run_job(jid):
    j = CJOBS[jid]
    with CSEM:
        j["state"] = "running"
        j["progress"] = 5
        j["at"] = time.time()
        try:
            src = j["src_ext"]
            dst = j["dst_ext"]
            if is_enh(dst):
                out = os.path.join(j["dir"], "out.mp4")
                j["progress"] = 8
                enhance_video(j["infile"], out, dst, job=j)
            else:
                out = os.path.join(j["dir"], f"out.{dst}")
                j["progress"] = 20
                do_convert(j["infile"], out, src, dst)
            j["progress"] = 95
            if not os.path.exists(out) or os.path.getsize(out) == 0:
                raise RuntimeError("empty output")
            if is_enh(dst):
                j.update(state="done", progress=100, file=out, size=os.path.getsize(out),
                         mime="video/mp4", filename=f"enhanced-{jid[:8]}.mp4", at=time.time())
            else:
                j.update(state="done", progress=100, file=out, size=os.path.getsize(out),
                         mime=MIME.get(dst, "application/octet-stream"),
                         filename=f"converted-{jid[:8]}.{dst}", at=time.time())
        except Exception as e:
            j.update(state="error", progress=0, error=str(e)[:300], at=time.time())


def create_job(upload_path, src_name, dst_ext, size):
    cleanup_c()
    if size > MAX_UPLOAD:
        raise ValueError("too_large")
    ensure_disk(size)
    src_ext = (os.path.splitext(src_name)[1] or "").lstrip(".").lower()
    if src_ext == "jpeg":
        src_ext = "jpg"
    dst_ext = (dst_ext or "").lower().lstrip(".")
    if dst_ext == "jpeg":
        dst_ext = "jpg"
    if not valid_target(src_ext, dst_ext):
        raise ValueError("unsupported")
    with CLOCK:
        active = sum(1 for j in CJOBS.values() if j["state"] in ("queued", "running"))
        if active >= 3:
            raise ValueError("busy")
        jid = uuid.uuid4().hex
        d = os.path.join(ROOT, jid)
        os.makedirs(d, exist_ok=True)
        infile = os.path.join(d, f"in.{src_ext}")
        shutil.move(upload_path, infile)
    if is_enh(dst_ext):
        try:
            info = probe_video(infile)
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise ValueError("not_video")
        if info["dur"] > enh_limit(info, dst_ext) + 1:
            shutil.rmtree(d, ignore_errors=True)
            raise ValueError("too_long")
    with CLOCK:
        CJOBS[jid] = {"state": "queued", "progress": 0, "at": time.time(), "dir": d,
                      "infile": infile, "src_ext": src_ext, "dst_ext": dst_ext,
                      "src_name": src_name}
    threading.Thread(target=run_job, args=(jid,), daemon=True).start()
    return jid


# ——— chunked upload sessions (bypass Render proxy body limits) ———
UPLOADS = {}
UCHUNK = 12 * 1024 * 1024  # 12 MB parts
UROOT = "/tmp/cuploads"


def init_upload(name, size, dst_ext, n_chunks):
    cleanup_c()
    size = int(size)
    n_chunks = int(n_chunks)
    if size <= 0 or n_chunks <= 0:
        raise ValueError("empty")
    if size > MAX_UPLOAD:
        raise ValueError("too_large")
    ensure_disk(size)
    src_ext = (os.path.splitext(name)[1] or "").lstrip(".").lower()
    if src_ext == "jpeg":
        src_ext = "jpg"
    dst_ext = (dst_ext or "").lower().lstrip(".")
    if dst_ext == "jpeg":
        dst_ext = "jpg"
    if not valid_target(src_ext, dst_ext):
        raise ValueError("unsupported")
    with CLOCK:
        active = sum(1 for j in CJOBS.values() if j["state"] in ("queued", "running"))
        if active >= 3:
            raise ValueError("busy")
        uid = uuid.uuid4().hex
        d = os.path.join(UROOT, uid)
        os.makedirs(d, exist_ok=True)
        UPLOADS[uid] = {
            "dir": d, "name": name, "size": size, "dst_ext": dst_ext, "src_ext": src_ext,
            "n": n_chunks, "got": set(), "at": time.time(),
        }
    return uid


def save_chunk(uid, index, stream):
    u = UPLOADS.get(uid)
    if not u:
        raise ValueError("no_upload")
    index = int(index)
    if index < 0 or index >= u["n"]:
        raise ValueError("bad_chunk")
    path = os.path.join(u["dir"], f"part-{index:05d}")
    written = 0
    with open(path, "wb") as out:
        while True:
            buf = stream.read(1024 * 1024)
            if not buf:
                break
            written += len(buf)
            if written > UCHUNK + 1024 * 1024:
                raise ValueError("chunk_too_big")
            out.write(buf)
    if written == 0:
        raise ValueError("empty")
    u["got"].add(index)
    u["at"] = time.time()
    return len(u["got"]), u["n"]


def finish_upload(uid):
    u = UPLOADS.get(uid)
    if not u:
        raise ValueError("no_upload")
    if len(u["got"]) != u["n"]:
        raise ValueError("incomplete")
    ensure_disk(u["size"])
    assembled = os.path.join(u["dir"], f"in.{u['src_ext']}")
    with open(assembled, "wb") as out:
        for i in range(u["n"]):
            part = os.path.join(u["dir"], f"part-{i:05d}")
            with open(part, "rb") as inp:
                shutil.copyfileobj(inp, out, 1024 * 1024)
            os.remove(part)
    got = os.path.getsize(assembled)
    # allow small mismatch from reported size
    if abs(got - u["size"]) > 64 and got > u["size"] + 1024:
        raise ValueError("size_mismatch")
    if got > MAX_UPLOAD:
        raise ValueError("too_large")
    jid = create_job(assembled, u["name"], u["dst_ext"], got)
    # drop session meta; create_job moved file into job dir
    with CLOCK:
        UPLOADS.pop(uid, None)
        try:
            shutil.rmtree(u["dir"], ignore_errors=True)
        except Exception:
            pass
    return jid


def cleanup_uploads():
    now = time.time()
    with CLOCK:
        for uid, u in list(UPLOADS.items()):
            if now - u.get("at", 0) > TTL:
                shutil.rmtree(u.get("dir") or "", ignore_errors=True)
                UPLOADS.pop(uid, None)
