import io
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import task_service


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.values = {}

    def hset(self, key, field=None, value=None, values=None):
        target = self.hashes.setdefault(key, {})
        if values:
            target.update({str(k): str(v) for k, v in values.items()})
        elif field is not None:
            target[str(field)] = str(value)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hincrby(self, key, field, increment):
        target = self.hashes.setdefault(key, {})
        target[field] = str(int(target.get(field, 0)) + increment)
        return int(target[field])

    def expire(self, key, seconds):
        return True

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return "OK"

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)

    def hdel(self, key, *fields):
        for field in fields:
            self.hashes.get(key, {}).pop(field, None)


class FakeBlob:
    def __init__(self):
        self.files = {}

    def put(self, path, body, **kwargs):
        self.files[path] = body
        return SimpleNamespace(
            url=f"https://blob.example/{path}",
            download_url=f"https://blob.example/{path}?download=1",
        )

    def get(self, url, **kwargs):
        path = url.removeprefix("https://blob.example/")
        return SimpleNamespace(content=self.files[path])

    def iter_objects(self, **kwargs):
        return iter([])

    def delete(self, urls):
        for url in urls:
            self.files.pop(url.removeprefix("https://blob.example/"), None)


class TaskServiceTest(unittest.TestCase):
    def test_duplicate_url_submissions_create_distinct_queued_tasks(self):
        redis = FakeRedis()
        url = "https://example.com/same"
        with (
            patch.object(task_service, "require_background_config"),
            patch.object(task_service, "_redis", return_value=redis),
            patch.object(task_service, "_publish_items"),
        ):
            first = task_service.create_task([url], "https://app.example/internal/process")
            second = task_service.create_task([url], "https://app.example/internal/process")
            first_task = task_service.get_task(first, include_items=True)
            second_task = task_service.get_task(second, include_items=True)
        self.assertNotEqual(first, second)
        self.assertEqual(first_task["status"], "queued")
        self.assertEqual(second_task["status"], "queued")
        self.assertEqual(first_task["items"][0]["status"], "queued")
        self.assertEqual(second_task["items"][0]["status"], "queued")

    def test_worker_marks_task_processing_before_extraction(self):
        redis = FakeRedis()
        url = "https://example.com/article"

        def assert_processing(_url):
            self.assertEqual(redis.hgetall(task_service._task_key(task_id))["status"], "processing")
            self.assertEqual(redis.hgetall(task_service._item_key(task_id, 1))["status"], "processing")
            raise RuntimeError("stop after state assertion")

        with (
            patch.object(task_service, "require_background_config"),
            patch.object(task_service, "_redis", return_value=redis),
            patch.object(task_service, "_publish_items"),
            patch.object(task_service, "_publish_retry"),
            patch.object(task_service, "extract_content", side_effect=assert_processing),
        ):
            task_id = task_service.create_task([url], "https://app.example/internal/process")
            task_service.process_batch(
                {"task_id": task_id, "items": [{"index": 1, "url": url}], "attempt": 1},
                "https://app.example/internal/process",
            )

    def test_publish_groups_urls_into_batches(self):
        qstash = SimpleNamespace(message=SimpleNamespace(batch_json=lambda messages: messages))
        with patch.object(task_service, "_qstash", return_value=qstash):
            messages = []
            qstash.message.batch_json = lambda value: messages.extend(value)
            task_service._publish_items(
                "task-1",
                [f"https://example.com/{index}" for index in range(12)],
                "https://app.example/internal/process",
            )
        self.assertEqual([len(message["body"]["items"]) for message in messages], [5, 5, 2])

    def test_two_tasks_are_isolated_and_finalize_independently(self):
        redis = FakeRedis()
        blob = FakeBlob()
        urls_a = ["https://example.com/a", "https://example.com/b"]
        urls_b = ["https://example.com/c"]

        def fake_extract(url):
            return f"**title**: {url}\n", 3

        with (
            patch.object(task_service, "require_background_config"),
            patch.object(task_service, "_redis", return_value=redis),
            patch.object(task_service, "_blob", return_value=blob),
            patch.object(task_service, "_publish_items"),
            patch.object(task_service, "_publish_retry"),
            patch.object(task_service, "extract_content", side_effect=fake_extract),
            patch.object(task_service, "markdown_to_docx", side_effect=lambda text: text.encode()),
            patch.object(task_service, "_download_blob", side_effect=lambda url: blob.get(url).content),
        ):
            task_a = task_service.create_task(urls_a, "https://app.example/internal/process")
            task_b = task_service.create_task(urls_b, "https://app.example/internal/process")

            task_service.process_batch(
                {"task_id": task_a, "items": [{"index": 1, "url": urls_a[0]}], "attempt": 1},
                "https://app.example/internal/process",
            )
            task_service.process_batch(
                {"task_id": task_b, "items": [{"index": 1, "url": urls_b[0]}], "attempt": 1},
                "https://app.example/internal/process",
            )

            state_a = task_service.get_task(task_a)
            state_b = task_service.get_task(task_b)
            self.assertEqual(state_a["status"], "processing")
            self.assertEqual(state_a["success"], 1)
            self.assertEqual(state_b["status"], "completed")
            self.assertEqual(state_b["success"], 1)

            task_service.process_batch(
                {"task_id": task_a, "items": [{"index": 2, "url": urls_a[1]}], "attempt": 1},
                "https://app.example/internal/process",
            )
            state_a = task_service.get_task(task_a)
            self.assertEqual(state_a["status"], "completed")
            self.assertIn(task_a, state_a["zip_url"])
            self.assertNotIn(task_b, state_a["zip_url"])

            zip_path = f"blog-tasks/{task_a}/blog-documents.zip"
            with zipfile.ZipFile(io.BytesIO(blob.files[zip_path])) as archive:
                self.assertEqual(len(archive.namelist()), 2)
                self.assertTrue(all(name.endswith(".docx") for name in archive.namelist()))
            self.assertTrue(all(path.endswith("/blog-documents.zip") for path in blob.files))
            self.assertEqual(len(blob.files), 2)

    def test_final_failure_is_recorded_in_zip(self):
        redis = FakeRedis()
        blob = FakeBlob()
        url = "https://example.com/fails"

        with (
            patch.object(task_service, "require_background_config"),
            patch.object(task_service, "_redis", return_value=redis),
            patch.object(task_service, "_blob", return_value=blob),
            patch.object(task_service, "_publish_items"),
            patch.object(task_service, "_publish_retry"),
            patch.object(task_service, "extract_content", side_effect=RuntimeError("boom")),
        ):
            task_id = task_service.create_task([url], "https://app.example/internal/process")
            task_service.process_batch(
                {"task_id": task_id, "items": [{"index": 1, "url": url}], "attempt": 3},
                "https://app.example/internal/process",
            )
            state = task_service.get_task(task_id)
            self.assertEqual(state["status"], "completed_with_errors")
            self.assertEqual(state["failed"], 1)

            zip_path = f"blog-tasks/{task_id}/blog-documents.zip"
            with zipfile.ZipFile(io.BytesIO(blob.files[zip_path])) as archive:
                self.assertEqual(archive.namelist(), ["失败记录.csv"])

    def test_batch_only_retries_failed_items(self):
        redis = FakeRedis()
        blob = FakeBlob()
        urls = ["https://example.com/good", "https://example.com/bad"]

        def fake_extract(url):
            if url.endswith("/bad"):
                raise RuntimeError("boom")
            return "content", 1

        with (
            patch.object(task_service, "require_background_config"),
            patch.object(task_service, "_redis", return_value=redis),
            patch.object(task_service, "_blob", return_value=blob),
            patch.object(task_service, "_publish_items"),
            patch.object(task_service, "_publish_retry") as retry,
            patch.object(task_service, "extract_content", side_effect=fake_extract),
            patch.object(task_service, "markdown_to_docx", return_value=b"docx"),
        ):
            task_id = task_service.create_task(urls, "https://app.example/internal/process")
            task_service.process_batch(
                {
                    "task_id": task_id,
                    "items": [{"index": 1, "url": urls[0]}, {"index": 2, "url": urls[1]}],
                    "attempt": 1,
                },
                "https://app.example/internal/process",
            )
            success = task_service.get_task(task_id)["success"]
        retry.assert_called_once_with(
            task_id,
            [{"index": 2, "url": urls[1]}],
            1,
            "https://app.example/internal/process",
        )
        self.assertEqual(success, 1)

    def test_cleanup_deletes_only_expired_blobs(self):
        blob = FakeBlob()
        old = SimpleNamespace(
            url="https://blob.example/blog-tasks/old.zip",
            uploaded_at=datetime.now(timezone.utc) - timedelta(days=8),
        )
        recent = SimpleNamespace(
            url="https://blob.example/blog-tasks/recent.zip",
            uploaded_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        blob.files = {"blog-tasks/old.zip": b"old", "blog-tasks/recent.zip": b"recent"}
        blob.iter_objects = lambda **kwargs: iter([old, recent])
        with patch.object(task_service, "_blob", return_value=blob):
            deleted = task_service.cleanup_old_blobs()
        self.assertEqual(deleted, 1)
        self.assertNotIn("blog-tasks/old.zip", blob.files)
        self.assertIn("blog-tasks/recent.zip", blob.files)

    def test_public_blob_download_uses_direct_url(self):
        response = SimpleNamespace(content=b"docx", raise_for_status=lambda: None)
        with patch.object(task_service.requests, "get", return_value=response) as get:
            self.assertEqual(task_service._download_blob("https://blob.example/file.docx"), b"docx")
        get.assert_called_once_with("https://blob.example/file.docx", timeout=30)


if __name__ == "__main__":
    unittest.main()
