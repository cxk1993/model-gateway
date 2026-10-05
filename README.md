# AI 模型网关 · 增强版 | AI Model Gateway

> 把多个上游 LLM 渠道聚合为一个 **OpenAI 兼容入口**，智能轮询、无感切换、熔断容灾。
> 单渠道额度用完或故障自动跳过，多渠道路由组按需调度。

`openai兼容` `LLM聚合` `API网关` `智能轮询` `多Key轮换` `熔断容灾` `无感切换` `路由组` `Token统计` `缓存命中` `Docker` `反向代理`

---

## 📖 项目简介

本程序是一个**模型 API 网关**：把多个上游 LLM 提供商（NVIDIA、商汤 SenseNova、魔搭 ModelScope、任何 OpenAI 兼容渠道……）聚合为统一的 OpenAI 兼容接口。你在 ChatBox、NextChat、SillyTavern、Dify 等任意支持 OpenAI 格式的工具里填入本网关的地址和密钥即可使用：

- 自动在多个上游渠道间**智能轮询**，单渠道挂了无感切换
- 实时**监控**所有模型的健康状态、可用率、延迟
- 自定义**路由组**，按需组合与调度模型
- 统计 **Token 消耗**（含缓存命中）与完整**调用日志**

程序提供 **Docker / 服务器部署** 与 **Python 直接运行** 两种方式。

> 本项目基于上游开源项目增强，新增了多 Key 轮换策略、缓存统计、路由严格顺序、Docker 部署等能力。
> 原版仓库已不可访问，本项目独立维护。

---

## ✨ 功能特性

### 🔑 多 Key 轮换与容灾

- **四种 Key 策略**（按 provider 独立配置）：

  | 策略 | 行为 |
  |------|------|
  | `sticky`（默认） | 同一 Key 用到触发 429 或连续失败才切换 |
  | `round_robin` | 顺序轮询 |
  | `random` | 随机选取 |
  | `first` | 只用第一个 Key |

- **坏 Key 自动拉黑**：401/403 立即拉黑该 Key 600 秒并换下一个重试
- **402 余额不足**：provider 级冷却 1800 秒，避免反复撞墙
- **5xx / 连接失败 / 非 JSON 响应**：自动换 Key 重试同一请求，客户端无感
- **熔断**：某模型连续失败 3 次熔断 60 秒，恢复后自动重置计数
- **流式中断续传**：流式转发中断时自动切换下一个候选继续输出

### 🔀 路由组

- 创建自定义路由组并勾选成员模型，客户端 `model` 字段填组名即命中（如 `1m`、`256k`）
- 默认按**可用率 + 延迟加权**在组内择优
- **严格顺序模式**（`config.json` → `router_strict_order: true`）：按声明顺序返回候选，靠前的只有真正失败才顺延到下一个 —— 适合「主渠道优先、备用兜底」的场景
- 路由组全失败自动重试一轮

### 📊 实时监控

- 展示所有上游模型健康状态、SLA 可用率、平均/极速/最慢延迟、探针次数
- 模型名旁状态点：🟢 正常 / 🔴 异常 / ⚪ 尚未巡检
- **健康探测总开关**：可关闭后台自动巡检（省额度、降低上游风控暴露面）
- 支持按可用性、提供商、关键词筛选

### 📈 消耗统计

- 按近 24 小时 / 7 天 / 30 天统计请求数、输入/输出/合计 Token
- **缓存命中 Token 单独记录与展示**（兼容多种上游格式）
  - OpenAI / Moonshot 系：`cached_tokens`、`prompt_tokens_details.cached_tokens`
  - DeepSeek：`prompt_cache_hit_tokens`
  - Anthropic 风格：`cache_read_input_tokens`
- 按「渠道 · 模型」维度明细展示，含命中率 %
- **数据保留天数可配置**（`usage_retention_days`，默认 720 天）
- **调用日志**（`call_log.jsonl`）：可按近 24 小时 / 7 天 / 30 天或自定义天数查询
- **上游不回 usage 时的兜底估算**：部分上游流式响应偶发不带 usage 块，网关按已发送字符数粗估并标记 `estimated`，避免该次调用被记成 0 token

> ⚠️ 估算值精度约 ±30%（中文约 3~4 字/token，代码与英文差异更大），仅用于「避免记 0」，不适用于精确计费对账。

### 🛡️ 管理面板鉴权

- 双重认证：**Basic**（`admin` / `admin_password`）或 **Bearer**（`local_api_key`）任选其一
- 未配置 `admin_password` 时自动禁用 Basic 认证（避免默认口令）

### ⚙️ 模型管理

