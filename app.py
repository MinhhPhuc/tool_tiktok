#!/usr/bin/env python3
"""
Đăng TikTok đa tài khoản - ứng dụng web chạy trên máy của bạn.

Chạy:   pip install -r requirements.txt   rồi   python app.py
Mở:     http://localhost:5000

Dữ liệu (token, hàng đợi, video) nằm trong thư mục ./data
"""
import hashlib
import html
import json
import os
import re
import secrets
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

API = "https://open.tiktokapis.com"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
CHUNK = 10 * 1024 * 1024  # 10MB mỗi đoạn upload
ALLOWED_EXT = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
VIDEOS = DATA / "videos"
VIDEOS.mkdir(parents=True, exist_ok=True)
ACCOUNTS_FILE = DATA / "accounts.json"
JOBS_FILE = DATA / "jobs.json"
CONFIG_FILE = DATA / "config.json"

LOCK = threading.RLock()
PENDING = {}  # state -> {name, verifier, created}

app = Flask(__name__, static_folder=str(BASE / "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024


# ---------------------------------------------------------------- lưu trữ
# Nếu đặt DATABASE_URL (Postgres), tài khoản/hàng đợi/cài đặt được lưu trong DB thay vì file.
# Cần thiết trên các host có ổ đĩa tạm như gói miễn phí của Render (file bị xóa khi dịch vụ ngủ hoặc khởi động lại).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
KV_KEYS = {"accounts.json": "accounts", "jobs.json": "jobs", "config.json": "config"}


def db_connect():
    import psycopg2  # chỉ cần khi dùng DATABASE_URL
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def db_init():
    conn = db_connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    finally:
        conn.close()


def db_get(key):
    conn = db_connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT v FROM kv WHERE k = %s", (key,))
            row = cur.fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def db_set(key, data):
    conn = db_connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kv (k, v) VALUES (%s, %s) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
                (key, json.dumps(data, ensure_ascii=False)),
            )
    finally:
        conn.close()


def read_json(path, default):
    key = KV_KEYS.get(path.name)
    if DATABASE_URL and key:
        # Lỗi DB phải nổ ra, không trả về giá trị rỗng, để không ghi đè nhầm dữ liệu thật.
        v = db_get(key)
        return default if v is None else v
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data, private=False):
    key = KV_KEYS.get(path.name)
    if DATABASE_URL and key:
        db_set(key, data)
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    if private:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


ENV_CONFIG = {
    "client_key": "TIKTOK_CLIENT_KEY",
    "client_secret": "TIKTOK_CLIENT_SECRET",
    "redirect_uri": "TIKTOK_REDIRECT_URI",
    "app_name": "APP_NAME",
    "contact_email": "CONTACT_EMAIL",
    "delay": "POST_DELAY",
}
_CFG_CACHE = {"t": 0.0, "v": None}


def load_config():
    """Mặc định < cài đặt đã lưu < biến môi trường. Có cache vài giây vì worker đọc liên tục."""
    now = time.time()
    if _CFG_CACHE["v"] is None or now - _CFG_CACHE["t"] > 3:
        ext = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
        c = {"client_key": "", "client_secret": "", "delay": 45, "app_name": "", "contact_email": "",
             "redirect_uri": (ext + "/callback") if ext else "http://localhost:5000/callback"}
        c.update(read_json(CONFIG_FILE, {}))
        for k, env in ENV_CONFIG.items():
            if os.environ.get(env):
                c[k] = os.environ[env].strip()
        try:
            c["delay"] = int(c["delay"])
        except (TypeError, ValueError):
            c["delay"] = 45
        _CFG_CACHE.update(t=now, v=c)
    return dict(_CFG_CACHE["v"])


def load_accounts():
    return read_json(ACCOUNTS_FILE, {})


def save_accounts(a):
    write_json(ACCOUNTS_FILE, a, private=True)


if DATABASE_URL:
    db_init()
JOBS = read_json(JOBS_FILE, [])
for _j in JOBS:  # công việc dang dở khi tắt app
    if _j["status"] in ("uploading", "processing"):
        _j["status"] = "failed"
        _j["error"] = "Bị gián đoạn vì ứng dụng đã tắt. Bấm Thử lại để đăng lại."


def save_jobs():
    write_json(JOBS_FILE, JOBS)


def err(msg, code=400):
    return jsonify({"error": msg}), code


