# Blog 内容下载器

抓取 Blog 正文并生成 Word。Web 版本采用后台任务架构，支持多个用户同时提交大批 URL。

## 后台任务架构

- Flask/Vercel：提交任务、展示进度、运行批处理 worker。
- Upstash QStash：持久化任务队列、调度批处理 worker、限制全局并发。
- Upstash Redis：保存任务与每条 URL 的状态，状态保留 7 天。
- Vercel Blob：临时保存单篇 Word，并保存最终 ZIP，避免 Vercel Function 4.5 MB 响应限制。

Web 版本最终 ZIP 只包含 Word 文件；若部分 URL 最终失败，还会包含 `失败记录.csv`。

单次最多提交 100 个 URL。默认每个 worker 顺序处理 5 条 URL，所有任务共享最多
3 个并发 worker。因此 A 用户的 50 条和 B 用户的 30 条可以同时排队处理，不会在
一个请求中阻塞。50 条 URL 通常只需要 10 次 worker 调用，而不是 50 次。

任务页面在前一分钟每 5 秒刷新一次，之后每 15 秒刷新一次；浏览器标签页不可见时
暂停刷新，以减少无意义的 Function 调用。

最终 ZIP 上传成功后会立即删除单篇 Word 中间文件，只保留最终 ZIP。Vercel Cron
每天再删除 7 天前的最终 ZIP，减少 Blob 存储费用。

## 部署到 Vercel

1. 将目录推送到 GitHub，并在 Vercel 导入仓库。
2. 在 Vercel 项目 `Storage` 中创建 **Public Blob**。
3. 在 [Upstash Console](https://console.upstash.com/) 创建 Redis 数据库和 QStash。
4. 按照 `.env.example` 在 Vercel 项目中配置环境变量。
5. 将 `APP_BASE_URL` 设置为正式部署域名，例如 `https://blog-downloader.example.com`。
6. 重新部署项目。

需要的环境变量：

```text
UPSTASH_REDIS_REST_URL
UPSTASH_REDIS_REST_TOKEN
QSTASH_URL
QSTASH_TOKEN
QSTASH_CURRENT_SIGNING_KEY
QSTASH_NEXT_SIGNING_KEY
BLOB_READ_WRITE_TOKEN
APP_BASE_URL
BATCH_SIZE=5
WORKER_PARALLELISM=3
CRON_SECRET
```

`QSTASH_URL` 必须与 QStash Token 所属区域一致：

```text
美国区：https://qstash-us-east-1.upstash.io
欧洲区：https://qstash-eu-central-1.upstash.io
```

当前任务状态链接依靠随机任务 ID 隔离；Public Blob 下载链接拿到后可以直接访问。
Redis 任务状态保留 7 天。Vercel Cron 每天 UTC 03:00 调用清理接口，删除 7 天前的
任务 Blob；必须为 `CRON_SECRET` 设置一个随机长字符串。

当前项目按无登录内部工具部署，不要启用 Vercel Deployment Protection，否则 QStash
无法调用后台 worker。

## 本地网页

安装依赖并将 `.env.example` 复制为 `.env`，填入真实外部服务配置：

```bash
pip install -r requirements.txt
flask --app app run --debug
```

本地 worker 必须能被 QStash 从公网访问，因此完整后台流程需要公网隧道，并将
`APP_BASE_URL` 设置为该公网地址。仅测试页面和状态接口时不需要隧道。

## 本地命令行

原有命令行模式仍可使用。创建 `urls.txt`，每行放一个 URL，然后执行：

```bash
pip install -r requirements.txt
python blog-script-new.py
```

文件输出到 `output_docs/`。

## 运行测试

```bash
python -m unittest discover -s tests -v
```
