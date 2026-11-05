import os, urllib.parse, uuid, time, threading, glob, shutil, hashlib
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
import yt_dlp, re, json, urllib.request

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"

def getjson(u):
    r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "application/json"}), timeout=20)
    return json.loads(r.read())

def alt_source(url):
    m = re.search(r"(?:twitter|x)\.com/([^/]+)/status/(\d+)", url)
    if m:
        for api in (f"https://api.fxtwitter.com/{m[1]}/status/{m[2]}", f"https://api.vxtwitter.com/{m[1]}/status/{m[2]}"):
            try:
                d = getjson(api)
                t = d.get("tweet") or d
                vids = [x for x in ((t.get("media") or {}).get("videos") or t.get("media_extended") or []) if x.get("type", "video") in ("video", "gif")]
                if vids:
                    v = vids[0]
                    best = v.get("url")
                    vs = [x for x in v.get("variants") or [] if "mp4" in (x.get("content_type") or x.get("url", ""))]
                    if vs:
                        best = max(vs, key=lambda x: x.get("bitrate") or 0)["url"]
                    if best:
                        return best, {"title": (t.get("text") or "x-video")[:80], "thumbnail": v.get("thumbnail_url")}
            except Exception:
                pass
    m = re.search(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
    if m:
        try:
            html = urllib.request.urlopen(urllib.request.Request(f"https://www.instagram.com/p/{m[1]}/embed/captioned/", headers={"User-Agent": UA}), timeout=20).read().decode("utf-8", "ignore")
            mm = re.search(r'video_url\\*":\\*"(.*?)\\*"', html)
            if mm:
                v = mm[1].replace("\\", "").replace("u0026", "&")
                return v, {"title": "instagram-" + m[1]}
        except Exception:
            pass
    if "tiktok.com" in url:
        try:
            d = getjson("https://www.tikwm.com/api/?hd=1&url=" + urllib.parse.quote(url, safe=""))["data"]
            u = d.get("hdplay") or d.get("play")
            if u:
                return (u if u.startswith("http") else "https://www.tikwm.com" + u), {"title": d.get("title"), "thumbnail": d.get("cover")}
        except Exception:
            pass
    return None, None

def fetch_to(u, fp):
    r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=30)
    with open(fp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)

KEY = os.environ.get("API_KEY", "")
WORK = "/tmp/jobs"
os.makedirs(WORK, exist_ok=True)
app = FastAPI()
JOBS = {}
BY_URL = {}
LOCK = threading.Lock()
SEM = threading.Semaphore(2)

FMT = ("best[ext=mp4][vcodec^=avc1][acodec!=none][height<=720]/"
       "bv*[ext=mp4][vcodec^=avc1][height<=720]+ba[ext=m4a]/"
       "bv*[vcodec^=avc1][height<=720]+ba/"
       "best[ext=mp4][acodec!=none][height<=1080]/"
       "bv*[height<=720]+ba/best[height<=720]/best")

def auth(req: Request):
    if KEY and req.headers.get("x-key") != KEY:
        raise HTTPException(401, "unauthorized")

def cleanup():
    now = time.time()
    with LOCK:
        for jid, j in list(JOBS.items()):
            if now - j["at"] > 1800:
                shutil.rmtree(os.path.join(WORK, jid), ignore_errors=True)
                JOBS.pop(jid, None)
                BY_URL.pop(j["url"], None)

def try_alt(j, url, d):
    try:
        u, meta = alt_source(url)
        if u:
            fp = os.path.join(d, "video.mp4")
            fetch_to(u, fp)
            if os.path.getsize(fp) > 1000:
                j.update(state="done", progress=100, file=fp, size=os.path.getsize(fp), ext="mp4", **meta)
                return True
    except Exception as e:
        j["alt_error"] = str(e)[:150]
    return False

