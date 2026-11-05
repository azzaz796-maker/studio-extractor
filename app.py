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

@app.get("/health")
def health():
    return {"ok": True, "ytdlp": yt_dlp.version.__version__}

@app.post("/job")
async def job(req: Request):
    auth(req)
    cleanup()
    body = await req.json()
    url = (body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "bad url")
    with LOCK:
        jid = BY_URL.get(url)
        if jid and JOBS.get(jid, {}).get("state") != "error":
            return {"id": jid}
        jid = uuid.uuid4().hex
        JOBS[jid] = {"url": url, "state": "queued", "progress": 0, "at": time.time()}
        BY_URL[url] = jid
    threading.Thread(target=run, args=(jid, url), daemon=True).start()
    return {"id": jid}

@app.get("/job/{jid}")
def status(jid: str, req: Request):
    auth(req)
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "no job")
    return {k: v for k, v in j.items() if k not in ("file",)}

@app.get("/file/{jid}")
def file(jid: str, req: Request):
    auth(req)
    j = JOBS.get(jid)
    if not j or j.get("state") != "done":
        raise HTTPException(404, "not ready")
    return FileResponse(j["file"], media_type="video/mp4" if j["ext"] == "mp4" else "application/octet-stream",
                        filename="video." + j["ext"])
