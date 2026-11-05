# AgentWriter

界面、执行进度和生成的博客均使用简体中文。即使主题以英文输入，文章标题、
大纲、正文、代码注释、引用标签和图注也会使用中文；产品名、API、URL 和代码
标识符保留原文。目标字数按中文字符计（不含代码）。
配图提示词使用英文帮助 FLUX 理解画面，图片说明和图注使用中文。

界面脚本与 Markdown 渲染依赖随项目一起部署，使用系统字体，页面初始化不需要访问第三方 CDN。

正文生成和配图均使用 Cloudflare Workers AI，应用与 `notebooks/` 共用
`cloudflare_models.py`：

- 文本：`@cf/meta/llama-3.3-70b-instruct-fp8-fast`，支持规划、资料整理和配图计划的 JSON Schema 输出。
- 配图：`@cf/black-forest-labs/flux-1-schnell`，4 步生成，保存为 JPEG。

在项目目录的 `.env` 中配置：

```dotenv
CLOUDFLARE_API_KEY=your_workers_ai_api_token
CLOUDFLARE_BASEURL=https://api.cloudflare.com/client/v4/accounts/YOUR_ACCOUNT_ID/ai/v1
TAVILY_API_KEY=your_tavily_key
DATABASE_URL=your_postgresql_connection_url
```

`CLOUDFLARE_API_KEY` 需要是有 Workers AI 调用权限的 API Token。
配图接口会从同一个 Base URL 推导 `/ai/run/` 地址，无需单独配置。
`.env` 按项目文件路径加载，从 IDE 或 Notebook 启动也能读取。
联网检索继续使用 Tavily，工作流检查点继续使用 PostgreSQL。

```powershell
python -m pip install -r requirements.txt
python app.py
```

打开 <http://127.0.0.1:8000>。

## Docker 服务器部署

服务器需要安装 Docker Engine 和 Docker Compose 插件。在服务器上放置项目目录，
进入 `AgentWriter`。首次配置时执行以下命令；已有 `.env` 时直接使用现有文件：

```bash
cp .env.example .env
```

编辑 `.env`，填写 Cloudflare、Tavily 和 PostgreSQL 的真实配置，再启动：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=100
```

浏览器打开 `http://服务器IP:8000`。需要其他端口时，在 `.env` 中设置
`AGENTWRITER_PORT=8080`，重新执行 `docker compose up -d` 后访问 8080 端口。
确保服务器防火墙或云安全组允许访问设置的端口。

容器通过 Uvicorn 监听 `0.0.0.0:8000`，关闭开发模式的自动重载。
运行时读取 `.env`，构建镜像时排除密钥、本地虚拟环境和已有文章。
现有 PostgreSQL 数据库继续保存工作流检查点，Compose 不会创建或替换数据库。
`DATABASE_URL` 必须能从容器访问；容器内的 `localhost` 指向容器本身。
外部数据库默认使用 SSL；若自行连接未启用 SSL 的数据库，需要显式配置 `sslmode=disable`。

文章保存在 `outputs/`，图片保存在 `images/`，分别挂载到 Docker 数据卷
`agentwriter_outputs` 和 `agentwriter_images`。普通重启、重新构建和
`docker compose down` 会保留数据；`docker compose down -v` 会删除这些数据卷。
本机已有的文章与图片不会自动复制到服务器的数据卷。

常用操作：

```bash
# 更新代码后重新构建并启动。
docker compose up -d --build

# 重启服务。
docker compose restart

# 停止服务，保留数据卷。
docker compose down

# 检查接口。
curl http://127.0.0.1:8000/api/health
```

健康检查验证 HTTP 服务是否可访问，数据库需要在启动时连接成功。
镜像使用非 root 用户运行；部署进程为单个 Uvicorn worker，章节写作仍由 LangGraph 并行执行。
若通过 Nginx 反向代理，需为 `/api/run` 关闭响应缓冲，并为长时间生成请求
设置足够的读取超时，例如 `proxy_buffering off;` 和 `proxy_read_timeout 600s;`。
当前应用没有登录鉴权，建议通过带身份认证的反向代理或受限网络提供访问。

配置参考：[Docker Compose 环境变量](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/)、
[Dockerfile 健康检查](https://docs.docker.com/reference/dockerfile/#healthcheck)。

这两个模型可使用 Workers AI 的每日免费额度，合计每天 10,000 Neurons，
北京时间每天 08:00 重置。免费账户用完后请求会失败；Workers Paid 账户
超出额度会计费。模型并非无限免费，代码不会自动切换到其他提供商。
配图失败时，文章保留错误说明及原始配图提示词。

验证本地接口适配（不消耗模型额度）：

```powershell
python -m unittest discover -s tests -v
```

浏览器回归检查可模拟 CDN 一直不响应，验证“开始写作”、进度事件和文章预览仍正常工作
（本机需安装 Chrome）：

```bash
python -m pip install playwright
python tests/browser_startup.py
```

接口及额度依据：
[OpenAI 兼容文本接口](https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/)、
[JSON Mode](https://developers.cloudflare.com/workers-ai/features/json-mode/)、
[FLUX.1 schnell](https://developers.cloudflare.com/workers-ai/models/flux-1-schnell/)、
[免费额度与计费](https://developers.cloudflare.com/workers-ai/platform/pricing/)。