- 每个提供商支持：编辑连接信息、勾选启用的模型、删除、配置 Key 策略
- 添加提供商时自动从上游拉取可用模型列表，可只拉免费模型
- **上下文长度**：点击监控表格「上下文窗口」列的数字即可修改，回车保存
- **按模型钳制 `max_tokens`**：部分上游对超限值直接返回 400，网关在入口统一钳制

### 📦 管理输出模型

- 查看本网关对外暴露的所有模型 ID（格式 `提供商名-模型名`），一键复制

### 🖼️ 识图辅助

- 内置多个视觉模型，覆盖 NVIDIA / 魔搭 / 商汤等平台
- 开启后自动识别图片并交给视觉模型回复，追问自动回退文本模型（不浪费额度）

### 🌙 其他

- 深色 / 浅色模式切换（自动记忆），表单控件全适配
- 所有回复强制简体中文
- 质量统计采用滑动窗口，准确反映近期状态
- 支持系统托盘后台常驻（桌面模式）

---

## 🚀 部署方式

### 方式一：Docker（推荐服务器部署）

```bash
git clone https://github.com/cxk1993/model-gateway.git
cd model-gateway

# 构建镜像
docker build -t model-gateway:latest .

# 运行（数据卷挂载，配置与调用记录持久化）
docker run -d --name model-gateway \
  -p 127.0.0.1:8000:8000 \
  -v $(pwd)/data:/app/data \
  --restart unless-stopped \
  model-gateway:latest
```

> `-p 127.0.0.1:8000:8000` 表示仅宿主机 loopback 可达，对外请用 nginx / Caddy 反向代理并配置 HTTPS。
> 容器内 `GATEWAY_DATA_DIR=/app/data`，`providers.json`、`config.json`、调用记录都在该卷内。

### 方式二：Python 直接运行

```bash
git clone https://github.com/cxk1993/model-gateway.git
cd model-gateway

pip install -r requirements.txt

# 纯网页模式（适合服务器）
python -m uvicorn app:app --host 0.0.0.0 --port 8000

# 或桌面窗口模式（需额外 pip install pywebview）
python app.py
```

浏览器访问 `http://<你的地址>:8000`。

### 配置提供商

首次启动会自动生成 `config.json`（含本地密钥）。在管理面板的「上游提供商」里添加你的 API，或直接编辑 `providers.json`。

可参考仓库里的 `config.example.json` 和 `providers.example.json`。

---

## ⚙️ 配置文件说明

| 文件 | 作用 | 说明 |
|------|------|------|
| `config.json` | 网关自身配置 | 本地密钥、面板口令、保留天数、健康探测开关、路由严格顺序等 |
| `providers.json` | 上游提供商 | 渠道名称、Base URL、API Key（可多个）、模型列表、Key 策略 |
| `models_meta.json` | 模型元数据 | 上下文长度、`max_tokens` 上限、能力标记 |
| `routers.json` | 路由组配置 | 自定义路由组（面板里配置，自动生成） |
| `history.jsonl` | 巡检历史 | 自动保留 30 天 |
| `usage.jsonl` | 消耗统计 | 保留天数由 `usage_retention_days` 控制（默认 720 天） |
| `call_log.jsonl` | 调用日志 | 自动保留 30 天 |

### 主要配置项（`config.json`）

| 键 | 默认 | 说明 |
|----|------|------|
| `local_api_key` | 首次启动自动生成 | 客户端访问 `/v1/*` 的 Bearer 密钥 |
| `admin_password` | 空 | 面板 Basic 认证口令；**留空则禁用 Basic**（只用 Bearer） |
| `usage_retention_days` | `720` | 消耗统计保留天数（1~7200） |
| `router_strict_order` | `false` | 路由组是否严格按声明顺序（`true` 时靠前优先，失败才顺延） |
| `health_poll_enabled` | `true` | 后台自动健康巡检开关 |
| `announcement_url` | 空 | 可选：远程公告源（任意 raw 文本链接）；留空则用本地 `announcement.json` |

---

## 🔐 鉴权说明

| 接口 | 凭据 |
|------|------|
| `/v1/chat/completions`、`/v1/models` | `Authorization: Bearer <local_api_key>` |
| `/api/*`（管理面板） | `Authorization: Bearer <local_api_key>` 或 Basic `admin:<admin_password>` |

---

## 🧪 测试

```bash
pip install pytest
pytest tests/ -v
```

---

## 🐳 反向代理示例（nginx）

```nginx
server {
    listen 8443 ssl;
    server_name your.domain;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_buffering off;          # 流式响应必须关闭缓冲
        proxy_read_timeout 300s;
    }
}
```

> 流式（SSE）必须设置 `proxy_buffering off`，否则客户端会等全部生成完才收到内容。

---

## 📄 License

[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)（署名 · 非商业性使用）

本项目基于上游开源项目增强，保留原许可证。详见仓库根目录 `LICENSE`。
