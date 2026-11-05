"""File conversion jobs for studio-extractor. Kept separate from download flow."""
import os, time, uuid, threading, shutil, subprocess, glob

CJOBS = {}
CLOCK = threading.Lock()
CSEM = threading.Semaphore(1)  # one convert at a time on free tier
MAX_UPLOAD = 100 * 1024 * 1024
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


def _ff(inp, out, *extra, timeout=600):
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", inp, *extra, out]
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
            out = os.path.join(j["dir"], f"out.{dst}")
            j["progress"] = 20
            do_convert(j["infile"], out, src, dst)
            j["progress"] = 95
            if not os.path.exists(out) or os.path.getsize(out) == 0:
                raise RuntimeError("empty output")
            j.update(state="done", progress=100, file=out, size=os.path.getsize(out),
                     mime=MIME.get(dst, "application/octet-stream"),
                     filename=f"converted-{jid[:8]}.{dst}", at=time.time())
        except Exception as e:
            j.update(state="error", progress=0, error=str(e)[:300], at=time.time())


def create_job(upload_path, src_name, dst_ext, size):
    cleanup_c()
    if size > MAX_UPLOAD:
        raise ValueError("too_large")
    src_ext = (os.path.splitext(src_name)[1] or "").lstrip(".").lower()
    if src_ext == "jpeg":
        src_ext = "jpg"
    dst_ext = (dst_ext or "").lower().lstrip(".")
    if dst_ext == "jpeg":
        dst_ext = "jpg"
    if not kind_of(src_ext) or dst_ext not in targets_for(src_ext):
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
        CJOBS[jid] = {"state": "queued", "progress": 0, "at": time.time(), "dir": d,
                      "infile": infile, "src_ext": src_ext, "dst_ext": dst_ext,
                      "src_name": src_name}
    threading.Thread(target=run_job, args=(jid,), daemon=True).start()
    return jid
