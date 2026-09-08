import csv
import io
import os
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

import requests
from qstash import QStash, Receiver
from upstash_redis import Redis
from vercel.blob import BlobClient

from blog_downloader import extract_content, filename_from_url, markdown_to_docx

TASK_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_URLS_PER_TASK = 100
MAX_ATTEMPTS = 3
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))
WORKER_PARALLELISM = int(os.getenv("WORKER_PARALLELISM", "3"))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _task_key(task_id):
    return f"blog-task:{task_id}"


def _item_key(task_id, index):
    return f"blog-task:{task_id}:item:{index}"


def _lock_key(task_id, index):
    return f"blog-task:{task_id}:lock:{index}"


def _redis():
    return Redis.from_env()


def _qstash():
    token = os.environ["QSTASH_TOKEN"]
    return QStash(token, base_url=os.environ["QSTASH_URL"])


def _blob():
    return BlobClient()


def _worker_headers():
    secret = os.getenv("VERCEL_AUTOMATION_BYPASS_SECRET")
    return {"x-vercel-protection-bypass": secret} if secret else {}


def require_background_config():
    required = [
        "UPSTASH_REDIS_REST_URL",
        "UPSTASH_REDIS_REST_TOKEN",
        "QSTASH_URL",
        "QSTASH_TOKEN",
        "QSTASH_CURRENT_SIGNING_KEY",
        "QSTASH_NEXT_SIGNING_KEY",
        "BLOB_READ_WRITE_TOKEN",
    ]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("缺少后台任务环境变量：" + ", ".join(missing))


def verify_qstash_request(signature, body, url):
    receiver = Receiver(
        current_signing_key=os.environ["QSTASH_CURRENT_SIGNING_KEY"],
        next_signing_key=os.environ["QSTASH_NEXT_SIGNING_KEY"],
    )
    receiver.verify(signature=signature, body=body, url=url)


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _publish_items(task_id, urls, worker_url):
    messages = []
    indexed_urls = [{"index": index, "url": url} for index, url in enumerate(urls, 1)]
    for batch_number, items in enumerate(_chunks(indexed_urls, BATCH_SIZE), 1):
        messages.append(
            {
                "url": worker_url,
                "body": {"task_id": task_id, "items": items, "attempt": 1},
                "headers": _worker_headers(),
                "retries": 2,
                "deduplication_id": f"{task_id}-batch-{batch_number}-1",
                "flow_control": {
                    "key": "blog-download-workers",
                    "parallelism": WORKER_PARALLELISM,
                },
                "label": f"blog-task-{task_id}",
            }
        )
    _qstash().message.batch_json(messages)


def _publish_retry(task_id, items, attempt, worker_url):
    attempt += 1
    _qstash().message.publish_json(
        url=worker_url,
        body={"task_id": task_id, "items": items, "attempt": attempt},
        headers=_worker_headers(),
        retries=2,
        delay=min(60, 5 * attempt),
        deduplication_id=f"{task_id}-retry-{'-'.join(str(item['index']) for item in items)}-{attempt}",
        flow_control={
            "key": "blog-download-workers",
            "parallelism": WORKER_PARALLELISM,
        },
        label=f"blog-task-{task_id}",
    )


def create_task(urls, worker_url):
    require_background_config()
    task_id = uuid.uuid4().hex
    redis = _redis()
    task_key = _task_key(task_id)
    redis.hset(
        task_key,
        values={
            "id": task_id,
            "status": "queued",
            "total": len(urls),
            "success": 0,
            "failed": 0,
            "created_at": _now(),
            "updated_at": _now(),
        },
    )
    redis.expire(task_key, TASK_TTL_SECONDS)
    for index, url in enumerate(urls, 1):
        key = _item_key(task_id, index)
        redis.hset(key, values={"url": url, "status": "queued", "attempt": 0})
        redis.expire(key, TASK_TTL_SECONDS)
    try:
        _publish_items(task_id, urls, worker_url)
    except Exception as exc:
        redis.hset(
            task_key,
            values={"status": "queue_error", "error": str(exc), "updated_at": _now()},
        )
        raise
    return task_id


def get_task(task_id, include_items=False):
    redis = _redis()
    task = redis.hgetall(_task_key(task_id))
    if not task:
        return None
    for field in ("total", "success", "failed"):
        task[field] = int(task.get(field, 0))
    task["processed"] = task["success"] + task["failed"]
    task["progress"] = round(task["processed"] * 100 / task["total"]) if task["total"] else 0
    if include_items:
        task["items"] = [
            {"index": index, **redis.hgetall(_item_key(task_id, index))}
            for index in range(1, task["total"] + 1)
        ]
    return task


def _upload_artifact(path, content, content_type):
    result = _blob().put(
        path,
        content,
        access="public",
        content_type=content_type,
        add_random_suffix=False,
        overwrite=True,
    )
    return result.url, result.download_url


