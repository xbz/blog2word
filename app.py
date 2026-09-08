import os
import re

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from task_service import (
    MAX_URLS_PER_TASK,
    cleanup_old_blobs,
    create_task,
    get_task,
    maybe_finalize_task,
    process_batch,
    verify_qstash_request,
)

app = Flask(__name__)


def _preview_task(task_id, include_items=False):
    if os.getenv("LOCAL_PREVIEW") != "1" or task_id != "preview":
        return None
    task = {
        "id": "preview",
        "status": "processing",
        "total": 50,
        "success": 31,
        "failed": 2,
        "processed": 33,
        "progress": 66,
    }
    if include_items:
        task["items"] = [
            {
                "index": index,
                "url": f"https://example.com/blog/article-{index}",
                "status": "completed" if index <= 31 else ("failed" if index <= 33 else "queued"),
                "error": "示例：页面抓取超时" if 31 < index <= 33 else "",
            }
            for index in range(1, 51)
        ]
    return task


def _get_task(task_id, include_items=False):
    return _preview_task(task_id, include_items) or get_task(task_id, include_items)


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/tasks")
def submit_task():
    urls = [
        line.strip()
        for line in request.form.get("urls", "").splitlines()
        if line.strip()
    ]
    if not urls or any(not re.match(r"^https?://", url, re.I) for url in urls):
        return render_template("index.html", error="请每行输入一个有效的 http/https URL。"), 400
    if len(urls) > MAX_URLS_PER_TASK:
        return render_template("index.html", error=f"单次最多处理 {MAX_URLS_PER_TASK} 个 URL。"), 400
    try:
        base_url = os.getenv("APP_BASE_URL", request.url_root.rstrip("/"))
        task_id = create_task(urls, f"{base_url}/internal/process")
    except Exception as exc:
        return render_template("index.html", error=f"任务提交失败：{exc}"), 502
    return redirect(url_for("task_page", task_id=task_id))


@app.get("/tasks/<task_id>")
def task_page(task_id):
    task = _get_task(task_id)
    if not task:
        abort(404)
    return render_template("task.html", task=task)


@app.get("/api/tasks/<task_id>")
def task_status(task_id):
    task = _get_task(task_id, include_items=True)
    if not task:
        abort(404)
    return jsonify(task)


@app.get("/tasks/<task_id>/download")
def download_task(task_id):
    task = get_task(task_id)
    if not task:
        abort(404)
    if not task.get("zip_download_url"):
        abort(409)
    return redirect(task["zip_download_url"], code=302)


@app.post("/tasks/<task_id>/retry-finalize")
def retry_finalize(task_id):
    task = get_task(task_id)
    if not task:
        abort(404)
    if task.get("status") != "finalize_error":
        abort(409)
    try:
        maybe_finalize_task(task_id)
    except Exception:
        pass
    return redirect(url_for("task_page", task_id=task_id))


@app.post("/internal/process")
def worker():
    raw_body = request.get_data(as_text=True)
    if os.getenv("DISABLE_QSTASH_SIGNATURE_VERIFY") != "1":
        signature = request.headers.get("Upstash-Signature")
        if not signature:
            abort(401)
        try:
            verify_qstash_request(signature, raw_body, request.url)
        except Exception:
            abort(401)
    payload = request.get_json(force=True)
    base_url = os.getenv("APP_BASE_URL", request.url_root.rstrip("/"))
    process_batch(payload, f"{base_url}/internal/process")
    return jsonify({"ok": True})


@app.get("/internal/cleanup")
def cleanup():
    secret = os.getenv("CRON_SECRET")
    if not secret or request.headers.get("Authorization") != f"Bearer {secret}":
        abort(401)
    return jsonify({"deleted": cleanup_old_blobs()})
