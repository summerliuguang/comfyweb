# ComfyWeb — ComfyUI 简易生成站

手机端优先的局域网图片生成网站：通过 API 调用另一台主机上的 ComfyUI，
把 ComfyUI 工作流变成简洁的生成表单（提示词 / 模型 / LoRA / 采样参数 / 数量），
带画廊，免构建、无 CDN，手机 + 桌面自适应。

## 使用

1. **设置**：首次访问进入「设置」页，填 ComfyUI 地址（如 `http://192.168.x.x:8188`），点「保存并测试连接」。
2. **导入工作流**：两种方式（「工作流 → 导入模板」）：
   - **从 ComfyUI 拉取**：直接列出 ComfyUI 界面里「保存」过的工作流，选一个自动转换成可运行格式后进入参数确认。含子图/bypass 节点或冷门插件时会转换失败，此时用下面的粘贴方式。
   - **粘贴 JSON**：在 ComfyUI 菜单「工作流 → 导出(API)」，把 JSON 粘贴进来。
   
   导入时自动识别可调参数（正/负提示词、模型、LoRA、KSampler 参数、宽高等），
   可勾选显示、改标签、设默认值，保存为模板。在 ComfyUI 里给节点起的标题会自动变成表单标签。
3. **生成**：「生成」页选模板 → 填表单 → 生成数量（工作流含 EmptyLatentImage 时走 batch_size，否则串行多次提交）→ 生成，实时显示采样进度。
4. **画廊**：本站生成记录（存 SQLite，图片经后端代理实时读取 ComfyUI 的输出）。

## 架构

- Flask 3.1.3 多页应用（`app.py`），所有 ComfyUI 调用经本站后端代理（ComfyUI 默认无 CORS，浏览器无法直连）。
- `comfy_client.py`：HTTP API 封装 + 常驻 WebSocket 监听线程（progress_state / execution_* 事件，兼容旧版），断线自动重连；WS 不可用时轮询 `/history` 对账兜底。
- `workflow.py`：API 格式工作流解析（参数自动识别）与提交时值注入。
- `db.py`：SQLite（设置 / 模板元数据 / 任务 / 图片记录），模板 JSON 存 `data/workflows/`。
- 前端：Bulma 0.9.4 本地 vendor + hub-nav 同款皮肤（`static/css/skin.css`），原生 JS，零构建。

## 部署（本机）

- systemd 服务：`deploy/comfyweb.service`（绑 `127.0.0.1:5012`）
- nginx：`deploy/nginx-comfyweb.conf`（对外 29xxx 端口自签证书，http 301 跳 https；可选 basic auth，凭据放 `.env`，htpasswd 路径见配置）
- 导航页注册：按自己导航页的格式把端口加进服务列表

## 已知取舍

- 图片存在 ComfyUI 主机，画廊经代理流式读取；ComfyUI 侧删除输出文件或更换服务器地址后，旧记录图片会显示"图片缺失"占位。
- 不做 PWA；未使用 ComfyUI 新版 /api/jobs（经典 /prompt API 稳定够用）。