def run(jid, url):
    j = JOBS[jid]
    d = os.path.join(WORK, jid)
    os.makedirs(d, exist_ok=True)
    def hook(s):
        if s.get("status") == "downloading":
            tot = s.get("total_bytes") or s.get("total_bytes_estimate") or 0
            if tot:
                j["progress"] = min(99, int(s.get("downloaded_bytes", 0) * 100 / tot))
    base = {"outtmpl": os.path.join(d, "video.%(ext)s"), "quiet": True, "noplaylist": True,
            "merge_output_format": "mp4", "progress_hooks": [hook], "format": FMT,
            "socket_timeout": 20, "retries": 3, "concurrent_fragment_downloads": 4,
            }
    variants = [{}]
    if "youtu" in url:
        variants = [{"extractor_args": {"youtube": {"player_client": [c]}}} for c in
                    ("default", "tv_simply", "web_safari", "mweb", "android_vr", "ios", "tv")]
    with SEM:
        j["state"] = "running"
        err = None
        if "youtu" not in url and try_alt(j, url, d):
            return
        for v in variants:
            for f in glob.glob(os.path.join(d, "*")):
                os.remove(f)
            try:
                with yt_dlp.YoutubeDL({**base, **v}) as y:
                    info = y.extract_info(url, download=True)
                if info.get("entries"):
                    info = info["entries"][0]
                files = [f for f in glob.glob(os.path.join(d, "video.*")) if not f.endswith(".part") and os.path.getsize(f) > 0]
                if not files:
                    raise Exception("no file")
                fp = max(files, key=os.path.getsize)
                j.update(state="done", progress=100, file=fp, size=os.path.getsize(fp),
                         title=info.get("title"), author=info.get("uploader"),
                         thumbnail=info.get("thumbnail"), duration=info.get("duration"),
                         ext=fp.rsplit(".", 1)[-1])
                return
            except Exception as e:
                err = str(e)[:300]
        if try_alt(j, url, d):
            return
        j.update(state="error", error=err)

PLAT = re.compile(r"^https://([a-z0-9-]+\.)*(youtube\.com|youtu\.be|tiktok\.com|instagram\.com|x\.com|twitter\.com)/", re.I)
ALLOW = ["gendownload.com","tikwm.com","twimg.com","googlevideo.com","ytimg.com","tiktokcdn.com","tiktokcdn-us.com",
         "tiktokv.com","cdninstagram.com","fbcdn.net","xenon.zone","cjs.nz"]
DUA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

def host_ok(h):
    h = (h or "").lower()
    return any(h == d or h.endswith("." + d) for d in ALLOW)

def ar_err(code, msg, status=422):
    return JSONResponse({"ok": False, "error": code, "detail": msg}, status_code=status)

def gendownload(url):
    r = urllib.request.urlopen(urllib.request.Request("https://gendownload.com/api/extract", data=json.dumps({"url": url}).encode(),
        headers={"content-type": "application/json", "accept": "application/json", "user-agent": DUA,
                 "origin": "https://gendownload.com", "referer": "https://gendownload.com/"}), timeout=25)
    d = json.loads(r.read())
    fm = [f for f in d.get("formats") or [] if f.get("url") and f.get("type") != "audio"]
    if not fm:
        return None
    def h(f):
        m = re.search(r"(\d{3,4})", f.get("label") or ""); return int(m[1]) if m else 0
    under = sorted([f for f in fm if 0 < h(f) <= 720], key=h, reverse=True)
    best = (under or sorted(fm, key=h, reverse=True))[0]
    fmts = [{"label": f.get("label"), "type": "video", "url": f["url"], "ext": f.get("ext") or "mp4",
             "filesize": f.get("filesize"), "height": h(f)} for f in fm]
    title = d.get("title") or "video"
    return {"ok": True, "title": title, "author": d.get("author"), "thumbnail": d.get("thumbnail"), "source": d.get("source"),
            "filename": re.sub(r"[^\w\s.-]+", "", title).strip()[:80] + ".mp4" if re.sub(r"[^\w\s.-]+", "", title).strip() else "video.mp4",
            "url": best["url"], "formats": fmts, "provider": "gendownload", "estimatedSize": best.get("filesize")}

COBALT = ["https://rue-cobalt.xenon.zone/", "https://cobaltapi.cjs.nz/"]
def cobalt(url):
    for inst in COBALT:
        try:
            r = urllib.request.urlopen(urllib.request.Request(inst, data=json.dumps({"url": url, "videoQuality": "720"}).encode(),
                headers={"content-type": "application/json", "accept": "application/json", "user-agent": DUA}), timeout=20)
            d = json.loads(r.read())
            u = d.get("url")
            if d.get("status") not in ("tunnel", "redirect") or not u:
                continue
            pr = urllib.request.urlopen(urllib.request.Request(u, headers={"range": "bytes=0-2047", "user-agent": DUA}), timeout=40)
            if not pr.read(2048):
                continue
            fn = d.get("filename") or "video.mp4"
            return {"ok": True, "title": fn.rsplit(".", 1)[0], "filename": "video.mp4", "url": u, "formats": [],
                    "provider": "cobalt", "estimatedSize": None, "thumbnail": None}
        except Exception:
            pass
    return None

