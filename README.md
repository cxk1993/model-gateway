# AI 模型网关 | AI Model Gateway

> **聚合你自己已有的多个 LLM 渠道**为一个 OpenAI 兼容入口：多 Key 轮换、无感切换、熔断容灾、路由组调度。

`openai兼容` `LLM聚合` `API网关` `多Key轮换` `流式聚合` `熔断容灾` `无感切换` `路由组` `Token统计` `缓存命中` `Docker`

---

## 📖 这是什么

一个**自托管的 LLM 请求转发网关**。它把**你自己已经拥有的**多个上游渠道（NVIDIA、商汤 SenseNova、魔搭 ModelScope、以及任何 OpenAI 兼容接口）聚合成一个统一的入口。

你在 ChatBox、NextChat、SillyTavern、Dify 等任意 OpenAI 兼容客户端里填**本网关**的地址和密钥，网关负责：

- 在**你配置的**多个渠道/Key 之间智能轮询与故障切换
- 实时监控各渠道模型健康状态、可用率、延迟
- 按路由组调度与降级
- 统计 Token 消耗（含缓存命中）与完整调用日志

> ⚠️ **本程序不提供任何模型额度或 API Key，也不代理任何厂商服务。**
> 你需要自行向各上游厂商注册并获取 Key，再把它们配进本网关。网关只做「转发 + 调度 + 观测」，不介入你与上游之间的账。

---

## 🚀 部署

### Docker（推荐）

```bash
git clone https://github.com/cxk1993/model-gateway.git
cd model-gateway

docker build -t model-gateway:latest .

docker run -d --name model-gateway \
  -p 127.0.0.1:8000:8000 \
  -v $(pwd)/data:/app/data \
  --restart unless-stopped \
  model-gateway:latest
```

> `-p 127.0.0.1:8000:8000` = 仅宿主机 loopback 可达。对外请用 nginx / Caddy 反代并配 HTTPS。
> 容器内 `GATEWAY_DATA_DIR=/app/data`，配置与调用记录都在该卷。

### Python 直接运行

```bash
git clone https://github.com/cxk1993/model-gateway.git
cd model-gateway
pip install -r requirements.txt

python -m uvicorn app:app --host 0.0.0.0 --port 8000   # 网页模式
python app.py                                          # 桌面窗口模式（需 pip install pywebview）
```

首次启动自动生成 `config.json`。在面板「上游提供商」里添加你的渠道，或直接编辑 `providers.json`（可参考 `config.example.json` / `providers.example.json`）。

---

## ✨ 功能特性

### 🔁 流式/非流式互转（本项目的核心增强）

这是与其他同类网关最大的差异点 —— **双协议双向兜底**：

| 场景 | 行为 |
|---|---|
| 客户端发**非流式**请求，但上游读超时 | 网关**改用流式**向同一上游重发，读取 SSE 并**聚合成标准非流式响应**返回 → 上游生成慢也不再撞总时长超时 |
| 客户端发**非流式**请求（默认路径） | 优先走「内部流式聚合」，按 chunk 重置读计时器，免疫上游长输出超时（慢速上游可达 130s+） |
| 客户端发**流式**请求 | 直接透传 SSE；流式中断时自动切换下一个候选模型**继续输出**，客户端无感 |

聚合器还处理了这些细节：

- **`tool_calls` 增量合并**（按 `index` 归并）→ Agent 工具调用经非流式聚合后不丢
- **非 SSE 降级**：上游忽略 `stream` 直接回 JSON 时也能正确解析
- **`reasoning_content` 原样透传**，不合并进 `content`（保留思考字段供客户端消费）
- 聚合结果仍走完整的失败分类（429 冷却 / 401 拉黑 / 402 退避 / 5xx 换 Key）

> 实测意义：部分上游（尤其长输出深思考模型）总生成时长动辄 120s+，走普通 POST 必被读超时打断；
> 本项目「非流式 → 内部流式聚合」后，同一请求可正常拿到完整结果。

### 🔑 多 Key 轮换与容灾

- **四种 Key 策略**（按 provider 独立配置）：

  | 策略 | 行为 |
  |------|------|
  | `sticky`（默认） | 同一 Key 用到触发 429 或连续失败才切换 |
  | `round_robin` | 顺序轮询 |
  | `random` | 随机选取 |
  | `first` | 只用第一个 Key |

- **坏 Key 自动拉黑**：401/403 立即拉黑该 Key **600 秒**并换下一个重试
- **402 余额不足**：provider 级冷却 **1800 秒**，避免反复撞墙
- **5xx / 连接失败 / 非 JSON 响应**：自动换 Key 重试**同一请求**，客户端无感
- **熔断**：某模型连续失败 **3 次**熔断 **60 秒**，恢复后自动重置计数
- **超时 ≠ Key 故障**：读超时不会误判为 Key 失效（避免把好 Key 全烧一遍）

### 🔀 路由组

- 自定义路由组，客户端 `model` 填组名即命中（如 `1m`、`256k`）
- 默认按**可用率 + 延迟加权**在组内择优
- **严格顺序模式**（`router_strict_order: true`）：按声明顺序返回候选，靠前项只有**真正失败**才顺延 —— 适合「主渠道优先、备用兜底」
- 路由组全失败自动重试一轮；组内成员输出上限可不等

