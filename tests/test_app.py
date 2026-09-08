import unittest
import os
from unittest.mock import patch

import app as app_module


class AppTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_index_and_input_validation(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("自动去空行、去重并校验".encode("utf-8"), page.data)
        self.assertIn(b"parseUrls", page.data)
        self.assertIn("本浏览器最近任务".encode("utf-8"), page.data)
        self.assertIn(b"blog-downloader-recent-tasks", page.data)
        self.assertEqual(self.client.post("/tasks", data={"urls": "invalid"}).status_code, 400)
        urls = "\n".join(["https://example.com"] * 101)
        self.assertEqual(self.client.post("/tasks", data={"urls": urls}).status_code, 400)

    def test_task_page_and_status_api(self):
        task = {
            "id": "task-1",
            "status": "processing",
            "total": 2,
            "success": 1,
            "failed": 0,
            "processed": 1,
            "progress": 50,
            "items": [{"index": 1, "url": "https://example.com/a", "status": "completed"}],
        }
        with patch.object(app_module, "get_task", return_value=task):
            page = self.client.get("/tasks/task-1")
            status = self.client.get("/api/tasks/task-1")
        self.assertEqual(page.status_code, 200)
        self.assertIn("整体进度".encode("utf-8"), page.data)
        self.assertIn(b"visibilitychange", page.data)
        self.assertIn(b"15000", page.data)
        self.assertIn("复制任务链接".encode("utf-8"), page.data)
        self.assertIn("搜索 URL".encode("utf-8"), page.data)
        self.assertIn("处理完成后可下载".encode("utf-8"), page.data)
        self.assertIn(b"saveRecent", page.data)
        self.assertIn("重新打包".encode("utf-8"), page.data)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json["progress"], 50)

    def test_cleanup_requires_cron_secret(self):
        with patch.dict(os.environ, {"CRON_SECRET": "secret"}):
            self.assertEqual(self.client.get("/internal/cleanup").status_code, 401)
            with patch.object(app_module, "cleanup_old_blobs", return_value=4):
                response = self.client.get(
                    "/internal/cleanup", headers={"Authorization": "Bearer secret"}
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["deleted"], 4)

    def test_retry_finalize_route(self):
        with (
            patch.object(app_module, "get_task", return_value={"status": "finalize_error"}),
            patch.object(app_module, "maybe_finalize_task") as finalize,
        ):
            response = self.client.post("/tasks/task-1/retry-finalize")
        self.assertEqual(response.status_code, 302)
        finalize.assert_called_once_with("task-1")


if __name__ == "__main__":
    unittest.main()