# ---------------------------------------------------------------- TikTok API
def check(resp):
    try:
        j = resp.json()
    except ValueError:
        raise RuntimeError(f"Phản hồi không hợp lệ từ TikTok ({resp.status_code})")
    e = j.get("error")
    if isinstance(e, dict) and e.get("code") not in (None, "ok"):
        raise RuntimeError(f"{e.get('code')}: {e.get('message')}")
    if isinstance(e, str) and e:
        raise RuntimeError(f"{e}: {j.get('error_description', '')}")
    return j


def friendly(msg):
    m = msg.lower()
    if "connectionpool" in m or "max retries" in m or "timed out" in m:
        return "Không kết nối được tới TikTok. Kiểm tra mạng rồi bấm Thử lại."
    if "unaudited_client" in m:
        return "App TikTok của bạn chưa được duyệt nên chỉ đăng được ở chế độ 'Chỉ mình tôi'. (" + msg + ")"
    if "spam_risk" in m or "rate_limit" in m:
        return "TikTok đang giới hạn tài khoản này (nghi spam hoặc đăng quá nhiều). Thử lại sau. (" + msg + ")"
    if "access_token" in m or "token" in m and "invalid" in m:
        return "Token không còn hiệu lực, hãy kết nối lại tài khoản. (" + msg + ")"
    return msg