### 🖼️ 识图转发（不是内置模型）

- 网关按 `models_meta.json` 的 `supports_vision` 标记识别**你配置的渠道里哪些模型能识图**
- 开启后，**含图请求**自动转交「识图」路由组；非含图请求正常走文本模型
- 追问自动回退文本模型，不浪费视觉额度
- 识图模型 `max_tokens` 上限较低，网关自动钳制避免上游 400
- 部分小模型忽略 system prompt，网关把中文指令直接注入用户消息末尾

> 前提：识图模型同样来自**你自己的渠道**，需要先在面板里把视觉模型配好并加进「识图」路由组。

### 📊 实时监控

- 所有上游模型健康状态、SLA 可用率、平均/极速/最慢延迟、探针次数
- 状态点：🟢 正常 / 🔴 异常 / ⚪ 尚未巡检
- **健康探测总开关**：默认关闭主动探测（省额度、降低上游风控暴露面），需要时手动触发
- 支持按可用性、提供商、关键词筛选

### 📈 消耗统计

- 按近 24 小时 / 7 天 / 30 天统计请求数、输入/输出/合计 Token
- **缓存命中 Token 单独记录与展示**，兼容多种上游格式：
  - OpenAI / Moonshot 系：`cached_tokens`、`prompt_tokens_details.cached_tokens`
  - DeepSeek：`prompt_cache_hit_tokens`
  - Anthropic 风格：`cache_read_input_tokens`
- 按「渠道 · 模型」明细展示，含命中率 %
- **数据保留天数可配置**（`usage_retention_days`，默认 720 天）
- **调用日志**（`call_log.jsonl`）：按 24 小时 / 7 天 / 30 天或自定义天数查询
- **上游不回 usage 时的兜底估算**：按已发送字符数粗估并标记 `estimated`，避免调用被记成 0 token

> ⚠️ 估算精度约 ±30%（中文约 3~4 字/token，代码与英文差异更大），仅用于「避免记 0」，不适合精确计费对账。

### 🛡️ 面板鉴权

- 双重认证：**Basic**（`admin` / `admin_password`）或 **Bearer**（`local_api_key`）任选
- 未配置 `admin_password` 时自动禁用 Basic（避免默认口令）

### ⚙️ 请求兼容处理

- **按模型钳制 `max_tokens`**：部分上游对超限值直接 400，网关在入口统一钳制
- **清洗历史消息**：移除上游会拒绝的空 `tool_calls: []`
- **内网穿透转发**：`provider_order` 等私有路由参数可按渠道自动注入（不覆盖客户端显式配置）
- **中文回复保障**：可配置强制简体中文（带判重，幂等）
- **响应无缓存**：面板与 API 响应加 `no-cache`，避免客户端拿到旧前端

### 🌙 其他

- 深色 / 浅色模式切换（自动记忆）
- 桌面模式支持系统托盘后台常驻

---

## ⚙️ 配置文件

| 文件 | 作用 | 说明 |
|------|------|------|
| `config.json` | 网关自身配置 | 本地密钥、面板口令、保留天数、探测开关、路由严格顺序 |
| `providers.json` | **你配的**上游渠道 | 名称、Base URL、API Key（可多个）、模型列表、Key 策略 |
| `models_meta.json` | 模型元数据 | 上下文长度、`max_tokens` 上限、识图能力标记 |
| `routers.json` | 路由组 | 自定义路由组（面板配置，自动生成） |
| `history.jsonl` | 巡检历史 | 保留 30 天 |
| `usage.jsonl` | 消耗统计 | 保留天数见 `usage_retention_days`（默认 720 天） |
| `call_log.jsonl` | 调用日志 | 保留 30 天 |

### 主要配置项（`config.json`）

| 键 | 默认 | 说明 |
|----|------|------|
| `local_api_key` | 首次启动自动生成 | 客户端访问 `/v1/*` 的 Bearer 密钥 |
| `admin_password` | 空 | 面板 Basic 口令；**留空则禁用 Basic** |
| `usage_retention_days` | `720` | 消耗统计保留天数（1~7200） |
| `router_strict_order` | `false` | 路由组是否严格按声明顺序 |
| `health_poll_enabled` | `false` | 后台自动健康巡检开关 |
| `vision_assist.enabled` | `false` | 识图转发总开关 |
| `announcement_url` | 空 | 可选：远程公告源；留空用本地 |

---

## 🔐 鉴权

| 接口 | 凭据 |
|------|------|
| `/v1/chat/completions`、`/v1/models` | `Authorization: Bearer <local_api_key>` |
| `/api/*`（管理面板） | `Authorization: Bearer <local_api_key>` 或 Basic `admin:<admin_password>` |

---

## 🐳 反向代理（nginx）

```nginx
server {
    listen 8443 ssl;
    server_name your.domain;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_buffering off;          # 流式必须关闭缓冲，否则客户端要等全部生成完
        proxy_read_timeout 300s;
    }
}
```

> **SSE 流式必须 `proxy_buffering off`**，否则会退化成「等全部生成完才收到」。

---

## 🧪 测试

```bash
pip install pytest
pytest tests/ -v
```

---

## 📄 License

[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)（署名 · 非商业性使用），详见 `LICENSE`。