@app.post("/api/resolve")
async def resolve(req: Request):
    try:
        url = ((await req.json()).get("url") or "").strip()
    except Exception:
        return ar_err("invalid_json", "طلب غير صالح", 400)
    if not PLAT.match(url):
        return ar_err("invalid_url", "الصق رابط فيديو صالح من يوتيوب أو تيك توك أو إنستغرام أو X", 400)
    import asyncio
    m = re.search(r"youtube\.com/shorts/([\w-]{6,})", url) or re.search(r"youtu\.be/([\w-]{6,})", url)
    if m:
        url = "https://www.youtube.com/watch?v=" + m[1]
    t0 = time.time()
    while time.time() - t0 < 60:
        for fn, to in ((cobalt, 45), (gendownload, 30)):
            try:
                r = await asyncio.wait_for(asyncio.to_thread(fn, url), to)
                if r:
                    return r
            except Exception:
                pass
        await asyncio.sleep(1)
    if "youtu" not in url:
        try:
            u, meta = await asyncio.to_thread(alt_source, url)
            if u:
                return {"ok": True, "title": meta.get("title"), "thumbnail": meta.get("thumbnail"), "filename": "video.mp4",
                        "url": u, "formats": [], "provider": "alt", "estimatedSize": None}
        except Exception:
            pass
    return ar_err("resolve_failed", "تعذّر استخراج الفيديو الآن. تأكد أن الرابط عام وأعد المحاولة بعد لحظات.")

@app.get("/dl")
def dl(req: Request, u: str = "", filename: str = "video.mp4"):
    from fastapi.responses import StreamingResponse, Response
    p = urllib.parse.urlparse(u)
    if p.scheme != "https" or not host_ok(p.hostname):
        return Response("host not allowed", status_code=403)
    h = {"user-agent": DUA, "accept": "*/*"}
    if req.headers.get("range"): h["range"] = req.headers["range"]
    host = p.hostname.lower()
    if host.endswith("gendownload.com"):
        h.update(referer="https://gendownload.com/", origin="https://gendownload.com")
    elif "googlevideo" in host or host.endswith("ytimg.com"):
        h.update(referer="https://www.youtube.com/", origin="https://www.youtube.com")
    try:
        up = urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=20)
    except Exception as e:
        return Response("upstream fetch failed: " + str(e)[:200], status_code=502)
    if not host_ok(urllib.parse.urlparse(up.geturl()).hostname):
        return Response("host not allowed", status_code=403)
    cl = up.headers.get("content-length")
    if cl == "0":
        return Response("empty upstream", status_code=502)
    name = re.sub(r'[/\\?%*:|"<>]', "", filename).strip()[:120] or "video.mp4"
    ascii_ = re.sub(r"[^\x20-\x7E]", "_", name).replace('"', "")
    oh = {"content-disposition": f"attachment; filename=\"{ascii_}\"; filename*=UTF-8''{urllib.parse.quote(name)}",
          "cache-control": "private, no-store", "accept-ranges": "bytes"}
    if cl: oh["content-length"] = cl
    if up.headers.get("content-range"): oh["content-range"] = up.headers["content-range"]
    def gen():
        try:
            while True:
                b = up.read(1 << 16)
                if not b: break
                yield b
        finally:
            up.close()
    return StreamingResponse(gen(), status_code=up.status, media_type=up.headers.get("content-type") or "video/mp4", headers=oh)

def create_job(url):
    cleanup()
    with LOCK:
        jid = BY_URL.get(url)
        if jid and JOBS.get(jid, {}).get("state") != "error":
            return jid
        if sum(1 for j in JOBS.values() if j["state"] in ("queued", "running")) > 8:
            raise HTTPException(429, "busy")
        jid = uuid.uuid4().hex
        JOBS[jid] = {"url": url, "state": "queued", "progress": 0, "at": time.time()}
        BY_URL[url] = jid
    threading.Thread(target=run, args=(jid, url), daemon=True).start()
    return jid

@app.post("/x/job")
async def xjob(req: Request):
    url = ((await req.json()).get("url") or "").strip()
    if not PLAT.match(url):
        raise HTTPException(400, "invalid_url")
    return {"id": create_job(url)}

@app.get("/x/job/{jid}")
def xstatus(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "no job")
    return {k: v for k, v in j.items() if k not in ("file", "url")}

@app.get("/x/file/{jid}")
def xfile(jid: str):
    j = JOBS.get(jid)
    if not j or j.get("state") != "done":
        raise HTTPException(404, "not ready")
    return FileResponse(j["file"], media_type="video/mp4", filename="video." + j["ext"],
                        headers={"cache-control": "no-store"})

@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"),
                        media_type="text/html; charset=utf-8", headers={"cache-control": "no-cache"})

@app.get("/health")
def health():
    pot = False
    try:
        urllib.request.urlopen("http://127.0.0.1:4416/ping", timeout=2); pot = True
    except Exception:
        pass
    return {"ok": True, "ytdlp": yt_dlp.version.__version__, "pot": pot}