def token_request(payload):
    c = load_config()
    if not c["client_key"] or not c["client_secret"]:
        raise RuntimeError("Chưa nhập Client key / Client secret trong Cài đặt")
    r = requests.post(
        f"{API}/v2/oauth/token/",
        data={"client_key": c["client_key"], "client_secret": c["client_secret"], **payload},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    return check(r)


def pack_token(t, old=None):
    now = int(time.time())
    out = dict(old or {})
    out.update({
        "open_id": t["open_id"],
        "access_token": t["access_token"],
        "refresh_token": t["refresh_token"],
        "expires_at": now + int(t["expires_in"]),
        "refresh_expires_at": now + int(t.get("refresh_expires_in", 0)) if t.get("refresh_expires_in") else 0,
        "scope": t.get("scope", ""),
    })
    return out


def get_access_token(name):
    with LOCK:
        accounts = load_accounts()
        if name not in accounts:
            raise RuntimeError(f"Tài khoản '{name}' đã bị gỡ")
        acc = accounts[name]
        now = time.time()
        if acc.get("refresh_expires_at") and acc["refresh_expires_at"] < now:
            raise RuntimeError("Phiên kết nối đã hết hạn, hãy kết nối lại tài khoản")
        if acc["expires_at"] - 300 < now:
            t = token_request({"grant_type": "refresh_token", "refresh_token": acc["refresh_token"]})
            acc = pack_token(t, acc)
            accounts[name] = acc
            save_accounts(accounts)
        return acc["access_token"]


def fetch_profile(token):
    try:
        r = requests.get(
            f"{API}/v2/user/info/?fields=open_id,display_name,avatar_url",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        return check(r).get("data", {}).get("user", {})
    except Exception:
        return {}


def finish_auth(code, state):
    with LOCK:
        for s in [s for s, p in PENDING.items() if time.time() - p["created"] > 900]:
            PENDING.pop(s, None)
        p = PENDING.pop(state, None)
    if not p:
        raise RuntimeError("Phiên cấp quyền không hợp lệ hoặc đã hết hạn, hãy thử thêm tài khoản lại")
    t = token_request({
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": load_config()["redirect_uri"],
        "code_verifier": p["verifier"],
    })
    prof = fetch_profile(t["access_token"])
    with LOCK:
        accounts = load_accounts()
        acc = pack_token(t)
        acc.update({
            "display_name": prof.get("display_name", ""),
            "avatar_url": prof.get("avatar_url", ""),
            "added_at": time.time(),
        })
        accounts[p["name"]] = acc
        save_accounts(accounts)
    return p["name"]


def upload_video(token, path, job, on_progress):
    size = path.stat().st_size
    chunk, total = (size, 1) if size <= CHUNK else (CHUNK, size // CHUNK)
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": total}

    if job["mode"] == "direct":
        info = check(requests.post(f"{API}/v2/post/publish/creator_info/query/", headers=auth, timeout=30))["data"]
        options = info.get("privacy_level_options", [])
        if job["privacy"] not in options:
            raise RuntimeError(f"Tài khoản này không cho phép chế độ '{job['privacy']}'. Các chế độ hợp lệ: {options}")
        body = {
            "post_info": {
                "title": job["caption"],
                "privacy_level": job["privacy"],
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": source,
        }
        endpoint = "/v2/post/publish/video/init/"
    else:
        body = {"source_info": source}
        endpoint = "/v2/post/publish/inbox/video/init/"

    data = check(requests.post(API + endpoint, headers=auth, json=body, timeout=30))["data"]
    publish_id, upload_url = data["publish_id"], data["upload_url"]
    ctype = ALLOWED_EXT.get(path.suffix.lower(), "video/mp4")

    with open(path, "rb") as f:
        for i in range(total):
            start = i * chunk
            end = size - 1 if i == total - 1 else start + chunk - 1
            buf = f.read(end - start + 1)
            r = requests.put(
                upload_url,
                data=buf,
                timeout=300,
                headers={
                    "Content-Type": ctype,
                    "Content-Length": str(len(buf)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                },
            )
            if r.status_code not in (200, 201, 206):
                raise RuntimeError(f"Tải đoạn {i + 1}/{total} thất bại ({r.status_code}): {r.text[:200]}")
            on_progress(int((i + 1) / total * 90), "uploading", publish_id)

    on_progress(92, "processing", publish_id)
    for _ in range(36):  # chờ tối đa ~6 phút
        st = check(requests.post(
            f"{API}/v2/post/publish/status/fetch/", headers=auth,
            json={"publish_id": publish_id}, timeout=30))["data"]
        status = st.get("status")
        if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return publish_id
        if status == "FAILED":
            raise RuntimeError(f"TikTok báo thất bại: {st.get('fail_reason')}")
        time.sleep(10)
    raise RuntimeError("TikTok xử lý quá lâu. Hãy kiểm tra trong app TikTok rồi thử lại nếu cần.")


# ---------------------------------------------------------------- hàng đợi
def update_job(job, **kw):
    with LOCK:
        job.update(kw)
        save_jobs()


def run_job(job):
    token = get_access_token(job["account"])
    path = VIDEOS / job["video"]
    if not path.is_file():
        raise RuntimeError("Không còn file video (host dùng ổ đĩa tạm sẽ xóa file khi khởi động lại). Hãy thêm lại bài đăng.")

    def on_progress(pct, status, pid):
        update_job(job, progress=pct, status=status, publish_id=pid)

    pid = upload_video(token, path, job, on_progress)
    update_job(job, status="done", progress=100, publish_id=pid, error="", finished_at=time.time())


def worker():
    last_finish = 0.0
    while True:
        job = None
        with LOCK:
            now = time.time()
            if now - last_finish >= load_config()["delay"]:
                due = [j for j in JOBS if j["status"] == "queued"
                       and (not j.get("scheduled_at") or j["scheduled_at"] <= now)]
                due.sort(key=lambda j: j.get("scheduled_at") or j["created_at"])
                if due:
                    job = due[0]
                    job.update(status="uploading", progress=0, error="")
                    save_jobs()
        if not job:
            time.sleep(1.5)
            continue
        try:
            run_job(job)
        except Exception as e:  # noqa: BLE001
            update_job(job, status="failed", error=friendly(str(e)), finished_at=time.time())
        last_finish = time.time()


# ---------------------------------------------------------------- giao diện
# --- Chế độ công khai: đặt biến môi trường APP_PASSWORD khi đưa app lên mạng ---
# Có mật khẩu: "/" là trang giới thiệu công khai, dashboard nằm ở "/app" và API cần đăng nhập.
# Không có mật khẩu (chạy trên máy cá nhân): "/" mở thẳng dashboard.
PUBLIC_PATHS = {"/", "/terms", "/privacy", "/callback", "/static/site.css", "/healthz"}


def is_public(path):
    return path in PUBLIC_PATHS or re.fullmatch(r"/tiktok[\w\-]+\.txt", path) is not None


@app.before_request
def gate():
    pw = os.environ.get("APP_PASSWORD")
    if not pw or is_public(request.path):
        return None
    a = request.authorization
    user = os.environ.get("APP_USER", "admin")
    if a and secrets.compare_digest((a.username or "").encode(), user.encode()) \
            and secrets.compare_digest((a.password or "").encode(), pw.encode()):
        return None
    return Response("Cần đăng nhập.", 401, {"WWW-Authenticate": 'Basic realm="Dashboard"'})


def render_page(name):
    c = load_config()
    text = (BASE / "static" / name).read_text(encoding="utf-8")
    text = text.replace("[APP NAME]", html.escape(c["app_name"] or "Your App Name"))
    text = text.replace("[CONTACT EMAIL]", html.escape(c["contact_email"] or "contact@example.com"))
    return Response(text, mimetype="text/html")


@app.get("/")
def index():
    if os.environ.get("APP_PASSWORD"):
        return render_page("landing.html")
    return send_from_directory(app.static_folder, "index.html")


@app.get("/app")
def dashboard():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/terms")
def terms():
    return render_page("terms.html")


@app.get("/privacy")
def privacy():
    return render_page("privacy.html")


@app.get("/<name>.txt")
def verify_file(name):
    """TikTok có thể yêu cầu xác minh URL bằng file tiktokXXXX.txt ở thư mục gốc: để file vào ./verify/"""
    if not re.fullmatch(r"tiktok[\w\-]+", name):
        return err("Không tìm thấy", 404)
    return send_from_directory(BASE / "verify", name + ".txt", mimetype="text/plain")


@app.get("/api/config")
def api_get_config():
    c = load_config()
    return jsonify({
        "client_key": c["client_key"],
        "has_secret": bool(c["client_secret"]),
        "redirect_uri": c["redirect_uri"],
        "delay": c["delay"],
        "app_name": c["app_name"],
        "contact_email": c["contact_email"],
    })


@app.post("/api/config")
def api_set_config():
    body = request.get_json(silent=True) or {}
    with LOCK:
        c = read_json(CONFIG_FILE, {})
        for k in ("client_key", "redirect_uri", "app_name", "contact_email"):
            if isinstance(body.get(k), str):
                c[k] = body[k].strip()
        if isinstance(body.get("client_secret"), str) and body["client_secret"].strip():
            c["client_secret"] = body["client_secret"].strip()
        if "delay" in body:
            try:
                c["delay"] = max(0, min(3600, int(body["delay"])))
            except (TypeError, ValueError):
                return err("Thời gian nghỉ phải là số giây")
        write_json(CONFIG_FILE, c, private=True)
        _CFG_CACHE["v"] = None
    return jsonify({"ok": True})


@app.get("/api/accounts")
def api_accounts():
    now = time.time()
    out = []
    for name, a in load_accounts().items():
        out.append({
            "name": name,
            "display_name": a.get("display_name", ""),
            "avatar_url": a.get("avatar_url", ""),
            "added_at": a.get("added_at", 0),
            "needs_reconnect": bool(a.get("refresh_expires_at")) and a["refresh_expires_at"] < now,
        })
    return jsonify(out)


@app.delete("/api/accounts/<name>")
def api_delete_account(name):
    with LOCK:
        accounts = load_accounts()
        if name not in accounts:
            return err("Không tìm thấy tài khoản", 404)
        if any(j["account"] == name and j["status"] in ("uploading", "processing") for j in JOBS):
            return err("Tài khoản đang đăng video, hãy đợi xong rồi gỡ", 409)
        accounts.pop(name)
        save_accounts(accounts)
    return jsonify({"ok": True})


@app.post("/api/accounts/connect")
def api_connect():
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not re.fullmatch(r"[\w\- .]{1,40}", name):
        return err("Tên gọi chỉ gồm chữ, số, dấu gạch, tối đa 40 ký tự")
    c = load_config()
    if not c["client_key"] or not c["client_secret"]:
        return err("Hãy nhập Client key và Client secret trong Cài đặt trước")
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(16)
    started = time.time()
    with LOCK:
        PENDING[state] = {"name": name, "verifier": verifier, "created": started}
    url = AUTH_URL + "?" + urlencode({
        "client_key": c["client_key"],
        "scope": "user.info.basic,video.publish,video.upload",
        "response_type": "code",
        "redirect_uri": c["redirect_uri"],
        "state": state,
        "code_challenge": hashlib.sha256(verifier.encode()).hexdigest(),  # TikTok dùng hex
        "code_challenge_method": "S256",
    })
    return jsonify({"url": url, "started": started})


@app.post("/api/accounts/finish")
def api_finish():
    """Dự phòng: người dùng dán URL chuyển hướng nếu trình duyệt không tự quay về."""
    pasted = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    qs = parse_qs(urlparse(pasted).query)
    if "code" not in qs or "state" not in qs:
        return err("URL không chứa mã cấp quyền. Hãy copy toàn bộ URL trên thanh địa chỉ.")
    try:
        name = finish_auth(qs["code"][0], qs["state"][0])
    except Exception as e:  # noqa: BLE001
        return err(friendly(str(e)))
    return jsonify({"ok": True, "name": name})


PAGE = """<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><body style="font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#f2f3f8;color:#161a2e">
<div style="max-width:420px;padding:32px;text-align:center"><h1 style="font-size:22px;margin:0 0 8px">{title}</h1>
<p style="color:#636a84;margin:0 0 20px">{msg}</p><a href="/" style="color:#fe2c55;font-weight:600">Về trang chính</a></div>{script}"""


@app.get("/callback")
def callback():
    if request.args.get("error"):
        msg = html.escape(request.args.get("error_description") or request.args["error"])
        return PAGE.format(title="Chưa kết nối được", msg=msg, script=""), 400
    try:
        name = finish_auth(request.args.get("code", ""), request.args.get("state", ""))
    except Exception as e:  # noqa: BLE001
        return PAGE.format(title="Chưa kết nối được", msg=html.escape(friendly(str(e))), script=""), 400
    return PAGE.format(
        title="Đã kết nối " + html.escape(name),
        msg="Bạn có thể đóng tab này và quay lại ứng dụng.",
        script="<script>setTimeout(()=>window.close(),1500)</script>",
    )


@app.get("/api/jobs")
def api_jobs():
    with LOCK:
        rows = sorted(JOBS, key=lambda j: j["created_at"], reverse=True)
        return jsonify([{k: v for k, v in j.items() if k != "video"} for j in rows])


@app.post("/api/jobs")
def api_create_jobs():
    f = request.files.get("video")
    if not f or not f.filename:
        return err("Chưa chọn video")
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return err("Chỉ hỗ trợ video .mp4, .mov, .webm")
    try:
        names = json.loads(request.form.get("accounts", "[]"))
    except ValueError:
        names = []
    accounts = load_accounts()
    names = [n for n in dict.fromkeys(names) if n in accounts]
    if not names:
        return err("Chưa chọn tài khoản nào")
    caption = request.form.get("caption", "")
    if len(caption) > 2200:
        return err("Caption tối đa 2200 ký tự")
    mode = request.form.get("mode", "direct")
    if mode not in ("direct", "draft"):
        return err("Chế độ đăng không hợp lệ")
    privacy = request.form.get("privacy", "SELF_ONLY")
    if privacy not in ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"):
        return err("Quyền riêng tư không hợp lệ")
    sched = None
    if request.form.get("scheduled_at"):
        try:
            sched = float(request.form["scheduled_at"])
        except ValueError:
            return err("Thời gian hẹn giờ không hợp lệ")

    stored = f"{uuid.uuid4().hex[:12]}_{secure_filename(f.filename) or 'video' + ext}"
    f.save(VIDEOS / stored)

    created = []
    with LOCK:
        for n in names:
            job = {
                "id": uuid.uuid4().hex[:10],
                "account": n,
                "video": stored,
                "video_name": f.filename,
                "caption": caption,
                "privacy": privacy,
                "mode": mode,
                "scheduled_at": sched,
                "status": "queued",
                "progress": 0,
                "error": "",
                "publish_id": "",
                "created_at": time.time(),
            }
            JOBS.append(job)
            created.append(job["id"])
        save_jobs()
    return jsonify({"created": created})


@app.post("/api/jobs/<jid>/retry")
def api_retry(jid):
    with LOCK:
        job = next((j for j in JOBS if j["id"] == jid), None)
        if not job:
            return err("Không tìm thấy công việc", 404)
        if job["status"] != "failed":
            return err("Chỉ thử lại được công việc bị lỗi", 409)
        job.update(status="queued", progress=0, error="", scheduled_at=None, created_at=time.time())
        save_jobs()
    return jsonify({"ok": True})


@app.delete("/api/jobs/<jid>")
def api_delete_job(jid):
    with LOCK:
        job = next((j for j in JOBS if j["id"] == jid), None)
        if not job:
            return err("Không tìm thấy công việc", 404)
        if job["status"] in ("uploading", "processing"):
            return err("Video đang được đăng, chưa thể xóa", 409)
        JOBS.remove(job)
        save_jobs()
        if not any(j["video"] == job["video"] for j in JOBS):
            try:
                (VIDEOS / job["video"]).unlink()
            except OSError:
                pass
    return jsonify({"ok": True})


_WORKER_STARTED = False


def start_worker():
    global _WORKER_STARTED
    with LOCK:
        if not _WORKER_STARTED:
            _WORKER_STARTED = True
            threading.Thread(target=worker, daemon=True).start()


if __name__ != "__main__" and not os.environ.get("NO_WORKER"):
    start_worker()  # chạy dưới gunicorn: dùng đúng 1 worker process (xem render.yaml)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    start_worker()
    start_path = "/app" if os.environ.get("APP_PASSWORD") else "/"
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}{start_path}")).start()
    print(f"Đang chạy tại http://localhost:{port}  (Ctrl+C để dừng)")
    app.run(host="127.0.0.1", port=port, threaded=True)