def _mark_terminal(task_id, index, values, success):
    redis = _redis()
    item_key = _item_key(task_id, index)
    current = redis.hgetall(item_key)
    if current.get("status") in {"completed", "failed"}:
        return False
    redis.hset(item_key, values=values)
    redis.hincrby(_task_key(task_id), "success" if success else "failed", 1)
    redis.hset(_task_key(task_id), values={"status": "processing", "updated_at": _now()})
    return True


def _process_item(task_id, item_payload, attempt):
    index = int(item_payload["index"])
    redis = _redis()
    item_key = _item_key(task_id, index)
    item = redis.hgetall(item_key)
    if item.get("status") in {"completed", "failed"}:
        return True
    if not redis.set(_lock_key(task_id, index), "1", nx=True, ex=240):
        return False
    try:
        redis.hset(_task_key(task_id), values={"status": "processing", "updated_at": _now()})
        redis.hset(item_key, values={"status": "processing", "attempt": attempt})
        content, word_count = extract_content(item_payload["url"])
        base = filename_from_url(item_payload["url"])
        prefix = f"blog-tasks/{task_id}/{index:03d}-{base}"
        docx_url, docx_download_url = _upload_artifact(
            f"{prefix}.docx",
            markdown_to_docx(content),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        _mark_terminal(
            task_id,
            index,
            {
                "status": "completed",
                "attempt": attempt,
                "word_count": word_count,
                "docx_url": docx_url,
                "docx_download_url": docx_download_url,
                "finished_at": _now(),
            },
            success=True,
        )
        return True
    except Exception as exc:
        redis.hset(item_key, values={"status": "retrying", "attempt": attempt, "error": str(exc)})
        if attempt >= MAX_ATTEMPTS:
            _mark_terminal(
                task_id,
                index,
                {
                    "status": "failed",
                    "attempt": attempt,
                    "error": str(exc),
                    "finished_at": _now(),
                },
                success=False,
            )
            return True
        return False
    finally:
        redis.delete(_lock_key(task_id, index))


def process_batch(payload, worker_url):
    task_id = payload["task_id"]
    attempt = int(payload.get("attempt", 1))
    if not get_task(task_id):
        return
    retry_items = []
    for item in payload["items"]:
        if not _process_item(task_id, item, attempt):
            retry_items.append(item)
    if retry_items and attempt < MAX_ATTEMPTS:
        _publish_retry(task_id, retry_items, attempt, worker_url)
    maybe_finalize_task(task_id)


def _download_blob(url):
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.content


def _build_task_zip(task):
    output = io.BytesIO()
    failures = []
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in task["items"]:
            index = int(item["index"])
            base = filename_from_url(item.get("url", ""))
            if item.get("status") == "completed":
                archive.writestr(f"{index:03d}-{base}.docx", _download_blob(item["docx_url"]))
            else:
                failures.append([index, item.get("url", ""), item.get("error", "未知错误")])
        if failures:
            text = io.StringIO()
            writer = csv.writer(text)
            writer.writerow(["序号", "URL", "错误"])
            writer.writerows(failures)
            archive.writestr("失败记录.csv", text.getvalue().encode("utf-8-sig"))
    return output.getvalue()


def maybe_finalize_task(task_id):
    redis = _redis()
    task = get_task(task_id, include_items=True)
    if not task or task["processed"] < task["total"] or task.get("zip_download_url"):
        return
    finalizer_key = f"blog-task:{task_id}:finalizing"
    if not redis.set(finalizer_key, "1", nx=True, ex=300):
        return
    try:
        redis.hset(_task_key(task_id), values={"status": "finalizing", "updated_at": _now()})
        zip_bytes = _build_task_zip(task)
        result = _blob().put(
            f"blog-tasks/{task_id}/blog-documents.zip",
            zip_bytes,
            access="public",
            content_type="application/zip",
            add_random_suffix=False,
            overwrite=True,
        )
        source_urls = [
            item["docx_url"]
            for item in task["items"]
            if item.get("status") == "completed" and item.get("docx_url")
        ]
        if source_urls:
            try:
                _blob().delete(source_urls)
            except Exception:
                pass
        status = "completed_with_errors" if task["failed"] else "completed"
        redis.hset(
            _task_key(task_id),
            values={
                "status": status,
                "zip_url": result.url,
                "zip_download_url": result.download_url,
                "finished_at": _now(),
                "updated_at": _now(),
            },
        )
        redis.hdel(_task_key(task_id), "error")
    except Exception as exc:
        redis.hset(
            _task_key(task_id),
            values={"status": "finalize_error", "error": str(exc), "updated_at": _now()},
        )
        raise
    finally:
        redis.delete(finalizer_key)


def cleanup_old_blobs(retention_days=7, limit=1000):
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    blob = _blob()
    expired = []
    for item in blob.iter_objects(prefix="blog-tasks/", limit=limit):
        uploaded_at = item.uploaded_at
        if uploaded_at.tzinfo is None:
            uploaded_at = uploaded_at.replace(tzinfo=timezone.utc)
        if uploaded_at < cutoff:
            expired.append(item.url)
    for urls in _chunks(expired, 100):
        blob.delete(urls)
    return len(expired)
