# 部署与环境变量

## 环境变量（.env）

```ini
SECRET_KEY=change-me
# 敏感字段加密专用密钥（可选，留空则用 SECRET_KEY 派生；写入数据后不可变更）
ENCRYPTION_KEY=
DEBUG=false
# 逗号分隔的允许 Host（* = 任意）
ALLOWED_HOSTS=*

# 数据目录 / DB
DATA_DIR=./data
DATABASE_PATH=./data/db.sqlite3

# 管理后台账号 / token
# ⚠ 下面三个值是**占位符，不是可用配置**：DEBUG=false 时若沿用它们，
# 启动期凭据门禁会直接抛 RuntimeError（settings.py 的默认凭据检查）。
# 照抄本文档部署会起不来——必须换成自己的强口令与随机 token。
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<强口令>
ADMIN_TOKEN=<随机长串>

# 上游
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1

# 默认配额（可在管理后台"设置"页覆盖，免重启）
# DEFAULT_NVIDIA_RPM=0 表示默认不限流；设正数才启用每 Key 的 RPM 闸门
DEFAULT_NVIDIA_RPM=0
# 单请求最大并行线路数。默认 8：每条线路都是一个真实的上游并发流，
# agent 客户端的工具调用是原子具现的（N 个子代理同一毫秒派发），
# 大倍数会把上游打到掐流。注意按渠道的 SystemSetting 覆盖值优先于本默认。
MAX_ROUTES_PER_REQUEST=8
UPSTREAM_CONNECT_TIMEOUT=10
# 0 = 不限制（慢模型写大文件可以跑数分钟，固定值会误杀）。
# 僵尸请求的兜底是运行时参数 upstream_total_timeout（默认 3600 秒，后台可调）。
UPSTREAM_READ_TIMEOUT=0
PROXY_TIMEOUT=10
MAX_CONCURRENT_REQUESTS=100

LOG_LEVEL=INFO

# 前端直连后端地址
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
```

生产务必改 `SECRET_KEY`、`ADMIN_PASSWORD`、`ADMIN_TOKEN`，并建议显式配置 `ENCRYPTION_KEY`（否则入库密钥由 `SECRET_KEY` 派生，两者都必须唯一且保密）。

## 本地开发

```bash
cd backend
pip install -r requirements.txt
python manage.py runserver 0.0.0.0:8000     # 启动时自动 migrate，无需手动建表

cd ../frontend
npm install
npm run dev                                  # http://localhost:3000
```

测试：

```bash
cd backend && python -m pytest tests
```

## Docker 一体化

```bash
docker compose up -d
```

- `backend` 容器内运行 Django，SQLite 落到挂载的 `./data`；**前端由同一个进程同源托管**
  （Next.js `output:"export"` 的静态产物在构建期复制进镜像，`api/frontend_views.py` 负责服务）

根 `Dockerfile` 是 all-in-one 两阶段构建：Node 22 阶段 build 前端 → Python 阶段运行后端。
（历史上这里写过"frontend 容器 `npm run start`"——该服务已删除，且 `output:"export"`
下 `npm run start` 根本不工作。）

## 生产注意点

- **前置代理**（nginx/caddy）需放行 SSE：`proxy_buffering off`；读超时要 ≥ 运行时参数
  `stream_max_duration`（默认 3600 秒）与 `upstream_total_timeout`（默认 3600 秒），
  否则代理会先于网关掐断长流
- 单实例 SQLite 适合中小流量；要承载大规模并发请迁移 PostgreSQL + Redis，并把 `MAX_CONCURRENT_REQUESTS` 提升
- 定期备份 `./data/db.sqlite3`
- 不要让 `/api/admin/*` 直接裸露公网，绑 Basic Auth 或放到内网域名
