# B站音频提取器（GitHub Pages + Render）

## 功能
- 输入公开可访问的 B 站视频链接，使用 yt-dlp 获取可访问的最佳音频流，并由 FFmpeg 转为高质量 MP3。
- 不使用 cookies，不绕过登录或访问限制；受限视频可能无法解析。
- 免费后端资源有限，首次唤醒可能较慢。默认限制单次时长 3 小时、输出 300 MB、并发 1 个任务。
- 请仅下载你有权保存和使用的内容，并遵守 B 站条款及著作权法律。

## 部署后端到 Render
1. 在 Render 选择 New → Web Service，连接此 GitHub 仓库。
2. Runtime 选择 Docker；Docker Context 填 `./backend`，Dockerfile Path 填 `./backend/Dockerfile`，计划选 Free，地区可选 Singapore。
3. 环境变量添加 `FRONTEND_ORIGIN=https://ancenchan.github.io`（如 GitHub 用户名或 Pages 域名不同，请改为实际网站 origin）。
4. 部署成功后复制服务 URL，例如 `https://bili-audio-api-xxxx.onrender.com`，访问其 `/health` 应返回 `{"status":"ok"}`。

## 配置前端
编辑仓库根目录的 `index.html`，把 `API_BASE` 中的 `https://YOUR-RENDER-SERVICE.onrender.com` 替换成实际 Render 服务 URL（不带末尾斜杠），提交修改。

## 启用 GitHub Pages
1. 仓库 Settings → Pages。
2. Build and deployment 选择 Deploy from a branch。
3. Branch 选择 `main`，目录选择 `/(root)`，保存。
4. 等待发布。网站地址通常是 `https://ancenchan.github.io/bili-audio-downloader/`。

## 注意
这是公开 API，CORS 不是访问授权。不要用于商业化或高流量场景。B 站接口变更、视频访问权限、免费实例资源和请求超时均可能导致失败。
