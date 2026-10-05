import json
import asyncio
import time
import os
import logging
import hashlib
import socket
import threading
import random
try:
    import winreg
except ImportError:
    winreg = None
import subprocess
from logging.handlers import RotatingFileHandler
from pathlib import Path
from contextlib import asynccontextmanager
from collections import deque

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import httpx
import secrets
import webbrowser
import re
import copy

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("gateway")

# ============================================================
# 路径与常量
# ============================================================
import sys

if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys._MEIPASS)
    DATA_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent
    # 开发模式支持环境变量 GATEWAY_DATA_DIR 指向真实数据目录（如 dist/）
    _env_data = os.environ.get("GATEWAY_DATA_DIR", "")
    if _env_data:
        DATA_DIR = Path(_env_data) if Path(_env_data).is_absolute() else Path(__file__).parent / _env_data
    else:
        DATA_DIR = Path(__file__).parent

DATA_FILE = DATA_DIR / "providers.json"
CONFIG_FILE = DATA_DIR / "config.json"
HISTORY_FILE = DATA_DIR / "history.jsonl"
USAGE_FILE = DATA_DIR / "usage.jsonl"
CALL_LOG_FILE = DATA_DIR / "call_log.jsonl"
META_FILE = DATA_DIR / "models_meta.json"
ROUTERS_FILE = DATA_DIR / "routers.json"
ANNOUNCEMENT_FILE = DATA_DIR / "announcement.json"

APP_VERSION = "1.8.0"

MAX_HISTORY_DAYS = 30
MAX_USAGE_DAYS = 720
MAX_CALL_LOG_DAYS = 30
HISTORY_CLEANUP_INTERVAL = 6 * 3600
ONE_MILLION = 1048576
POLL_INTERVAL = 300
CIRCUIT_FAIL_THRESHOLD = 3
CIRCUIT_RECOVERY_SECONDS = 60
QUALITY_WINDOW = 20
POLL_MAX_COUNT = 20
CALL_LOG_MAX = 100


# ============================================================
# 原子写入
# ============================================================
def atomic_write(path: Path, content: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


# ============================================================
# 配置加载
# ============================================================
def load_config():
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        # 补充缺失的默认字段
        data.setdefault("local_api_key", "sk-local-" + secrets.token_hex(16))
        data.setdefault("port", 8000)
        data.setdefault("poll_count", 0)
        data.setdefault("last_daily_poll", "")
        data.setdefault("usage_retention_days", 720)
        return data
    data = {
        "local_api_key": "sk-local-" + secrets.token_hex(16),
        "port": 8000,
        "poll_count": 0,
        "last_daily_poll": "",
        "usage_retention_days": 720,
    }
    atomic_write(CONFIG_FILE, json.dumps(data, indent=2))
    return data


def save_config():
    """将内存中的 app_config 原子写回 config.json。"""
    atomic_write(CONFIG_FILE, json.dumps(app_config, ensure_ascii=False, indent=2))


def load_providers():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return []


def save_providers(data):
    atomic_write(DATA_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def load_meta():
    """三层合并：内置兜底(APP_DIR/models_meta.json) → 外部覆盖(DATA_DIR/models_meta.json)。
    dict 字段深合并，其余字段直接覆盖。"""
    default = {
        "aliases": {},
        "context_limits": {},
        "non_chat_keywords": [],
        "model_descriptions": {},
        "supports_vision": {},
    }
    # 内置版（打包内嵌进 exe，断网保底；非打包时与外部版同路径）
    builtin = APP_DIR / "models_meta.json"
    if builtin.exists():
        try:
            default.update(json.loads(builtin.read_text(encoding="utf-8")))
        except Exception:
            pass
    # 外部版（exe 同目录，用户可覆盖/补充）
    if META_FILE.exists():
        try:
            ext = json.loads(META_FILE.read_text(encoding="utf-8"))
            for k, v in ext.items():
                if isinstance(v, dict) and isinstance(default.get(k), dict):
                    default[k].update(v)
                else:
                    default[k] = v
        except Exception:
            pass
    return default


def load_routers():
    if ROUTERS_FILE.exists():
        try:
            return json.loads(ROUTERS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}

def save_routers():
    atomic_write(ROUTERS_FILE, json.dumps(ROUTERS, indent=2, ensure_ascii=False))


app_config = load_config()
LOCAL_API_KEY = app_config.get("local_api_key")
# 未配置 admin_password 时留空 —— Basic 认证自动禁用，避免默认口令随源码公开
ADMIN_PASSWORD = app_config.get("admin_password", "")


def _check_basic_auth(authorization: str) -> bool:
    """Basic Auth 检查（admin / ADMIN_PASSWORD）"""
    if not authorization or not authorization.startswith("Basic "):
        return False
    if not ADMIN_PASSWORD:
        return False                      # 未配置口令 → 禁用 Basic 认证
    try:
        import base64
        decoded = base64.b64decode(authorization[6:].strip()).decode("utf-8", errors="ignore")
        username, _, password = decoded.partition(":")
        return username == "admin" and password == ADMIN_PASSWORD
    except Exception:
        return False
ROUTERS = load_routers()

meta = load_meta()
MODEL_ALIASES = meta.get("aliases", {})
CONTEXT_LIMITS = meta.get("context_limits", {})
NON_CHAT_KEYWORDS = meta.get("non_chat_keywords", [])
MODEL_DESCRIPTIONS = meta.get("model_descriptions", {})
SUPPORTS_VISION = meta.get("supports_vision", {})


# ============================================================
# 鉴权
# ============================================================
security = HTTPBearer(auto_error=False)


def verify_client(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """客户端调用 /v1/* 的鉴权"""
    if not credentials or credentials.credentials != LOCAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")
    return credentials


def verify_admin(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """管理面板调用 /api/* 的鉴权：接受 Basic(admin/ADMIN_PASSWORD) 或 Bearer(local_api_key)"""
    if credentials and credentials.credentials == LOCAL_API_KEY:
        return credentials
    auth_header = request.headers.get("Authorization", "")
    if _check_basic_auth(auth_header):
        return credentials
    raise HTTPException(status_code=401, detail="Invalid credentials")


# ============================================================
# 断电恢复：检查一键配置的残留备份，自动还原
# ============================================================
def _recover_preset_backup():
    bak_p = DATA_FILE.with_suffix(DATA_FILE.suffix + ".preset_bak")
    bak_r = ROUTERS_FILE.with_suffix(ROUTERS_FILE.suffix + ".preset_bak")
    recovered = False
    if bak_p.exists():
        try:
            atomic_write(DATA_FILE, bak_p.read_text("utf-8"))
            bak_p.unlink()
            recovered = True
            logger.warning("断电恢复：已从备份还原 providers.json")
        except Exception:
            logger.exception("备份还原 providers.json 失败")
    if bak_r.exists():
        try:
            atomic_write(ROUTERS_FILE, bak_r.read_text("utf-8"))
            bak_r.unlink()
            recovered = True
            logger.warning("断电恢复：已从备份还原 routers.json")
        except Exception:
            logger.exception("备份还原 routers.json 失败")
    return recovered

_recover_preset_backup()

# ============================================================
# 全局状态
# ============================================================
providers = load_providers()
# 兼容旧格式：api_key → api_keys
for p in providers:
    if "api_key" in p and "api_keys" not in p:
        p["api_keys"] = [p.pop("api_key")]
    elif "api_key" in p and "api_keys" in p:
        del p["api_key"]
    p.setdefault("api_keys", [])
    p.setdefault("key_strategy", "sticky")
save_providers(providers)

health_status: dict = {}
model_details: dict = {}
model_quality: dict = {}          # key -> {ok, fail, error, latencies: deque}
circuit_breaker: dict = {}        # key -> {fails, open_until}
providers_lock = asyncio.Lock()
history_lock = asyncio.Lock()
usage_lock = asyncio.Lock()
http_client: httpx.AsyncClient | None = None
poll_task = None
last_poll_time: float = 0
last_check_time: float = time.time()
last_history_cleanup: float = 0

# 调用日志（内存队列，最多保留 100 条）
call_log = deque(maxlen=CALL_LOG_MAX)

def _extract_cached_tokens(u: dict) -> int:
    """从上游 usage 里提取「缓存命中」的 prompt token 数（兼容多家格式）。
       Moonshot / OpenAI 系: usage.cached_tokens 或 usage.prompt_tokens_details.cached_tokens
       DeepSeek: usage.prompt_cache_hit_tokens
       Anthropic 风格: usage.cache_read_input_tokens
       字段缺失或非数字一律返回 0。"""
    if not isinstance(u, dict):
        return 0
    c = u.get("cached_tokens")
    if not c:
        d = u.get("prompt_tokens_details")
        if isinstance(d, dict):
            c = d.get("cached_tokens")
    if not c:
        c = u.get("prompt_cache_hit_tokens")
    if not c:
        c = u.get("cache_read_input_tokens")
    try:
        return int(c or 0)
    except (TypeError, ValueError):
        return 0


# ============================================================
# 上游未返回 usage 时的 token 兜底估算
# ============================================================
# 背景：部分上游的【流式】响应偶尔（约 1/4 概率）不回 usage 块。
# 若不兜底，该次调用会被记成 0 token、统计缺失。这里按「字符数 / 4」粗估
# （中英混排经验值，准确度约 ±30%），并在记录里打 estimated 标记，
# 便于识别哪些是估算值。仅在上游 usage 完全缺失时兜底。
TOKEN_ESTIMATE_CHARS_PER_TOKEN = 4


def _estimate_tokens_from_text(text):
    """按字符数粗估 token 数（中英混排取 4 字符 ≈ 1 token）。"""
    if not text:
        return 0
    try:
        return max(1, round(len(str(text)) / TOKEN_ESTIMATE_CHARS_PER_TOKEN))
    except Exception:
        return 0


def _messages_to_text(messages):
    """把 messages 拍平成纯文本，供无 usage 时估算 prompt token。"""
    if not isinstance(messages, list):
        return ""
    parts = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for seg in c:
                if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                    parts.append(seg["text"])
        tc = m.get("tool_calls")
        if isinstance(tc, list):
            for call in tc:
                fn = (call or {}).get("function") or {}
                parts.append(str(fn.get("name") or ""))
                parts.append(str(fn.get("arguments") or ""))
    return "\n".join(parts)


def _extract_out_text(parsed):
    """从响应 dict 取出助手输出文本（content + reasoning + tool_calls），供估算 ct。"""
    try:
        msg = parsed["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    parts = []
    c = msg.get("content")
    if isinstance(c, str):
        parts.append(c)
    rc = msg.get("reasoning_content")
    if isinstance(rc, str):
        parts.append(rc)
    tcs = msg.get("tool_calls")
    if isinstance(tcs, list):
        for call in tcs:
            fn = (call or {}).get("function") or {}
            parts.append(str(fn.get("name") or ""))
            parts.append(str(fn.get("arguments") or ""))
    return "\n".join(parts)


def _usage_or_estimate(usage_obj, req_messages, out_text):
    """返回 (pt, ct, cached, estimated)。
    usage 有效（pt 或 ct > 0）时原样返回；完全缺失时按字符数估算并置 estimated=True。"""
    u = usage_obj or {}
    pt = u.get("prompt_tokens", 0) or 0
    ct = u.get("completion_tokens", 0) or 0
    ch = _extract_cached_tokens(u)
    if pt > 0 or ct > 0:
        return pt, ct, ch, False
    pt = _estimate_tokens_from_text(_messages_to_text(req_messages))
    ct = _estimate_tokens_from_text(out_text)
    return pt, ct, ch, bool(pt or ct)


def _append_call_log_sync(entry: dict):
    """追加调用日志到文件（持久化）"""
    with open(CALL_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def append_call_log(entry: dict):
    entry["ts"] = time.time()
    call_log.append(entry)
    # 同步写文件（单行追加，开销极小）
    try:
        _append_call_log_sync(entry)
    except Exception:
        pass

def _cleanup_call_log_sync():
    """清理超过 MAX_CALL_LOG_DAYS 的调用日志"""
    if not CALL_LOG_FILE.exists():
        return 0
    cutoff = time.time() - MAX_CALL_LOG_DAYS * 86400
    kept = []
    removed = 0
    with open(CALL_LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line.strip())
                if rec.get("ts", 0) >= cutoff:
                    kept.append(line if line.endswith("\n") else line + "\n")
                else:
                    removed += 1
            except Exception:
                pass
    if removed > 0:
        atomic_write(CALL_LOG_FILE, "".join(kept))
    return removed

# Key 选择策略（sticky / round_robin / random / first）
# 参考 OmniRoute / freellmapi 的 key 路由设计，切换逻辑全部在网关内部完成，前端无感知
#   sticky      : 一直用同一个 Key，直到 429 或连续 3 次失败才切到下一个（默认，保缓存降延迟）
#   round_robin : 每次请求顺序轮换下一个 Key
#   random      : 每次请求随机选一个 Key
#   first       : 永远只取第一个 Key（不轮换）
_active_key_index: dict = {}     # provider_name -> 当前活跃 Key 的索引
_key_consecutive_fails: dict = {} # provider_name -> 当前 Key 的连续失败次数
_key_rr_counters: dict = {}      # provider_name -> 轮询计数器
# 坏 Key 黑名单：provider_name -> {key: 解禁时间戳}，认证类失败直接拉黑，select_key 自动跳过
_key_blacklist: dict = {}
KEY_BLACKLIST_SECONDS = 600

VALID_KEY_STRATEGIES = {"sticky", "round_robin", "random", "first"}

def _get_keys(provider):
    return provider.get("api_keys", [])

def _key_banned(name: str, key: str) -> bool:
    until = _key_blacklist.get(name, {}).get(key, 0)
    return time.time() < until

def blacklist_key(provider: dict, key: str):
    """把一个坏 Key 拉黑 KEY_BLACKLIST_SECONDS 秒（select_key 会跳过）"""
    if not key:
        return
    name = provider["name"]
    bl = _key_blacklist.setdefault(name, {})
    # 顺手清掉过期条目，防止 dict 无限增长
    now = time.time()
    for kk in [kk for kk, ts in bl.items() if ts <= now]:
        del bl[kk]
    bl[key] = now + KEY_BLACKLIST_SECONDS
    logger.warning("Key 拉黑 [%s]: %s（%ds 内不再使用）", name, mask_key(key), KEY_BLACKLIST_SECONDS)

def _available_keys(provider: dict) -> list:
    """返回未被拉黑的 Key；若全部拉黑则临时放行（宁可再撞墙也不返回空）"""
    keys = _get_keys(provider)
    if not keys:
        return []
    name = provider["name"]
    ok = [k for k in keys if not _key_banned(name, k)]
    return ok if ok else keys

def inject_provider_order(provider: dict, req_body: dict) -> dict:
    """若 provider 配置了 provider_order，自动注入 provider.order 路由策略。
    仅在请求未显式指定 provider 策略时注入，避免覆盖客户端主动传的配置。"""
    order = provider.get("provider_order")
    if order:
        provider_cfg = req_body.get("provider")
        if not isinstance(provider_cfg, dict) or "order" not in provider_cfg:
            req_body.setdefault("provider", {})
            req_body["provider"]["order"] = order
    return req_body

def _get_strategy(provider: dict) -> str:
    s = provider.get("key_strategy", "sticky")
    return s if s in VALID_KEY_STRATEGIES else "sticky"

def select_key(provider: dict) -> str:
    """按 provider 配置的策略返回下一个 API Key"""
    keys = _get_keys(provider)
    if not keys:
        return ""
    name = provider["name"]
    strategy = _get_strategy(provider)

    if strategy == "round_robin":
        pool = _available_keys(provider)
        idx = _key_rr_counters.get(name, 0) % len(pool)
        _key_rr_counters[name] = idx + 1
        return pool[idx]

    if strategy == "random":
        return random.choice(_available_keys(provider))

    if strategy == "first":
        return keys[0]

    # sticky（默认）：返回当前粘性 Key；黑名单里的坏 Key 自动跳过并前移指针
    # 修复（2026-09-23）：旧写法 `idx = idx + 1` 不归零，且 for 循环次数用尽后
    # 会直接返回黑名单里的坏 key（凌晨 02:30 SenseNova 12 连击即此 bug）。
    # 新写法：从指针处开始，最多绕一圈找到第一个未拉黑的 key；若全部拉黑，
    # 返回指针当前位置（兜底放行），但绝不返回一个"本可避开"的坏 key。
    n = len(keys)
    idx = _active_key_index.get(name, 0) % n
    chosen = None
    for step in range(n):
        cand = (idx + step) % n
        if not _key_banned(name, keys[cand]):
            chosen = cand
            break
    if chosen is None:
        # 全部拉黑：临时放行指针当前位置（宁可再撞墙也不返回空）
        chosen = idx
    _active_key_index[name] = chosen
    return keys[chosen]

def on_key_success(provider: dict):
    """调用成功，重置当前 Key 的失败计数（仅 sticky 用）"""
    name = provider["name"]
    _key_consecutive_fails[name] = 0

def on_key_failure(provider: dict, status_code: int | None = None, bad_key: str | None = None):
    """调用失败，累计失败次数。
    401/403 认证失败 → 立即切换 + 拉黑坏 Key；429 → 立即切换；其他连续 3 次才切（仅 sticky 用）"""
    name = provider["name"]
    keys = _get_keys(provider)
    if not keys:
        return
    # 认证失败与坏 Key 拉黑对所有策略生效（round_robin/random 一样会反复选中坏 Key）
    if status_code in AUTH_ERROR_CODES:
        if bad_key:
            blacklist_key(provider, bad_key)
    # 非 sticky 策略不需要粘性指针切换
    if _get_strategy(provider) != "sticky":
        return

    fails = _key_consecutive_fails.get(name, 0) + 1
    _key_consecutive_fails[name] = fails

    is_429 = status_code == 429
    is_auth = status_code in AUTH_ERROR_CODES
    should_switch = is_429 or is_auth or fails >= 3

    if should_switch:
        # 修复（2026-09-23）：旧写法 next_idx 从 current_idx 起步、循环内才 +1，
        # 而坏 key 已在上面 blacklist_key 拉黑——第一轮 +1 常恰好跳过坏 key 后
        # 绕回原地，导致日志出现 "sk-XX → sk-XX" 自切换、指针空转。
        # 新写法：明确从 current_idx 的下一格开始，绕一圈找第一个未拉黑的 key；
        # 找不到（全黑）时退到 current_idx+1，保证指针一定前进。
        n = len(keys)
        current_idx = _active_key_index.get(name, 0) % n
        next_idx = None
        for step in range(1, n + 1):
            cand = (current_idx + step) % n
            if not _key_banned(name, keys[cand]):
                next_idx = cand
                break
        if next_idx is None:
            next_idx = (current_idx + 1) % n
        _active_key_index[name] = next_idx
        _key_consecutive_fails[name] = 0
        if is_auth:
            reason = f"认证失败 {status_code}，坏 Key 已拉黑 {KEY_BLACKLIST_SECONDS}s"
        elif is_429:
            reason = "429 限流"
        else:
            reason = f"连续 {fails} 次失败"
        logger.info("Key 切换 [%s]: %s → %s（%s）", name, mask_key(keys[current_idx]), mask_key(keys[next_idx]), reason)
        if current_idx == next_idx:
            logger.warning("Key 切换 [%s]: 指针未能前进（可能全部 Key 已拉黑），保持 idx=%d", name, current_idx)

# ============================================================
# 失败分类：区分请求问题 / Key 问题 / 限流 / 服务问题
# ============================================================
# 请求体本身有问题 → 换 Key 无用，不切 Key、不熔断，直接透传错误
REQUEST_ERROR_CODES = {400, 404, 405, 409, 413, 415, 422}
# Key 认证失败 → 下一个 Key 可能有效，拉黑坏 Key + 换 Key 重试
AUTH_ERROR_CODES = {401, 403}
# 账号余额/权限问题 → 换 Key 无用（账号级），冷却整个 provider，直接路由下一个候选
BILLING_ERROR_CODES = {402}

def classify_failure(status_code: int | None) -> str:
    """'request' | 'auth' | 'quota' | 'billing' | 'server' | 'connect'"""
    if status_code is None:
        return "connect"
    if status_code in REQUEST_ERROR_CODES:
        return "request"
    if status_code in AUTH_ERROR_CODES:
        return "auth"
    if status_code == 429:
        return "quota"
    if status_code in BILLING_ERROR_CODES:
        return "billing"
    return "server"

# 429 很可能是 IP 级限流，换 Key 也无效 → provider 级冷却，避免空转切完所有 Key
_provider_429_cooldown: dict = {}
PROVIDER_429_COOLDOWN_SECONDS = 180
PROVIDER_BILLING_COOLDOWN_SECONDS = 1800  # 402 余额不足：冷却 30 分钟，避免反复撞墙

def mark_provider_429(provider: dict, seconds: int = PROVIDER_429_COOLDOWN_SECONDS):
    _provider_429_cooldown[provider["name"]] = time.time() + seconds

def is_provider_429_cooling(provider: dict) -> bool:
    return time.time() < _provider_429_cooldown.get(provider["name"], 0)

# ============================================================
# 消息清洗：适配上游严格校验（SenseNova 等拒绝空 tool_calls 数组）
# ============================================================
def sanitize_messages(messages: list) -> list:
    """移除上游会 400 的字段：
    - tool_calls 为空数组（SenseNova: 'messages[N].tool_calls': empty array）
    返回新列表，不改原 body。
    """
    if not isinstance(messages, list):
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("tool_calls") == []:
            m = {k: v for k, v in m.items() if k != "tool_calls"}
        out.append(m)
    return out

def clamp_max_tokens(body: dict, model: str) -> dict:
    """按 models_meta.json 的 max_output_limits 钳制 max_tokens。

    背景：部分上游的 mimo-v2.6-flash 对 max_tokens > 131072
    直接返回 HTTP 400 LITELLM_ERROR "Param Incorrect"。1m 等路由会带百万级
    max_tokens，原样转给上游必然 400（表现为前端「请求参数错误」）。
    这里在入口统一钳制，流式/非流式均覆盖。
    仅在超限时改动，不覆盖客户端已传的合理值；无配置的模型不动。
    """
    if not isinstance(body, dict):
        return body
    limits = meta.get("max_output_limits", {}) if isinstance(meta, dict) else {}
    if not limits:
        return body
    # 查表顺序：全名 → 后缀匹配（网关模型 ID 形如 "{provider}-{model}"）。
    # 不能用 split("-",1) —— 当 provider 名自身含 "-" 时，
    # 前缀会被切掉、后缀永远匹配不上。
    cap = None
    if model in limits:
        cap = limits[model]
    else:
        for name, lim in limits.items():
            if model == name or model.endswith("-" + name):
                cap = lim
                break
    if not cap:
        return body
    for k in ("max_tokens", "max_completion_tokens"):
        v = body.get(k)
        if isinstance(v, int) and v > cap:
            logger.info("clamp max_tokens for %s: %d -> %d", model, v, cap)
            body[k] = cap
    return body


# 轮询计数（从 config 加载，poll_all 里增量更新）
poll_count_state = app_config.get("poll_count", 0)
last_daily_poll_state = app_config.get("last_daily_poll", "")
poll_stage = "init"  # init | waiting | fetching_models | running | idle


def mark_full_check():
    """记录一次完整检测的时间，用于重置自动轮询计时"""
    global last_check_time
    last_check_time = time.time()


# ============================================================
# 历史记录（异步文件 IO）
# ============================================================
def _append_history_sync(snapshot: dict):
    line = json.dumps({"time": time.time(), "data": snapshot}, ensure_ascii=False) + "\n"
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(line)


async def append_history(snapshot: dict):
    await asyncio.to_thread(_append_history_sync, snapshot)


def _read_history_sync(hours: int):
    if not HISTORY_FILE.exists():
        return []
    cutoff = time.time() - hours * 3600
    records = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line.strip())
                if rec["time"] >= cutoff:
                    records.append(rec)
            except Exception:
                pass
    return records


async def read_history(hours: int = 24):
    async with history_lock:
        return await asyncio.to_thread(_read_history_sync, hours)


def _cleanup_history_sync():
    if not HISTORY_FILE.exists():
        return 0
    cutoff = time.time() - MAX_HISTORY_DAYS * 86400
    kept = []
    removed = 0
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line.strip())
                if rec["time"] >= cutoff:
                    kept.append(line if line.endswith("\n") else line + "\n")
                else:
                    removed += 1
            except Exception:
                pass
    if removed > 0:
        atomic_write(HISTORY_FILE, "".join(kept))
    return removed


async def maybe_cleanup_history():
    global last_history_cleanup
    now = time.time()
    if now - last_history_cleanup < HISTORY_CLEANUP_INTERVAL:
        return
    last_history_cleanup = now
    n = await asyncio.to_thread(_cleanup_history_sync)
    if n:
        logger.info("history cleanup: removed %d expired records", n)
    un = await asyncio.to_thread(_cleanup_usage_sync)
    if un:
        logger.info("usage cleanup: removed %d expired records", un)
    cn = await asyncio.to_thread(_cleanup_call_log_sync)
    if cn:
        logger.info("call log cleanup: removed %d expired records", cn)


# ============================================================
# 消耗统计（异步文件 IO）
# ============================================================
def _append_usage_sync(record: dict):
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(USAGE_FILE, "a", encoding="utf-8") as f:
        f.write(line)


async def append_usage(record: dict):
    await asyncio.to_thread(_append_usage_sync, record)


def _read_usage_sync(days: int):
    if not USAGE_FILE.exists():
        return []
    cutoff = time.time() - days * 86400
    records = []
    with open(USAGE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line.strip())
                if rec.get("ts", 0) >= cutoff:
                    records.append(rec)
            except Exception:
                pass
    return records


async def read_usage(days: int = 1):
    async with usage_lock:
        return await asyncio.to_thread(_read_usage_sync, days)


def get_usage_retention_days() -> int:
    """返回当前配置的消耗数据保留天数（config.json 可调，默认 720）"""
    try:
        v = int(app_config.get("usage_retention_days", MAX_USAGE_DAYS) or MAX_USAGE_DAYS)
        return max(1, min(v, 7200))
    except Exception:
        return MAX_USAGE_DAYS


def _cleanup_usage_sync():
    if not USAGE_FILE.exists():
        return 0
    cutoff = time.time() - get_usage_retention_days() * 86400
    kept = []
    removed = 0
    with open(USAGE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line.strip())
                if rec.get("ts", 0) >= cutoff:
                    kept.append(line if line.endswith("\n") else line + "\n")
                else:
                    removed += 1
            except Exception:
                pass
    if removed > 0:
        atomic_write(USAGE_FILE, "".join(kept))
    return removed


# ============================================================
# 模型工具函数
# ============================================================
def is_chat_model(model_id: str) -> bool:
    lower = model_id.lower()
    return not any(kw in lower for kw in NON_CHAT_KEYWORDS)


def is_free_model(model_info: dict) -> bool:
    pricing = model_info.get("pricing", {})
    prompt_price = pricing.get("prompt", "")
    completion_price = pricing.get("completion", "")
    try:
        if float(prompt_price) == 0 and float(completion_price) == 0:
            return True
    except (ValueError, TypeError):
        pass
    return False


def is_free_by_name(model_id: str) -> bool:
    lower = model_id.lower()
    return ":free" in lower or "-free" in lower


def get_enabled_models(provider: dict) -> list[str]:
    """返回该 provider 未被禁用的模型列表"""
    disabled = set(provider.get("disabled_models", []))
    return [m for m in provider.get("models", []) if m not in disabled]


def normalize_model(model: str) -> str:
    """归一化模型名到标准名（去 xxx/ 前缀 + 转小写）。
    先查别名表做名字修正（如 mistral-small-2603 → mistral-small-4-119b-2603），
    再统一去前缀转小写。这样不同平台/大小写命名都归一到同一标准名。"""
    if model in MODEL_ALIASES:
        model = MODEL_ALIASES[model]
    return model.split('/')[-1].lower()


def get_context_length(model: str) -> int:
    # ① 归一化名查表
    norm = normalize_model(model)
    ctx = CONTEXT_LIMITS.get(norm) or CONTEXT_LIMITS.get(model)
    if ctx:
        return ctx
    # ② 大小写回退（防标准名表里仍存了带前缀/带大小写的旧键）
    lower = model.lower()
    for k, v in CONTEXT_LIMITS.items():
        if k.lower() == lower:
            return v
    # ③ 轮询拉取的 model_details
    return (model_details.get(norm, {}).get("context_length")
            or model_details.get(model, {}).get("context_length")
            or 32768)


def is_vision_model(model: str) -> bool:
    """是否支持识图（基于 supports_vision 标记 + 归一化匹配）"""
    norm = normalize_model(model)
    return bool(SUPPORTS_VISION.get(norm) or SUPPORTS_VISION.get(model))


def is_1m_model(model: str) -> bool:
    ctx = get_context_length(model)
    return bool(ctx) and ctx >= ONE_MILLION


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 12:
        return "****"
    return key[:6] + "****" + key[-4:]


# ============================================================
# 质量分（内存滑动窗口）
# ============================================================
def update_model_quality(key: str, info: dict):
    q = model_quality.get(key)
    if q is None:
        q = {"status_window": deque(maxlen=QUALITY_WINDOW), "latencies": deque(maxlen=QUALITY_WINDOW)}
        model_quality[key] = q
    st = info.get("status", "unknown")
    if st in ("ok", "fail", "error"):
        q["status_window"].append(st)
    if st == "ok":
        lat = info.get("latency_ms")
        if lat:
            q["latencies"].append(lat)


def get_quality_score(key: str) -> float:
    """0~1 可用率，无数据返回 1.0（乐观）"""
    q = model_quality.get(key)
    if not q or not q["status_window"]:
        return 1.0
    ok_count = sum(1 for s in q["status_window"] if s == "ok")
    return ok_count / len(q["status_window"])


def get_avg_latency(key: str):
    q = model_quality.get(key)
    if not q or not q["latencies"]:
        return None
    return sum(q["latencies"]) / len(q["latencies"])


# ============================================================
# 熔断
# ============================================================
def is_circuit_open(key: str) -> bool:
    cb = circuit_breaker.get(key)
    if not cb:
        return False
    if cb.get("open_until") and time.time() >= cb["open_until"]:
        cb["fails"] = 0
        cb["open_until"] = 0
        return False
    return bool(cb.get("open_until"))


def record_fail(key: str):
    cb = circuit_breaker.setdefault(key, {"fails": 0, "open_until": 0})
    cb["fails"] += 1
    if cb["fails"] >= CIRCUIT_FAIL_THRESHOLD:
        cb["open_until"] = time.time() + CIRCUIT_RECOVERY_SECONDS
        logger.warning("circuit opened: %s", key)


def record_success(key: str):
    cb = circuit_breaker.get(key)
    if cb:
        cb["fails"] = 0
        cb["open_until"] = 0


# ============================================================
# 探测
# ============================================================
async def check_model(base_url: str, api_key: str, model: str) -> dict:
    actual_model = MODEL_ALIASES.get(model, model)
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": actual_model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    start = time.time()
    try:
        resp = await http_client.post(url, json=payload, headers=headers, timeout=30)
        latency = round((time.time() - start) * 1000)
        if resp.status_code == 200:
            usage = resp.json().get("usage", {})
            return {
                "status": "ok",
                "code": resp.status_code,
                "latency_ms": latency,
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            }
        return {
            "status": "fail",
            "code": resp.status_code,
            "latency_ms": latency,
            "detail": resp.text[:200],
        }
    except Exception as e:
        latency = round((time.time() - start) * 1000)
        return {"status": "error", "latency_ms": latency, "detail": str(e)[:200]}


async def fetch_model_details(base_url: str, api_key: str) -> dict:
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = await http_client.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            model_list = data.get("data", data) if isinstance(data, dict) else data
            details = {}
            for m in model_list:
                if isinstance(m, dict) and "id" in m:
                    pricing = m.get("pricing", {})
                    details[m["id"]] = {
                        "context_length": m.get("context_length"),
                        "prompt_price": pricing.get("prompt", ""),
                        "completion_price": pricing.get("completion", ""),
                    }
            return details
    except Exception:
        logger.exception("fetch_model_details failed for %s", base_url)
    return {}


async def fetch_models(base_url: str, api_key: str, free_only: bool = True) -> list[str]:
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = await http_client.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            model_list = data.get("data", data) if isinstance(data, dict) else data
            if not isinstance(model_list, list):
                return []
            if free_only:
                has_pricing = any(isinstance(m, dict) and m.get("pricing") for m in model_list)
                if has_pricing:
                    free_by_api = [
                        m for m in model_list
                        if isinstance(m, dict) and "id" in m and is_free_model(m)
                    ]
                    if free_by_api:
                        return [m["id"] for m in free_by_api if is_chat_model(m["id"])]
                free_by_name = [
                    m["id"] for m in model_list
                    if isinstance(m, dict) and "id" in m
                    and is_free_by_name(m["id"]) and is_chat_model(m["id"])
                ]
                if free_by_name:
                    return free_by_name
            return [
                m["id"] for m in model_list
                if "id" in m and isinstance(m, dict) and is_chat_model(m["id"])
            ]
    except Exception:
        logger.exception("fetch_models failed for %s", base_url)
    return []


async def verify_provider_key_impl(base_url: str, api_key: str) -> dict:
    """校验上游 key 是否有效：调上游 /models 接口，401/403 判定 key 无效"""
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = await http_client.get(url, headers=headers, timeout=15)
        if resp.status_code in (401, 403):
            return {"ok": False, "detail": f"Key 无效（HTTP {resp.status_code}）"}
        if resp.status_code == 200:
            return {"ok": True, "detail": "连接成功"}
        return {"ok": False, "detail": f"上游返回 HTTP {resp.status_code}"}
    except httpx.RequestError as e:
        return {"ok": False, "detail": f"连接失败：{str(e)[:150]}"}
    except Exception as e:
        return {"ok": False, "detail": f"校验异常：{str(e)[:150]}"}


# ============================================================
# 轮询
# ============================================================
async def run_health_checks(tasks: list[tuple[str, str, str, str]]) -> dict:
    """并发检测所有 (name, model, base_url, api_key) 任务，返回 {key: result}。"""
    sem = asyncio.Semaphore(10)

    async def limited_check(url, key, m):
        async with sem:
            return await check_model(url, key, m)

    results = await asyncio.gather(
        *[limited_check(url, key, m) for _, m, url, key in tasks],
        return_exceptions=True,
    )
    new_status = {}
    for (name, m, _, _), result in zip(tasks, results):
        k = f"{name}||{m}"
        if isinstance(result, Exception):
            new_status[k] = {
                "status": "error",
                "detail": str(result)[:200],
                "checked_at": time.time(),
            }
        else:
            result["checked_at"] = time.time()
            new_status[k] = result
        update_model_quality(k, new_status[k])
    return new_status


async def poll_all():
    global health_status, last_poll_time, last_check_time, poll_count_state, last_daily_poll_state, poll_stage
    # 先让窗口加载出来，避免卡在启动页
    poll_stage = "waiting"
    await asyncio.sleep(1.5)

    # 并发拉取所有 provider 的 model details（原来串行很慢）
    poll_stage = "fetching_models"
    async def fetch_one(p):
        try:
            # 探测用「第一个未被拉黑的 Key」，避免坏 Key[0] 把整个 provider 误判为不健康
            pool = _available_keys(p)
            ak = pool[0] if pool else ""
            return await fetch_model_details(p["base_url"], ak)
        except Exception:
            logger.exception("model_details fetch failed: %s", p.get("name"))
            return {}
    results = await asyncio.gather(*[fetch_one(p) for p in list(providers)])
    for details in results:
        if details:
            model_details.update(details)

    while True:
        try:
            # 每天中午 12 点强制检测一次（无论 poll_count 是否超过 20）
            today = time.strftime("%Y-%m-%d")
            now_hour = time.localtime().tm_hour
            should_daily_poll = (today != last_daily_poll_state and now_hour >= 12)

            # 超过 20 次且不是每日检测时间 → 跳过
            if poll_count_state >= POLL_MAX_COUNT and not should_daily_poll:
                poll_stage = "idle"
                await asyncio.sleep(30)
                continue

            poll_stage = "running"
            tasks = []
            for p in list(providers):
                for m in get_enabled_models(p):
                    tasks.append((p["name"], m, p["base_url"], (p.get("api_keys", [""])[0] if p.get("api_keys") else "")))
            new_status = await run_health_checks(tasks)
            health_status = new_status
            last_poll_time = time.time()
            last_check_time = last_poll_time
            await append_history(new_status)
            await maybe_cleanup_history()
            ok_count = sum(1 for v in new_status.values() if v.get("status") == "ok")
            logger.info("poll done: %d/%d ok", ok_count, len(new_status))

            # 更新轮询计数 & 每日检测标记
            poll_count_state += 1
            app_config["poll_count"] = poll_count_state
            if should_daily_poll:
                last_daily_poll_state = today
                app_config["last_daily_poll"] = today
            save_config()

        except Exception:
            logger.exception("poll_all loop error")
        # 等待到 last_check_time + POLL_INTERVAL
        while time.time() < last_check_time + POLL_INTERVAL:
            await asyncio.sleep(5)


# ============================================================
# lifespan
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, poll_task
    # 运行时日志落盘（带轮转，避免无限膨胀）；放在此处确保 uvicorn 配置 logging 后再挂，不被清空
    try:
        _fh = RotatingFileHandler(DATA_DIR / "gateway.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        logging.getLogger().addHandler(_fh)
    except Exception:
        pass
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    app.state.http = http_client
    # ★ 健康探测总开关：默认**不启**探测。
    #   poll_all() 每 300s 对所有启用模型发真实 chat 请求（"hi", max_tokens:5），
    #   会产生额外的上游流量并消耗额度。
    #   需要时在 config.json 写 {"health_poll_enabled": true} 并重启即可恢复。
    if app_config.get("health_poll_enabled"):
        poll_task = asyncio.create_task(poll_all())
        logger.info("健康探测已启用（health_poll_enabled=true）")
    else:
        poll_task = None
        logger.info("健康探测已关闭（health_poll_enabled 缺省）—— 不发起任何主动探测")
    yield
    if poll_task:
        poll_task.cancel()
    await http_client.aclose()


app = FastAPI(title="模型API网关", lifespan=lifespan)
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.auto_reload = False
templates.env.cache = None


@app.middleware("http")
async def no_cache_middleware(request: Request, call_next):
    """给页面和 API 响应加 no-cache，避免 pywebview/浏览器缓存旧前端。"""
    resp = await call_next(request)
    if request.url.path in ("/",) or request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# ============================================================
# Pydantic 模型
# ============================================================
class ProviderIn(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    api_keys: list[str] = []
    models: list[str] = []
    free_only: bool = True
    key_strategy: str = "sticky"


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_keys: list[str] | None = None
    models: list[str] | None = None
    free_only: bool | None = None
    key_strategy: str | None = None


class ToggleModelIn(BaseModel):
    model: str
    enabled: bool


class VerifyKeyIn(BaseModel):
    base_url: str
    api_key: str


class OpenUrlIn(BaseModel):
    url: str


class PresetApplyIn(BaseModel):
    keys: dict = {}


# ============================================================
# 模型选择
# ============================================================
def pick_available_models(model: str | None = None, force: bool = False) -> list[tuple[dict, str]]:
    """返回按质量排序的候选 (provider, model) 列表"""
    
    raw = []
    unhealthy_raw = []
    
    # 如果请求的是自定义路由组
    if model in ROUTERS:
        order = list(ROUTERS[model])          # 保留声明顺序（勿用 set 丢序）
        target_models = set(order)
        for p in providers:
            for m in get_enabled_models(p):
                if m in target_models:
                    k = f"{p['name']}||{m}"
                    raw.append((p, m, k))
        # ★ 严格顺序模式（config.json: router_strict_order=true）
        # 按 routers.json 成员的声明顺序返回候选：靠前的候选只有真正失败
        # （连接错误 / 4xx / 5xx / 超时）才由 _stream_with_failover 顺延到下一个。
        # 默认 false = 保持原有（可用率+延迟）排序。
        if app_config.get("router_strict_order"):
            idx = {name: i for i, name in enumerate(order)}
            raw.sort(key=lambda x: idx.get(x[1], len(order)))
            return [(p, m) for p, m, _ in raw]
        scored = [
            (get_quality_score(k), get_avg_latency(k) or 1e9, p, m)
            for p, m, k in raw
        ]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [(p, m) for _, _, p, m in scored]

    # 否则按具体模型匹配
    for p in providers:
        for m in get_enabled_models(p):
            prefixed = f"{p['name']}-{m}"
            if model and model != m and model != prefixed:
                continue
            k = f"{p['name']}||{m}"
            if force or model:
                raw.append((p, m, k))
                continue
            st = health_status.get(k, {}).get("status")
            if not is_circuit_open(k) and st in ("ok", None, "unknown"):
                raw.append((p, m, k))
            else:
                unhealthy_raw.append((p, m, k))
    if not raw:
        raw = unhealthy_raw
    scored = [
        (get_quality_score(k), get_avg_latency(k) or 1e9, p, m)
        for p, m, k in raw
    ]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(p, m) for _, _, p, m in scored]


def pick_available_model(model: str | None = None, force: bool = False):
    cands = pick_available_models(model, force)
    return cands[0] if cands else (None, None)


# ============================================================
# Hermes 工具名压缩 / 还原
# ============================================================
HERMES_MAP = [
    ("mcp_hermes_studio_use_hermes_studio_use_", "mcp_hsu_"),
    ("mcp_hermes_studio_devices_hermes_studio_lan_", "mcp_hsd_"),
    ("mcp_hermes_studio_api_hermes_studio_api_", "mcp_hsa_"),
]


def compress_hermes(obj: dict) -> dict:
    s = json.dumps(obj, ensure_ascii=False)
    for long, short in HERMES_MAP:
        s = s.replace(long, short)
    return json.loads(s)


def restore_hermes_text(text: str) -> str:
    for long, short in HERMES_MAP:
        text = text.replace(short, long)
    return text


def merge_reasoning(obj: dict) -> dict:
    """保留 reasoning_content 字段原样透传，不合并到 content"""
    return obj

# ============================================================
# 回复语言跟随：根据用户消息语言决定回复语言
# ============================================================
LANG_HINT = (
    "\n\n【重要】请始终使用简体中文回答用户。"
    "思考过程(reasoning)也请用中文。"
    "代码、命令、文件名、专有名词、标识符等保持原样即可，不要翻译。"
)


def ensure_lang_reply(body: dict) -> dict:
    """注入简体中文回复提示。
    - 已有 system 且为纯文本：在末尾追加指令（带判重，幂等）。
    - 无 system：在最前面插入一条 system。"""
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return body
    first = msgs[0]
    if isinstance(first, dict) and first.get("role") == "system":
        c = first.get("content")
        if isinstance(c, str) and "请始终使用简体中文" not in c:
            first["content"] = c.rstrip() + LANG_HINT
        return body
    msgs.insert(0, {"role": "system", "content": "请使用简体中文回答。" + LANG_HINT})
    return body


# ============================================================
# 页面
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    auth_header = request.headers.get("Authorization", "")
    is_authorized = _check_basic_auth(auth_header) or auth_header == f"Bearer {LOCAL_API_KEY}"
    if not is_authorized:
        from fastapi.responses import Response
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="model-gateway"'})
    ctx = {
        "request": request,
        "local_api_key": LOCAL_API_KEY,
        "app_version": APP_VERSION,
    }
    return templates.TemplateResponse(request, "index.html", ctx)


# ============================================================
# 管理接口（admin 鉴权）
# ============================================================
@app.get("/api/poll-status")
async def poll_status(_=Depends(verify_admin)):
    return {
        "last_poll_time": last_poll_time,
        "total_models": sum(len(get_enabled_models(p)) for p in providers),
        "poll_count": poll_count_state,
        "poll_max": POLL_MAX_COUNT,
        "stage": poll_stage,
    }


@app.get("/api/history")
async def get_history(hours: int = 24, _=Depends(verify_admin)):
    return await read_history(hours)


_stability_cache: dict = {}
STABILITY_CACHE_TTL = 30


@app.get("/api/stability")
async def get_stability(hours: int = 24, _=Depends(verify_admin)):
    now = time.time()
    cached = _stability_cache.get(hours)
    if cached and now - cached[0] < STABILITY_CACHE_TTL:
        return cached[1]
    records = await read_history(hours)
    model_stats: dict = {}
    for rec in records:
        for key, info in rec.get("data", {}).items():
            if key not in model_stats:
                model_stats[key] = {"ok": 0, "fail": 0, "error": 0, "total": 0, "latencies": []}
            model_stats[key]["total"] += 1
            st = info.get("status", "unknown")
            if st == "ok":
                model_stats[key]["ok"] += 1
                if info.get("latency_ms"):
                    model_stats[key]["latencies"].append(info["latency_ms"])
            elif st == "fail":
                model_stats[key]["fail"] += 1
            elif st == "error":
                model_stats[key]["error"] += 1
    allowed = set()

    for p in providers:
        for m in p.get("models", []):
            k = f"{p['name']}||{m}"
            allowed.add(k)
            if k not in model_stats:
                model_stats[k] = {"ok": 0, "fail": 0, "error": 0, "total": 0, "latencies": []}
    model_stats = {k: v for k, v in model_stats.items() if k in allowed}
    result = []
    for key, s in model_stats.items():
        name, model = key.split("||", 1)
        avg_lat = sum(s["latencies"]) / len(s["latencies"]) if s["latencies"] else None
        result.append({
            "provider": name,
            "model": model,
            "checks": s["total"],
            "ok": s["ok"],
            "fail": s["fail"],
            "error": s["error"],
            "availability": round(s["ok"] / s["total"] * 100, 1) if s["total"] else 0,
            "avg_latency_ms": round(avg_lat) if avg_lat else None,
            "min_latency_ms": min(s["latencies"]) if s["latencies"] else None,
            "max_latency_ms": max(s["latencies"]) if s["latencies"] else None,
            "last_status": health_status.get(key, {}).get("status", "unknown"),
            "vision": is_vision_model(model),
        })
    # 隐藏从未成功过的模型（检查过但 ok=0），新加入的模型（checks=0）正常展示
    result = [r for r in result if not (r["checks"] > 0 and r["ok"] == 0)]
    result.sort(key=lambda x: (-x["availability"], x["avg_latency_ms"] or 99999))
    _stability_cache[hours] = (now, result)
    return result


@app.get("/api/usage")
async def get_usage(days: int = 1, _=Depends(verify_admin)):
    days = max(1, min(days, get_usage_retention_days()))
    records = await read_usage(days)
    total = {"pt": 0, "ct": 0, "tt": 0, "cache_pt": 0, "requests": 0}
    by_day = {}
    by_model = {}
    for r in records:
        ts = r.get("ts", 0)
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        pt = r.get("pt", 0) or 0
        ct = r.get("ct", 0) or 0
        tt = r.get("tt", 0) or (pt + ct)
        cp = r.get("cache_pt", 0) or 0
        m = r.get("model", "unknown")
        p = r.get("provider", "unknown")
        total["pt"] += pt
        total["ct"] += ct
        total["tt"] += tt
        total["cache_pt"] += cp
        total["requests"] += 1
        d = by_day.setdefault(day, {"pt": 0, "ct": 0, "tt": 0, "cache_pt": 0, "requests": 0})
        d["pt"] += pt
        d["ct"] += ct
        d["tt"] += tt
        d["cache_pt"] += cp
        d["requests"] += 1
        mk = f"{p} · {m}"
        mm = by_model.setdefault(mk, {"pt": 0, "ct": 0, "tt": 0, "cache_pt": 0, "requests": 0, "provider": p, "model": m})
        mm["pt"] += pt
        mm["ct"] += ct
        mm["tt"] += tt
        mm["cache_pt"] += cp
        mm["requests"] += 1
    by_day_list = [{"date": d, **v} for d, v in sorted(by_day.items())]
    by_model_list = [
        {"provider": v["provider"], "model": v["model"], "pt": v["pt"], "ct": v["ct"],
         "tt": v["tt"], "cache_pt": v["cache_pt"], "requests": v["requests"]}
        for _, v in sorted(by_model.items(), key=lambda x: -x[1]["tt"])
    ]
    return {"days": days, "total": total, "by_day": by_day_list, "by_model": by_model_list}


@app.get("/api/model-details")
async def get_model_details(_=Depends(verify_admin)):
    merged = {}
    # 1. 上游探测结果（键为上游模型 id / 原始名）
    for k, v in model_details.items():
        merged[k] = dict(v)
    # 2. 对 providers 里每个模型(原始名)，用别名归一化查 meta 兜底
    #    解决魔搭等 provider 用别名形式(如 ZhipuAI/GLM-5.2)而 meta 里
    #    只有规范化名(如 glm-5.2) 导致前端查不到上下文/描述的问题

    for p in providers:
        for m in p.get("models", []):
            entry = merged.setdefault(m, {})
            norm = MODEL_ALIASES.get(m, m)
            meta_desc = MODEL_DESCRIPTIONS.get(norm, {})
            if not entry.get("context_length"):
                ctx = meta_desc.get("ctx") or CONTEXT_LIMITS.get(norm)
                if ctx:
                    entry["context_length"] = ctx
            if not entry.get("desc"):
                desc = meta_desc.get("desc", "")
                if desc:
                    entry["desc"] = desc
    # 3. 对 meta 里规范化名也建条目（兼容以规范化名查询）
    for k, v in MODEL_DESCRIPTIONS.items():
        if k not in merged:
            merged[k] = {}
        # 上游 context_length 为 None/0/缺失时，用元数据覆盖
        if not merged[k].get("context_length"):
            merged[k]["context_length"] = v.get("ctx")
        merged[k]["desc"] = v.get("desc", "")
    return merged


@app.get("/api/context-limits")
async def get_context_limits(_=Depends(verify_admin)):
    return {"ok": True, "data": CONTEXT_LIMITS}

class ContextLimitUpdate(BaseModel):
    model: str
    context_length: int

@app.put("/api/context-limits")
async def update_context_limit(req: ContextLimitUpdate, _=Depends(verify_admin)):
    global CONTEXT_LIMITS, meta
    meta = load_meta()
    if "context_limits" not in meta:
        meta["context_limits"] = {}
    meta["context_limits"][req.model] = req.context_length
    CONTEXT_LIMITS[req.model] = req.context_length
    atomic_write(META_FILE, json.dumps(meta, indent=2, ensure_ascii=False))
    return {"ok": True}


@app.delete("/api/context-limits/{model}")
async def delete_context_limit(model: str, _=Depends(verify_admin)):
    """删除某条自定义上下文长度配置"""
    global CONTEXT_LIMITS, meta
    meta = load_meta()
    if "context_limits" in meta and model in meta["context_limits"]:
        del meta["context_limits"][model]
    CONTEXT_LIMITS.pop(model, None)
    atomic_write(META_FILE, json.dumps(meta, indent=2, ensure_ascii=False))
    return {"ok": True}


@app.get("/api/routers")
async def get_routers_api(_=Depends(verify_admin)):
    return {"ok": True, "data": ROUTERS}

@app.post("/api/routers")
async def save_routers_api(request: Request, _=Depends(verify_admin)):
    global ROUTERS
    body = await request.json()
    ROUTERS = body
    save_routers()
    return {"ok": True}


@app.get("/api/vision-models")
async def vision_models_api(_=Depends(verify_admin)):
    """返回 supports_vision 标记的模型名列表，供前端识图配置标记"""
    return {"ok": True, "data": sorted(SUPPORTS_VISION.keys())}


# ---------- 系统公告（Gitee 远程，本地兜底） ----------
# ★ 2026-09-28 封堵作者远程控制：改为本地占位（必失败 → 回退本地 announcement.json）
# 原值: https://gitee.com/ywtc000/dongye/raw/master/announcement.md
DEFAULT_ANNOUNCEMENT_URL = "http://127.0.0.1:9/announcement-local.md"
ANNOUNCEMENT_CACHE_FILE = DATA_DIR / "announcement_cache.json"
_announcement_cache = {"content": None, "ts": 0}
ANNOUNCEMENT_TTL = 300


def _content_hash(content: str) -> str:
    """计算内容 MD5 指纹，用于前端检测公告变动"""
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def _announce_response(ok: bool, content: str) -> dict:
    return {"ok": ok, "content": content, "hash": _content_hash(content)}


@app.get("/api/announcement")
async def get_announcement(_=Depends(verify_admin)):
    """优先读 config.json 的 announcement_url（如 Gitee raw 链接）远程抓取；
    未配置或抓取失败时回退到本地 announcement.json。远程结果缓存 5 分钟。"""
    cfg = load_config()
    url = cfg.get("announcement_url") or DEFAULT_ANNOUNCEMENT_URL
    now = time.time()
    if _announcement_cache["content"] is not None and now - _announcement_cache["ts"] < ANNOUNCEMENT_TTL:
        return _announce_response(True, _announcement_cache["content"])
    # 远程抓取
    try:
        resp = await http_client.get(url, timeout=10, follow_redirects=True)
        if resp.status_code == 200 and resp.text.strip():
            content = resp.text
            _announcement_cache["content"] = content
            _announcement_cache["ts"] = now
            # 持久化到本地缓存文件，断网时回退显示上次成功的内容
            try:
                atomic_write(ANNOUNCEMENT_CACHE_FILE, json.dumps({"content": content, "ts": now}, ensure_ascii=False))
            except Exception:
                logger.warning("write announcement cache file failed")
            return _announce_response(True, content)
    except Exception:
        logger.warning("fetch remote announcement failed: %s", url)
    # 远程失败：读本地缓存文件（上次成功抓取的内容）
    if ANNOUNCEMENT_CACHE_FILE.exists():
        try:
            data = json.loads(ANNOUNCEMENT_CACHE_FILE.read_text(encoding="utf-8"))
            if data.get("content"):
                return _announce_response(True, data["content"])
        except Exception:
            pass
    # 最终兜底：默认 announcement.json
    if ANNOUNCEMENT_FILE.exists():
        try:
            data = json.loads(ANNOUNCEMENT_FILE.read_text(encoding="utf-8"))
            return _announce_response(True, data.get("content", ""))
        except Exception:
            logger.exception("parse announcement.json failed")
    return _announce_response(False, "暂无公告内容。")


# ---------- 在线更新 ----------
# ★ 2026-09-28 封堵作者远程控制：不再连作者仓库查版本
# 原值: https://gitee.com/ywtc000/dongye/raw/master/version.json
VERSION_CHECK_URL = "http://127.0.0.1:9/version-local.json"
_update_download_state = {
    "downloading": False,
    "progress": 0,
    "total": 0,
    "done": False,
    "error": None,
    "file": None,
}


def _version_gt(a: str, b: str) -> bool:
    """比较版本号 a > b"""
    try:
        pa = [int(x) for x in a.split(".")]
        pb = [int(x) for x in b.split(".")]
        while len(pa) < len(pb):
            pa.append(0)
        while len(pb) < len(pa):
            pb.append(0)
        return pa > pb
    except Exception:
        return a.strip() != b.strip()


def _cleanup_old_exe():
    """启动时清理上次更新遗留的 .old 文件"""
    old_path = sys.executable + ".old"
    if os.path.exists(old_path):
        try:
            os.remove(old_path)
        except Exception:
            pass


@app.get("/api/check-update")
async def check_update(_=Depends(verify_admin)):
    """检查 gitee 是否有新版本"""
    cfg = load_config()
    url = cfg.get("version_check_url") or VERSION_CHECK_URL
    try:
        resp = await http_client.get(url, timeout=10, follow_redirects=True)
        if resp.status_code == 200:
            data = resp.json()
            if not isinstance(data, dict):
                return {"ok": False, "error": "版本信息格式错误"}
            latest_ver = data.get("version", "")
            has_update = _version_gt(latest_ver, APP_VERSION)
            min_ver = data.get("min_version", "")
            force_update = bool(min_ver and _version_gt(min_ver, APP_VERSION))
            return {
                "ok": True,
                "current": APP_VERSION,
                "latest": latest_ver,
                "has_update": has_update,
                "force_update": force_update,
                "download_url": data.get("download_url", ""),
                "release_notes": data.get("release_notes", ""),
                "min_version": min_ver,
            }
    except Exception as e:
        logger.warning("check update failed: %s", e)
    return {"ok": False, "error": "无法连接更新服务器"}


@app.post("/api/start-download")
async def start_download(data: dict, _=Depends(verify_admin)):
    """启动后台下载新版 exe，返回后通过 /api/download-progress 轮询进度"""
    url = data.get("url", "")
    if not url:
        return {"ok": False, "error": "缺少下载地址"}
    if _update_download_state["downloading"]:
        return {"ok": False, "error": "正在下载中，请稍候"}
    _update_download_state.update({
        "downloading": True, "progress": 0, "total": 0,
        "done": False, "error": None, "file": None,
    })
    asyncio.create_task(_do_download(url))
    return {"ok": True}


async def _do_download(url: str):
    """后台下载任务，流式写入临时文件，实时更新进度"""
    import tempfile
    # 只取临时文件名，不保持句柄（Windows 下未关闭的句柄会导致后续 open 失败）
    fd, tmp_path = tempfile.mkstemp(suffix=".exe", dir=DATA_DIR)
    os.close(fd)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=15)) as client:
            async with client.stream("GET", url, follow_redirects=True) as resp:
                if resp.status_code != 200:
                    _update_download_state["error"] = f"下载失败: HTTP {resp.status_code}"
                    _update_download_state["downloading"] = False
                    return
                content_length = resp.headers.get("content-length")
                total = int(content_length) if content_length else 0
                _update_download_state["total"] = total
                downloaded = 0
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            _update_download_state["progress"] = round(downloaded / total * 100, 1)
        _update_download_state["done"] = True
        _update_download_state["file"] = tmp_path
        _update_download_state["downloading"] = False
    except Exception as e:
        _update_download_state["error"] = str(e)
        _update_download_state["downloading"] = False
        try:
            os.remove(tmp_path)
        except Exception:
            pass


@app.get("/api/download-progress")
async def download_progress(_=Depends(verify_admin)):
    """返回当前下载进度"""
    return {"ok": True, **{k: v for k, v in _update_download_state.items()}}


@app.post("/api/apply-update")
async def apply_update(_=Depends(verify_admin)):
    """应用更新：替换 exe 并重启"""
    if not getattr(sys, 'frozen', False):
        return {"ok": False, "error": "开发模式下不支持热更新，请打包后使用"}
    if not _update_download_state["done"] or not _update_download_state["file"]:
        return {"ok": False, "error": "没有可应用的更新"}
    new_file = _update_download_state["file"]
    if not os.path.exists(new_file):
        return {"ok": False, "error": "更新文件不存在"}
    try:
        _do_swap_and_restart(new_file)
    except Exception as e:
        return {"ok": False, "error": f"更新失败: {e}"}
    return {"ok": True}


def _do_swap_and_restart(new_exe: str):
    """重命名当前 exe → 替换新 exe → 启动新进程 → 退出当前进程"""
    import subprocess
    current_exe = sys.executable
    old_exe = current_exe + ".old"
    # 1. 删除旧残留
    if os.path.exists(old_exe):
        os.remove(old_exe)
    # 2. 当前 exe 改名为 .old（运行中的 exe 可以改名不能删）
    os.rename(current_exe, old_exe)
    # 3. 新 exe 移到当前 exe 位置
    os.rename(new_exe, current_exe)
    # 4. 启动新 exe
    creationflags = 0x00000008 if sys.platform == "win32" else 0  # DETACHED_PROCESS
    subprocess.Popen([current_exe], close_fds=True, creationflags=creationflags)
    # 5. 退出
    os._exit(0)


@app.get("/api/providers")
async def list_providers(_=Depends(verify_admin)):
    result = []

    for p in providers:
        keys = p.get("api_keys", [])
        item = {
            "name": p["name"],
            "base_url": p["base_url"],
            "api_key_masked": mask_key(keys[0]) if keys else "",
            "api_keys_masked": [mask_key(k) for k in keys],
            "api_key_count": len(keys),
            "models": p.get("models", []),
            "disabled_models": p.get("disabled_models", []),
            "free_only": p.get("free_only", True),
            "key_strategy": p.get("key_strategy", "sticky"),
            "health": {},
        }
        for m in p.get("models", []):
            k = f"{p['name']}||{m}"
            item["health"][m] = health_status.get(k, {"status": "unknown"})
        result.append(item)
    return result


@app.post("/api/providers")
async def add_provider(data: ProviderIn, _=Depends(verify_admin)):
    if not re.match(r'^[\u4e00-\u9fa5a-zA-Z0-9_.\-]+$', data.name):
        raise HTTPException(400, "名称只能包含中文、字母、数字、横杠(-)、下划线(_)、点(.)，不能含斜杠/空格等特殊字符")
    # 兼容旧格式：如果传了 api_key 单字符串，转为数组
    keys = list(data.api_keys) if data.api_keys else []
    if data.api_key and data.api_key not in keys:
        keys.append(data.api_key)
    if not keys:
        raise HTTPException(400, "至少需要一个 API Key")
    # 只校验第一个 key
    vr = await verify_provider_key_impl(data.base_url, keys[0])
    if not vr["ok"]:
        raise HTTPException(400, f"API Key 校验失败：{vr['detail']}")
    async with providers_lock:
        for p in providers:
            if p["name"] == data.name:
                raise HTTPException(400, "名称已存在")
        if not data.models:
            data.models = await fetch_models(data.base_url, keys[0], data.free_only)
        pd = data.model_dump()
        pd["api_keys"] = keys
        pd.pop("api_key", None)
        # 校验 key 策略
        if pd.get("key_strategy") not in VALID_KEY_STRATEGIES:
            pd["key_strategy"] = "sticky"
        providers.append(pd)
        save_providers(providers)
    return {"ok": True}


@app.post("/api/providers/verify-key")
async def verify_provider_key(data: VerifyKeyIn, _=Depends(verify_admin)):
    """校验上游 base_url + api_key 是否可用"""
    return await verify_provider_key_impl(data.base_url, data.api_key)


@app.post("/api/open-url")
async def open_url(data: OpenUrlIn, _=Depends(verify_admin)):
    """用系统默认浏览器打开外链（pywebview 内 target=_blank 会被拦截，统一走此接口）"""
    url = (data.url or "").strip()
    if not re.match(r'^https?://', url, re.I):
        raise HTTPException(400, "仅允许 http/https 链接")
    try:
        webbrowser.open(url)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"打开失败: {e}")


@app.get("/api/preset-info")
async def preset_info(_=Depends(verify_admin)):
    """返回预设清单（远端热更新优先，内置兜底）"""
    return await load_preset()


@app.get("/api/vision-assist")
async def get_vision_assist(_=Depends(verify_admin)):
    """返回识图辅助开关状态（默认关闭）"""
    cfg = app_config.get("vision_assist", {})
    enabled = cfg.get("enabled", False) if isinstance(cfg, dict) else False
    return {"enabled": enabled}


@app.put("/api/vision-assist")
async def set_vision_assist(data: dict, _=Depends(verify_admin)):
    """开启/关闭识图辅助，并持久化到 config.json"""
    enabled = bool(data.get("enabled", False))
    cfg = app_config.get("vision_assist", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["enabled"] = enabled
    app_config["vision_assist"] = cfg
    save_config()
    return {"enabled": enabled}


@app.post("/api/providers/preset")
async def apply_preset(data: PresetApplyIn, _=Depends(verify_admin)):
    """一键应用预设：三平台逐个校验 key → 创建/覆盖 provider → 合并路由组。
    - 用户填了 Key 的平台：校验 → 覆盖旧配置 → 重新拉模型
    - 用户没填 Key 但已有同名 provider：跳过，保留原配置不变
    - 用户没填 Key 且无同名 provider：标记未配置

    断电保护：保存前先备份，保存后删备份。下次启动若发现备份残留自动恢复。"""
    preset = await load_preset()
    platforms = preset.get("platforms", {})
    keys = data.keys or {}
    results = {}
    created_names = []

    # ---- 备份当前数据（防断电） ----
    bak_providers = DATA_FILE.with_suffix(DATA_FILE.suffix + ".preset_bak")
    bak_routers = ROUTERS_FILE.with_suffix(ROUTERS_FILE.suffix + ".preset_bak")
    try:
        if DATA_FILE.exists():
            bak_providers.write_text(DATA_FILE.read_text("utf-8"), "utf-8")
        if ROUTERS_FILE.exists():
            bak_routers.write_text(ROUTERS_FILE.read_text("utf-8"), "utf-8")
    except Exception:
        pass  # 备份失败不阻塞主流程

    try:
        async with providers_lock:
            existing_names = {p["name"] for p in providers}
            for plat_name, plat_cfg in platforms.items():
                key = (keys.get(plat_name) or "").strip()
                if not key:
                    if plat_name in existing_names:
                        existing = next((p for p in providers if p["name"] == plat_name), None)
                        if existing and existing.get("api_keys"):
                            key = existing["api_keys"][0]
                        else:
                            results[plat_name] = {"ok": True, "detail": "保留已有配置（无可用 Key）"}
                            continue
                    else:
                        results[plat_name] = {"ok": False, "detail": "未填写 Key"}
                        continue
                vr = await verify_provider_key_impl(plat_cfg["base_url"], key)
                if not vr["ok"]:
                    results[plat_name] = {"ok": False, "detail": vr["detail"]}
                    continue
                if plat_name in existing_names:
                    providers[:] = [p for p in providers if p["name"] != plat_name]
                    existing_names.discard(plat_name)
                fetched = await fetch_models(plat_cfg["base_url"], key, plat_cfg.get("free_only", True))
                visible = plat_cfg.get("models_visible", [])
                if visible:
                    models = [m for m in fetched if m in set(visible)]
                    for m in visible:
                        if m not in models:
                            models.append(m)
                    disabled = []
                else:
                    models = fetched
                    disabled = []
                providers.append({
                    "name": plat_name,
                    "base_url": plat_cfg["base_url"],
                    "api_keys": [key] if key else [],
                    "models": models,
                    "disabled_models": disabled,
                    "free_only": plat_cfg.get("free_only", True),
                })
                existing_names.add(plat_name)
                created_names.append(plat_name)
                results[plat_name] = {"ok": True, "detail": f"已配置 {len(models)} 个模型"}
            save_providers(providers)
            preset_routers = preset.get("routers", {})
            for gname, members in preset_routers.items():
                ROUTERS[gname] = list(members)
            save_routers()
    except Exception:
        raise
    finally:
        # ---- 保存成功，删除备份 ----
        try:
            if bak_providers.exists():
                bak_providers.unlink()
            if bak_routers.exists():
                bak_routers.unlink()
        except Exception:
            pass

    return {"ok": True, "results": results, "created": created_names}


@app.post("/api/providers/{name}/fetch-models")
async def refresh_models(name: str, _=Depends(verify_admin)):
    async with providers_lock:
        for p in providers:
            if p["name"] == name:
                models = await fetch_models(p["base_url"], p["api_keys"][0] if p.get("api_keys") else "", p.get("free_only", True))
                if models:
                    p["models"] = models
                    save_providers(providers)
                return {"ok": True, "models": models}
    raise HTTPException(404, "未找到")


@app.get("/api/providers/{name}/available-models")
async def get_available_models(name: str, _=Depends(verify_admin)):

    for p in providers:
        if p["name"] == name:
            keys = p.get("api_keys", [])
            ak = keys[0] if keys else ""
            models = await fetch_models(p["base_url"], ak, free_only=False)
            return {"ok": True, "models": models}
    raise HTTPException(404, "未找到")


@app.put("/api/providers/{name}")
async def update_provider(name: str, data: ProviderUpdate, _=Depends(verify_admin)):
    async with providers_lock:
        for i, p in enumerate(providers):
            if p["name"] == name:
                updates = data.model_dump(exclude_unset=True)
                if "api_key" in updates and "api_keys" not in updates:
                    if isinstance(updates["api_key"], str) and updates["api_key"]:
                        updates["api_keys"] = [updates["api_key"]]
                    del updates["api_key"]
                if "api_keys" in updates:
                    updates["api_keys"] = [k for k in updates["api_keys"] if k]
                # 校验 key 策略
                if updates.get("key_strategy") not in VALID_KEY_STRATEGIES:
                    updates.pop("key_strategy", None)
                providers[i].update(updates)
                save_providers(providers)
                return {"ok": True}
    raise HTTPException(404, "未找到")


@app.delete("/api/providers/{name}")
async def delete_provider(name: str, _=Depends(verify_admin)):
    global providers
    async with providers_lock:
        providers = [p for p in providers if p["name"] != name]
        save_providers(providers)
    return {"ok": True}


@app.post("/api/providers/{name}/add-key")
async def add_provider_key(name: str, request: Request, _=Depends(verify_admin)):
    """为 provider 添加一个新的 API Key"""
    body = await request.json()
    new_key = (body.get("api_key") or "").strip()
    if not new_key:
        raise HTTPException(400, "API Key 不能为空")
    async with providers_lock:
        for p in providers:
            if p["name"] == name:
                p.setdefault("api_keys", [])
                if new_key in p["api_keys"]:
                    return {"ok": True, "detail": "Key 已存在", "api_keys_masked": [mask_key(k) for k in p["api_keys"]]}
                p["api_keys"].append(new_key)
                save_providers(providers)
                return {"ok": True, "detail": "Key 已添加", "api_keys_masked": [mask_key(k) for k in p["api_keys"]]}
    raise HTTPException(404, "未找到")


@app.post("/api/providers/{name}/remove-key")
async def remove_provider_key(name: str, request: Request, _=Depends(verify_admin)):
    """从 provider 中删除一个 API Key（支持按索引或精确值）"""
    body = await request.json()
    key_idx = body.get("index")
    key_to_remove = (body.get("api_key") or "").strip()
    async with providers_lock:
        for p in providers:
            if p["name"] == name:
                keys = p.get("api_keys", [])
                if key_idx is not None and isinstance(key_idx, int):
                    if key_idx < 0 or key_idx >= len(keys):
                        return {"ok": False, "detail": "索引超出范围"}
                    key_to_remove = keys[key_idx]
                if not key_to_remove:
                    return {"ok": False, "detail": "API Key 不能为空"}
                if key_to_remove not in keys:
                    return {"ok": False, "detail": "Key 不存在"}
                if len(keys) <= 1:
                    return {"ok": False, "detail": "至少保留一个 Key"}
                keys.remove(key_to_remove)
                p["api_keys"] = keys
                save_providers(providers)
                return {"ok": True, "detail": "Key 已删除", "api_keys_masked": [mask_key(k) for k in keys]}
    raise HTTPException(404, "未找到")


@app.post("/api/providers/{name}/toggle-model")
async def toggle_model(name: str, data: ToggleModelIn, _=Depends(verify_admin)):
    async with providers_lock:
        for p in providers:
            if p["name"] == name:
                disabled = p.get("disabled_models", [])
                if data.enabled:
                    if data.model in disabled:
                        disabled.remove(data.model)
                else:
                    if data.model not in disabled:
                        disabled.append(data.model)
                p["disabled_models"] = disabled
                save_providers(providers)
                return {"ok": True, "disabled_models": disabled}
    raise HTTPException(404, "未找到")


@app.post("/api/check/{name}/{model}")
async def manual_check(name: str, model: str, _=Depends(verify_admin)):

    for p in providers:
        if p["name"] == name:
            ak = (p.get("api_keys", [""])[0] if p.get("api_keys") else "")
            result = await check_model(p["base_url"], ak, model)
            k = f"{name}||{model}"
            result["checked_at"] = time.time()
            health_status[k] = result
            update_model_quality(k, result)
            await append_history({k: result})
            return result
    raise HTTPException(404, "未找到")


@app.post("/api/check/all")
async def check_all(_=Depends(verify_admin)):
    tasks = []
    for p in list(providers):
        _pool = _available_keys(p)
        _probe_key = _pool[0] if _pool else ""
        for m in get_enabled_models(p):
            tasks.append((p["name"], m, p["base_url"], _probe_key))
    results = await run_health_checks(tasks)
    health_status.update(results)
    await append_history(results)
    mark_full_check()
    return results


# ============================================================
# 预设模板（三层加载：远端热更新 → 内置兜底）
# ============================================================
# ★ 2026-09-28 封堵作者远程控制：不再拉作者预设（防 base_url 被改成偷 key 的地址）
# 原值: https://gitee.com/ywtc000/dongye/raw/master/presets.json
PRESET_REMOTE_URL = "http://127.0.0.1:9/presets-local.json"
# ★ 2026-09-28 封堵作者外链（原值: 作者飞书文档）
PRESET_DOC_URL = ""
PRESET_CACHE_TTL = 300

# 内置兜底预设（断网保底；平台变更时改远端 presets.json 热更新即可，无需重新打包）
BUILTIN_PRESET = {
    "version": "2026-07-20",
    "updated_at": "2026-07-20",
    "doc_url": PRESET_DOC_URL,
    "platforms": {
        "NVIDIA": {
            "base_url": "https://integrate.api.nvidia.com/v1",
            "free_only": True,
            "key_page_url": "https://build.nvidia.com/",
            "auth_hint": "需绑定手机号",
            "models_visible": [
                "deepseek-ai/deepseek-v4-flash",
                "deepseek-ai/deepseek-v4-pro",
                "minimaxai/minimax-m3",
                "mistralai/mistral-large-3-675b-instruct-2512",
                "mistralai/mistral-small-4-119b-2603",
                "nvidia/nemotron-3-super-120b-a12b",
                "nvidia/nemotron-3-ultra-550b-a55b",
                "qwen/qwen3.5-122b-a10b",
                "z-ai/glm-5.2",
                "meta/llama-3.2-11b-vision-instruct",
                "nvidia/nemotron-nano-12b-v2-vl",
                "nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
            ],
        },
        "SenseNova": {
            "base_url": "https://token.sensenova.cn/v1",
            "free_only": False,
            "key_page_url": "https://platform.sensenova.cn/console/keys",
            "auth_hint": "手机号注册登录即可",
            "models_visible": [
                "deepseek-v4-flash",
                "glm-5.2",
                "sensenova-6.7-flash-lite",
            ],
        },
        "魔搭": {
            "base_url": "https://api-inference.modelscope.cn/v1",
            "free_only": False,
            "key_page_url": "https://modelscope.cn/my/myaccesstoken",
            "auth_hint": "需绑定阿里云账号（支付宝实名）",
            "models_visible": [
                "Qwen/Qwen3.5-122B-A10B",
                "Qwen/Qwen3.5-397B-A17B",
                "deepseek-ai/DeepSeek-V4-Flash",
                "deepseek-ai/DeepSeek-V4-Pro",
                "OpenGVLab/InternVL3_5-241B-A28B",
                "Qwen/Qwen3-VL-8B-Thinking",
                "Qwen/Qwen3-VL-8B-Instruct",
                "PaddlePaddle/ERNIE-4.5-VL-28B-A3B-PT",
            ],
        },
    },
    "routers": {
        "256k": [
            "mistralai/mistral-large-3-675b-instruct-2512",
            "mistralai/mistral-small-4-119b-2603",
            "nvidia/nemotron-3-super-120b-a12b",
            "qwen/qwen3.5-122b-a10b",
            "stepfun/step-router-v1",
            "sensenova-6.7-flash-lite",
            "Qwen/Qwen3.5-122B-A10B",
            "Qwen/Qwen3.5-397B-A17B",
        ],
        "1m": [
            "deepseek-ai/deepseek-v4-pro",
            "minimaxai/minimax-m3",
            "nvidia/nemotron-3-ultra-550b-a55b",
            "z-ai/glm-5.2",
            "glm-5.2",
            "deepseek-ai/DeepSeek-V4-Pro",
            "deepseek-ai/deepseek-v4-flash",
            "deepseek-v4-flash",
            "deepseek-ai/DeepSeek-V4-Flash",
            "meituan/LongCat-2.0"
        ],
        "识图": [
            "sensenova-6.7-flash-lite",
            "mistralai/mistral-large-3-675b-instruct-2512",
            "mistralai/mistral-small-4-119b-2603",
            "meta/llama-3.2-11b-vision-instruct",
            "nvidia/nemotron-nano-12b-v2-vl",
            "nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
            "OpenGVLab/InternVL3_5-241B-A28B",
            "Qwen/Qwen3-VL-8B-Thinking",
            "Qwen/Qwen3-VL-8B-Instruct",
            "PaddlePaddle/ERNIE-4.5-VL-28B-A3B-PT",
        ],
    },
}

_preset_cache = {"data": None, "ts": 0.0}


async def load_preset(force_remote: bool = False) -> dict:
    """三层加载：远端热更新(优先) → 内置兜底。缓存 PRESET_CACHE_TTL 秒。"""
    now = time.time()
    if (not force_remote and _preset_cache["data"]
            and now - _preset_cache["ts"] < PRESET_CACHE_TTL):
        return _preset_cache["data"]
    if http_client:
        try:
            resp = await http_client.get(PRESET_REMOTE_URL, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and data.get("platforms"):
                    _preset_cache["data"] = data
                    _preset_cache["ts"] = now
                    return data
        except Exception:
            logger.warning("load_preset remote fetch failed, fallback to builtin")
    if not _preset_cache["data"]:
        _preset_cache["data"] = BUILTIN_PRESET
        _preset_cache["ts"] = now
    return _preset_cache["data"]


def has_image(body: dict) -> bool:
    """检测 messages 是否含 image_url（仅看最后一轮 user 消息，历史图片不算）"""
    msgs = body.get("messages", [])
    for i in range(len(msgs) - 1, -1, -1):
        msg = msgs[i]
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        return True
            return False  # 最后一轮 user 消息没有图，不再往前看
    return False


CN_HINT = "（请用简体中文回答）"

def _inject_cn_hint(body: dict):
    """将中文回复指令注入最后一条 user 消息，确保识图模型用中文回答。
    部分小模型会忽略 system prompt，直接塞进用户消息最可靠。"""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return
    for i in range(len(msgs) - 1, -1, -1):
        msg = msgs[i]
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str) and CN_HINT not in content:
                msg["content"] = content + "\n" + CN_HINT
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        t = part.get("text", "")
                        if CN_HINT not in t:
                            part["text"] = t + "\n" + CN_HINT
                        break
            break


# ============================================================
# 代理（客户端鉴权）
# ============================================================
async def _nonstream_via_stream(provider: dict, model: str, req_body: dict, used_key: str,
                                 timeout: float = 600.0):
    """内部流式聚合：用 stream=true 请求上游，读 SSE 聚合为标准非流式响应 dict。

    - 流式每个 chunk 重置读计时器 → 免疫上游总生成时长超时
    - 支持 delta.tool_calls 增量合并（按 index）→ agent 工具调用不丢
    - 支持非 SSE 降级：上游忽略 stream 直接回 JSON 时也能解析

    返回: (parsed_dict, usage_dict) ; 失败抛异常由调用方处理。
    """
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    body = copy.deepcopy(req_body)
    body["stream"] = True
    body.setdefault("stream_options", {"include_usage": True})
    headers = {
        "Authorization": f"Bearer {used_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage_obj: dict = {}
    finish_reason = None
    resp_model = model
    role = "assistant"

    # tool_calls 增量合并：index -> {id, type, function:{name, arguments}}
    tool_calls_accum: dict[int, dict] = {}
    # 非 SSE 降级缓冲（首行不是 data: 时尝试整包 JSON）
    plain_buf: list[str] = []

    req = http_client.build_request("POST", url, json=body, headers=headers)
    async with http_client.stream(
        "POST", url, json=body, headers=headers,
        timeout=httpx.Timeout(timeout, connect=15.0),
    ) as resp:
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"upstream {resp.status_code}", request=req, response=resp
            )
        saw_data = False
        async for line in resp.aiter_lines():
            if not line:
                continue
            if not line.startswith("data: "):
                if not saw_data:
                    plain_buf.append(line)
                continue
            saw_data = True
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if obj.get("usage"):
                usage_obj = obj["usage"]
            if isinstance(obj.get("model"), str):
                resp_model = model
            for ch in (obj.get("choices") or []):
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                delta = ch.get("delta") or {}
                if isinstance(delta.get("role"), str):
                    role = delta["role"]
                c = delta.get("content")
                if isinstance(c, str) and c:
                    content_parts.append(c)
                rc = delta.get("reasoning_content")
                if isinstance(rc, str) and rc:
                    reasoning_parts.append(rc)
                tc = delta.get("tool_calls")
                if isinstance(tc, list):
                    for call in tc:
                        if not isinstance(call, dict):
                            continue
                        idx = call.get("index", 0)
                        entry = tool_calls_accum.setdefault(idx, {
                            "id": "", "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if call.get("id"):
                            entry["id"] = call["id"]
                        if call.get("type"):
                            entry["type"] = call["type"]
                        fn = call.get("function") or {}
                        if fn.get("name"):
                            entry["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            entry["function"]["arguments"] += fn["arguments"]

    # 非 SSE 降级：上游直接返回了普通 JSON（忽略 stream=true）
    if not saw_data and plain_buf:
        raw = "".join(plain_buf).strip()
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
                return obj, (obj.get("usage") or {})
            except json.JSONDecodeError:
                pass

    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    message: dict = {"role": role, "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_calls_accum:
        message["tool_calls"] = [
            tool_calls_accum[i] for i in sorted(tool_calls_accum)
        ]
    if not content and not reasoning and not tool_calls_accum:
        raise RuntimeError("stream-aggregate returned empty content")

    parsed = {
        "id": f"chatcmpl-agg-{int(time.time()*1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resp_model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason or "stop",
        }],
        "usage": usage_obj or {},
    }
    return parsed, usage_obj


async def _try_stream_agg_first(candidates, body, is_router, prelude: str = ""):
    """方案 C+ 主路径：非流式请求【始终先试】内部流式聚合。

    语义沿用网关既有失败分类：
      - 429 冷却：跳过 provider
      - quota(429)：切 key 重试；全部 key 429 → 冷却 provider
      - auth(401/403)：拉黑坏 key + 切 key
      - request(400 等)：透传错误，整体 fallback 普通 POST（同一请求体 POST 可能可用）
      - billing(402)：冷却 provider 1800s，fallback 普通 POST
      - server(5xx)/连接/聚合异常：切 key 重试；仍失败 → 最后 fallback

    返回: JSONResponse（成功）/ None（整体失败，调用方应 fallback 普通 POST）
    """
    async def _build_response(agg_parsed):
        agg_parsed = merge_reasoning(agg_parsed)
        _s = json.dumps(agg_parsed, ensure_ascii=False)
        _s = restore_hermes_text(_s)
        agg_parsed = json.loads(_s)
        if prelude:
            try:
                _msg = agg_parsed["choices"][0]["message"]
                _c = _msg.get("content")
                if isinstance(_c, str) and _c:
                    _msg["content"] = f"{prelude.rstrip()}\n\n{_c}"
                elif isinstance(_c, str):
                    _msg["content"] = prelude.rstrip()
            except (KeyError, IndexError, TypeError):
                pass
        return JSONResponse(content=agg_parsed, status_code=200)

    for attempt in (2,) if is_router else (1,):
        if not any(not is_provider_429_cooling(p) for p, _ in candidates):
            return None   # 全部冷却 → fallback（普通 POST 侧也会判冷却，语义一致）
        for provider, model in candidates:
            if is_provider_429_cooling(provider):
                continue
            k = f"{provider['name']}||{model}"
            keys = _get_keys(provider)
            max_key_tries = max(1, len(keys))
            key_tried = 0
            used_key = None
            while key_tried < max_key_tries:
                key_tried += 1
                req_body = copy.deepcopy(body)
                req_body["model"] = MODEL_ALIASES.get(model, model)
                inject_provider_order(provider, req_body)
                used_key = select_key(provider)
                try:
                    agg_parsed, agg_usage = await _nonstream_via_stream(
                        provider, model, req_body, used_key)
                except httpx.HTTPStatusError as hse:
                    status = hse.response.status_code if hse.response is not None else 500
                    ftype = classify_failure(status)
                    detail = ""
                    try:
                        detail = (hse.response.text or "")[:300]
                    except Exception:
                        pass
                    logger.warning("upstream %d from %s (stream-agg): %s", status, provider["name"], detail)
                    append_call_log({
                        "time": time.strftime("%H:%M:%S"), "provider": provider["name"],
                        "model": model, "status": "fail", "tokens": 0,
                        "error": f"HTTP {status}",
                    })
                    if ftype == "request":
                        # 400 等请求错误：换 key 无用；POST 可能同样 400，但保底交给 fallback
                        return None
                    if ftype == "billing":
                        mark_provider_429(provider, PROVIDER_BILLING_COOLDOWN_SECONDS)
                        logger.info("provider %s 402 余额不足，冷却 %ds（fallback 普通 POST）", provider["name"], PROVIDER_BILLING_COOLDOWN_SECONDS)
                        return None
                    if ftype == "quota":
                        on_key_failure(provider, status, bad_key=used_key)
                        if key_tried < max_key_tries:
                            logger.info("provider %s 429 限流，换下一个 Key 重试（%d/%d）", provider["name"], key_tried, max_key_tries)
                            continue
                        mark_provider_429(provider)
                        logger.info("provider %s 全部 Key 均 429，冷却 %ds", provider["name"], PROVIDER_429_COOLDOWN_SECONDS)
                        break
                    record_fail(k)
                    on_key_failure(provider, status, bad_key=used_key)
                    if key_tried < max_key_tries:
                        logger.info("provider %s HTTP %d，换下一个 Key 重试（%d/%d）", provider["name"], status, key_tried, max_key_tries)
                        continue
                    break
                except httpx.RequestError as e:
                    logger.warning("forward error to %s (stream-agg): %s", provider["name"], e)
                    record_fail(k)
                    on_key_failure(provider, bad_key=used_key)
                    append_call_log({
                        "time": time.strftime("%H:%M:%S"), "provider": provider["name"],
                        "model": model, "status": "fail", "tokens": 0, "error": "连接失败",
                    })
                    if key_tried < max_key_tries:
                        logger.info("provider %s 连接失败，换下一个 Key 重试（%d/%d）", provider["name"], key_tried, max_key_tries)
                        continue
                    break
                except Exception as e:
                    logger.warning("stream-agg forward error to %s: %s: %s", provider["name"], type(e).__name__, e)
                    record_fail(k)
                    on_key_failure(provider, bad_key=used_key)
                    append_call_log({
                        "time": time.strftime("%H:%M:%S"), "provider": provider["name"],
                        "model": model, "status": "fail", "tokens": 0, "error": "流式聚合失败",
                    })
                    if key_tried < max_key_tries:
                        logger.info("provider %s 聚合异常，换下一个 Key 重试（%d/%d）", provider["name"], key_tried, max_key_tries)
                        continue
                    break
                # ------ 流式聚合成功 ------
                try:
                    _pt, _ct, _ch, _is_est = _usage_or_estimate(
                        agg_usage, req_body.get("messages"), _extract_out_text(agg_parsed))
                    _rec1 = {
                        "ts": time.time(), "model": model,
                        "provider": provider["name"],
                        "pt": _pt, "ct": _ct, "tt": _pt + _ct, "cache_pt": _ch,
                    }
                    _cl1 = {
                        "time": time.strftime("%H:%M:%S"), "provider": provider["name"],
                        "model": model, "status": "ok", "tokens": _pt + _ct,
                        "cached": _ch,
                    }
                    if _is_est:
                        _rec1["estimated"] = True
                        _cl1["estimated"] = True
                    await append_usage(_rec1)
                    append_call_log(_cl1)
                except Exception:
                    logger.exception("append_usage(stream-agg) failed")
                record_success(k)
                on_key_success(provider)
                return await _build_response(agg_parsed)
    return None


async def _stream_with_failover(candidates, body, is_router, prelude: str = ""):
    """流式转发，中断时自动切换下一个候选模型继续输出。prelude 为先输出给用户的提示文本。"""

    async def gen():
        accumulated = ""
        prefix_done = False
        max_attempts = 2 if is_router else 1
        last_request_err = None  # 400 类错误：全部候选失败后才透出给客户端

        if prelude:
            yield "data: " + json.dumps({"choices": [{"delta": {"content": prelude}, "index": 0}]}, ensure_ascii=False) + "\n\n"

        for attempt in range(max_attempts):
            if not any(not is_provider_429_cooling(p) for p, _ in candidates):
                yield "data: " + json.dumps({
                    "error": {"message": "上游限流冷却中，请稍后重试", "type": "rate_limit_error", "code": 429},
                }, ensure_ascii=False) + "\n\n"
                return
            for provider, model in candidates:
                if is_provider_429_cooling(provider):
                    logger.info("provider %s 429 冷却中，跳过", provider["name"])
                    continue
                k = f"{provider['name']}||{model}"

                # Key 级重试：任何 Key 侧错误（429/401/403/5xx/连接失败）都换下一个 Key
                # 重试当前请求，前端无感；全部 Key 试完才冷却 provider / 路由下一候选。
                # 402 是账号级问题（余额），换 Key 无用 → 直接长冷却 + 下一候选。
                keys = _get_keys(provider)
                max_key_tries = max(1, len(keys))
                key_tried = 0
                used_key = None
                resp = None
                while key_tried < max_key_tries:
                    key_tried += 1
                    req_body = copy.deepcopy(body)
                    req_body["model"] = MODEL_ALIASES.get(model, model)
                    inject_provider_order(provider, req_body)

                    if accumulated:
                        msgs = list(req_body.get("messages", []))
                        msgs.append({"role": "assistant", "content": accumulated})
                        msgs.append({"role": "user", "content": "请继续上面的回复，从中断处接着写。"})
                        req_body["messages"] = msgs

                    used_key = select_key(provider)
                    url = provider["base_url"].rstrip("/") + "/chat/completions"
                    headers = {
                        "Authorization": f"Bearer {used_key}",
                        "Content-Type": "application/json",
                    }

                    req = http_client.build_request("POST", url, json=req_body, headers=headers)
                    try:
                        resp = await http_client.send(req, stream=True)
                    except httpx.RequestError as e:
                        logger.warning("stream connect error to %s: %s", provider["name"], e)
                        record_fail(k)
                        on_key_failure(provider, bad_key=used_key)
                        append_call_log({
                            "time": time.strftime("%H:%M:%S"),
                            "provider": provider["name"],
                            "model": model,
                            "status": "fail",
                            "tokens": 0,
                            "error": "连接失败",
                        })
                        resp = None
                        if key_tried < max_key_tries:
                            logger.info("provider %s 连接失败，换下一个 Key 重试（%d/%d）", provider["name"], key_tried, max_key_tries)
                            continue
                        break

                    if resp.status_code != 200:
                        try:
                            await resp.aread()
                        except Exception:
                            pass
                        await resp.aclose()
                        ftype = classify_failure(resp.status_code)
                        if ftype == "request":
                            # 请求体问题：换 Key 无用，但别家上游可能兼容该请求
                            # → 不切 Key、不熔断，路由到下一候选；全部失败才把错误详情透出
                            logger.warning("upstream stream error %d from %s (请求参数错误，路由下一候选)", resp.status_code, provider["name"])
                            append_call_log({
                                "time": time.strftime("%H:%M:%S"),
                                "provider": provider["name"],
                                "model": model,
                                "status": "fail",
                                "tokens": 0,
                                "error": f"HTTP {resp.status_code} 请求参数错误",
                            })
                            last_request_err = f"upstream {resp.status_code}"
                            resp = None
                            break
                        if ftype == "billing":
                            # 402 余额/权限：账号级问题，换 Key 无用 → provider 长冷却，路由下一候选
                            logger.warning("upstream stream error %d from %s (余额/权限，冷却 %ds 后路由下一候选)", resp.status_code, provider["name"], PROVIDER_BILLING_COOLDOWN_SECONDS)
                            mark_provider_429(provider, PROVIDER_BILLING_COOLDOWN_SECONDS)
                            append_call_log({
                                "time": time.strftime("%H:%M:%S"),
                                "provider": provider["name"],
                                "model": model,
                                "status": "fail",
                                "tokens": 0,
                                "error": f"HTTP {resp.status_code} 余额不足",
                            })
                            resp = None
                            break
                        logger.warning("upstream stream error %d from %s", resp.status_code, provider["name"])
                        if ftype == "quota":
                            # 限流：先切 Key 换下一个重试（无感切换），全部 Key 429 才冷却 provider
                            on_key_failure(provider, resp.status_code, bad_key=used_key)
                            append_call_log({
                                "time": time.strftime("%H:%M:%S"),
                                "provider": provider["name"],
                                "model": model,
                                "status": "fail",
                                "tokens": 0,
                                "error": f"HTTP {resp.status_code}",
                            })
                            if key_tried < max_key_tries:
                                logger.info("provider %s 429 限流，换下一个 Key 重试（%d/%d）", provider["name"], key_tried, max_key_tries)
                                continue
                            mark_provider_429(provider)
                            logger.info("provider %s 全部 %d 个 Key 均 429，冷却 %ds", provider["name"], max_key_tries, PROVIDER_429_COOLDOWN_SECONDS)
                            break
                        # 认证失败(401/403 拉黑坏Key) / 服务故障(5xx)：换 Key 重试当前请求
                        record_fail(k)
                        on_key_failure(provider, resp.status_code, bad_key=used_key)
                        append_call_log({
                            "time": time.strftime("%H:%M:%S"),
                            "provider": provider["name"],
                            "model": model,
                            "status": "fail",
                            "tokens": 0,
                            "error": f"HTTP {resp.status_code}",
                        })
                        resp = None
                        if key_tried < max_key_tries:
                            logger.info("provider %s HTTP 错误，换下一个 Key 重试（%d/%d）", provider["name"], key_tried, max_key_tries)
                            continue
                        break
                    else:
                        break  # 200 成功

                if resp is None or resp.status_code != 200:
                    continue

                usage_obj = None
                stream_ok = True

                try:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if not line.startswith("data: "):
                            yield line + "\n"
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            yield "data: [DONE]\n\n"
                            break
                        try:
                            obj = json.loads(data_str)
                            if obj.get("usage"):
                                usage_obj = obj["usage"]
                            if "model" in obj and isinstance(obj["model"], str):
                                obj["model"] = model
                            choices = obj.get("choices") or []
                            if choices:
                                delta = choices[0].get("delta") or {}
                                c = delta.get("content")
                                if isinstance(c, str):
                                    accumulated += c
                                rc = delta.get("reasoning_content")
                                if isinstance(rc, str):
                                    accumulated += rc
                            if not prefix_done:
                                choices2 = obj.get("choices") or []
                                if choices2:
                                    delta2 = choices2[0].get("delta") or {}
                                    c2 = delta2.get("content")
                                    if isinstance(c2, str) and c2:
                                        delta2["content"] = c2
                                        prefix_done = True
                            out = json.dumps(obj, ensure_ascii=False)
                            out = restore_hermes_text(out)
                            yield "data: " + out + "\n\n"
                        except json.JSONDecodeError:
                            yield line + "\n"
                    record_success(k)
                    on_key_success(provider)
                    try:
                        pt, ct, ch, _is_est = _usage_or_estimate(
                            usage_obj, req_body.get("messages"), accumulated)
                        _rec2 = {
                            "ts": time.time(), "model": model,
                            "provider": provider["name"],
                            "pt": pt, "ct": ct, "tt": pt + ct, "cache_pt": ch,
                        }
                        _cl2 = {
                            "time": time.strftime("%H:%M:%S"),
                            "provider": provider["name"],
                            "model": model,
                            "status": "ok",
                            "tokens": pt + ct,
                            "cached": ch,
                        }
                        if _is_est:
                            _rec2["estimated"] = True
                            _cl2["estimated"] = True
                        await append_usage(_rec2)
                        append_call_log(_cl2)
                    except Exception:
                        logger.exception("append_usage(stream) failed")
                    return
                except Exception:
                    stream_ok = False
                    logger.exception("stream interrupted from %s, switching", provider["name"])
                    record_fail(k)
                    on_key_failure(provider, bad_key=used_key)
                    append_call_log({
                        "time": time.strftime("%H:%M:%S"),
                        "provider": provider["name"],
                        "model": model,
                        "status": "fail",
                        "tokens": 0,
                        "error": "流中断",
                    })
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
                    continue
                finally:
                    if stream_ok:
                        try:
                            await resp.aclose()
                        except Exception:
                            pass

        err_note = f"（{last_request_err}）" if last_request_err else ""
        yield "data: " + json.dumps({"choices": [{"delta": {"content": f"\n\n⚠️ 所有模型均失败{err_note}，回复中断。"}, "index": 0}]}, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.api_route("/v1/chat/completions", methods=["POST"], dependencies=[Depends(verify_client)])
async def proxy_chat(request: Request, force: bool = False):
    body = await request.json()
    body = compress_hermes(body)
    body = ensure_lang_reply(body)
    # 清洗历史消息：移除上游拒绝的空 tool_calls 数组（如 messages[558].tool_calls: []）
    if isinstance(body.get("messages"), list):
        body["messages"] = sanitize_messages(body["messages"])
    requested_model = body.get("model")
    # 按模型钳制 max_tokens（部分上游对超限值直接 400，如 mimo-v2.6-flash >131072）
    clamp_max_tokens(body, requested_model or "")

    # 识图辅助：含图片且目标非识图组/识图模型 → 直接转交识图路由组
    vision_cfg = app_config.get("vision_assist", {})
    vision_enabled = vision_cfg.get("enabled", False) if isinstance(vision_cfg, dict) else False
    is_vision_request = (requested_model == "识图") or bool(requested_model and is_vision_model(requested_model))
    vision_prelude = ""
    if vision_enabled and has_image(body) and not is_vision_request:
        if "识图" in ROUTERS:
            requested_model = "识图"
            vision_prelude = "🖼️ 已切换到视觉模型回复…\n\n"
            # 识图模型 max_tokens 上限较低（部分仅 32768），避免客户端传的百万级值导致 upstream 400
            for key in ("max_tokens", "max_completion_tokens"):
                if body.get(key, 0) > 16384:
                    body[key] = 16384
            # 部分识图模型忽略 system prompt，把中文指令直接注入用户消息末尾
            _inject_cn_hint(body)
        else:
            raise HTTPException(503, "识图辅助已开启，但未配置识图路由组，无法处理图片。")

    candidates = pick_available_models(requested_model, force=force)
    if not candidates:
        raise HTTPException(503, f"无可用的模型: {requested_model or '任意'}")

    # 按候选模型的真实模型名钳制 max_tokens（路由名如 "1m" 查不到上限，
    # 必须在 candidates 确定后按实际模型名再钳一次）
    for _prov, _mdl in candidates:
        clamp_max_tokens(body, _mdl)

    is_router = requested_model in ROUTERS
    stream = body.get("stream", False)
    last_err = None

    if stream:
        return await _stream_with_failover(candidates, body, is_router, prelude=vision_prelude)

    # 方案 C+：非流式请求【始终先试】内部流式聚合——
    # 流式按 chunk 重置读计时器，彻底免疫上游长输出超时（慢速上游可达 130s+）。
    # 整体失败时 fallback 到下方原有普通 POST 逻辑（保留 timeout=120/换 key/冷却/错误透传）。
    try:
        _agg_first = await _try_stream_agg_first(candidates, body, is_router, vision_prelude)
        if _agg_first is not None:
            return _agg_first
        logger.info("stream-agg 主路径未成功（返回 None），fallback 普通 POST")
    except Exception as _agg_err:
        logger.warning("stream-agg 主路径异常，fallback 普通 POST: %s: %s", type(_agg_err).__name__, _agg_err)

    if not force and not any(not is_provider_429_cooling(p) for p, _ in candidates):
        raise HTTPException(429, "上游限流冷却中，请稍后重试")

    for attempt in (2,) if is_router else (1,):
        for provider, model in candidates:
            if is_provider_429_cooling(provider):
                logger.info("provider %s 429 冷却中，跳过", provider["name"])
                continue
            k = f"{provider['name']}||{model}"

            # Key 级重试：任何 Key 侧错误（429/401/403/5xx/连接失败）都换下一个 Key
            # 重试当前请求，前端无感；全部 Key 试完才冷却 provider / 路由下一候选。
            # 402 是账号级问题（余额），换 Key 无用 → 直接长冷却 + 下一候选。
            keys = _get_keys(provider)
            max_key_tries = max(1, len(keys))
            key_tried = 0
            used_key = None
            while key_tried < max_key_tries:
                key_tried += 1
                req_body = copy.deepcopy(body)
                req_body["model"] = MODEL_ALIASES.get(model, model)
                inject_provider_order(provider, req_body)
                used_key = select_key(provider)
                url = provider["base_url"].rstrip("/") + "/chat/completions"
                headers = {
                    "Authorization": f"Bearer {used_key}",
                    "Content-Type": "application/json",
                }

                try:
                    resp = await http_client.post(url, json=req_body, headers=headers, timeout=120)
                    if resp.status_code >= 400:
                        ftype = classify_failure(resp.status_code)
                        err_detail = resp.text[:300]
                        logger.warning("upstream %d from %s: %s", resp.status_code, provider["name"], err_detail)
                        if ftype == "request":
                            # 请求体问题：换 Key 无用，不切 Key、不熔断，透传错误给客户端
                            last_err = f"upstream {resp.status_code}: {err_detail}"
                            append_call_log({
                                "time": time.strftime("%H:%M:%S"),
                                "provider": provider["name"],
                                "model": model,
                                "status": "fail",
                                "tokens": 0,
                                "error": f"HTTP {resp.status_code} 请求参数错误",
                            })
                            break
                        if ftype == "billing":
                            # 402 余额/权限：账号级问题，换 Key 无用 → provider 长冷却，路由下一候选
                            mark_provider_429(provider, PROVIDER_BILLING_COOLDOWN_SECONDS)
                            logger.info("provider %s 402 余额不足，冷却 %ds 后路由下一候选", provider["name"], PROVIDER_BILLING_COOLDOWN_SECONDS)
                            last_err = f"upstream {resp.status_code}"
                            append_call_log({
                                "time": time.strftime("%H:%M:%S"),
                                "provider": provider["name"],
                                "model": model,
                                "status": "fail",
                                "tokens": 0,
                                "error": f"HTTP {resp.status_code} 余额不足",
                            })
                            break
                        if ftype == "quota":
                            # 限流：先切 Key 换下一个重试（无感切换），全部 Key 429 才冷却 provider
                            on_key_failure(provider, resp.status_code, bad_key=used_key)
                            last_err = f"upstream {resp.status_code}"
                            append_call_log({
                                "time": time.strftime("%H:%M:%S"),
                                "provider": provider["name"],
                                "model": model,
                                "status": "fail",
                                "tokens": 0,
                                "error": f"HTTP {resp.status_code}",
                            })
                            if key_tried < max_key_tries:
                                logger.info("provider %s 429 限流，换下一个 Key 重试（%d/%d）", provider["name"], key_tried, max_key_tries)
                                continue
                            mark_provider_429(provider)
                            logger.info("provider %s 全部 %d 个 Key 均 429，冷却 %ds", provider["name"], max_key_tries, PROVIDER_429_COOLDOWN_SECONDS)
                            break
                        # 认证失败(401/403 拉黑坏Key) / 服务故障(5xx)：换 Key 重试当前请求
                        record_fail(k)
                        on_key_failure(provider, resp.status_code, bad_key=used_key)
                        last_err = f"upstream {resp.status_code}"
                        append_call_log({
                            "time": time.strftime("%H:%M:%S"),
                            "provider": provider["name"],
                            "model": model,
                            "status": "fail",
                            "tokens": 0,
                            "error": f"HTTP {resp.status_code}",
                        })
                        if key_tried < max_key_tries:
                            logger.info("provider %s HTTP %d，换下一个 Key 重试（%d/%d）", provider["name"], resp.status_code, key_tried, max_key_tries)
                            continue
                        break
                    try:
                        parsed = json.loads(resp.text)
                        parsed = merge_reasoning(parsed)
                        parsed_str = json.dumps(parsed, ensure_ascii=False)
                        parsed_str = restore_hermes_text(parsed_str)
                        parsed = json.loads(parsed_str)
                        record_success(k)
                        on_key_success(provider)
                        parsed["model"] = model
                        try:
                            pt, ct, ch, _is_est = _usage_or_estimate(
                                parsed.get("usage"), req_body.get("messages"),
                                _extract_out_text(parsed))
                            _rec3 = {
                                "ts": time.time(), "model": model,
                                "provider": provider["name"],
                                "pt": pt, "ct": ct, "tt": pt + ct, "cache_pt": ch,
                            }
                            _cl3 = {
                                "time": time.strftime("%H:%M:%S"),
                                "provider": provider["name"],
                                "model": model,
                                "status": "ok",
                                "tokens": pt + ct,
                                "cached": ch,
                            }
                            if _is_est:
                                _rec3["estimated"] = True
                                _cl3["estimated"] = True
                            await append_usage(_rec3)
                            # 调用日志记录
                            append_call_log(_cl3)
                        except Exception:
                            logger.exception("append_usage(non-stream) failed")
                        try:
                            msg = parsed["choices"][0]["message"]
                            c = msg.get("content")
                            prefix_parts = []
                            if vision_prelude:
                                prefix_parts.append(vision_prelude.rstrip())
                            prefix = "\n\n".join(prefix_parts)
                            if isinstance(c, str) and c:
                                msg["content"] = f"{prefix}\n\n{c}" if prefix else c
                            elif isinstance(c, str):
                                msg["content"] = prefix
                        except (KeyError, IndexError, TypeError):
                            pass
                        return JSONResponse(content=parsed, status_code=resp.status_code)
                    except json.JSONDecodeError:
                        logger.warning("upstream non-json from %s: %s", provider["name"], resp.text[:200])
                        record_fail(k)
                        on_key_failure(provider, resp.status_code, bad_key=used_key)
                        last_err = f"upstream non-json ({resp.status_code})"
                        append_call_log({
                            "time": time.strftime("%H:%M:%S"),
                            "provider": provider["name"],
                            "model": model,
                            "status": "fail",
                            "tokens": 0,
                            "error": "响应格式错误",
                        })
                        if key_tried < max_key_tries:
                            logger.info("provider %s 响应非 JSON，换下一个 Key 重试（%d/%d）", provider["name"], key_tried, max_key_tries)
                            continue
                        break
                except httpx.RequestError as e:
                    # 方案 C：读超时 ≠ Key 故障。上游生成耗时长（如长输出 >120s）会触发
                    # httpx.ReadTimeout（str 为空），此时换 Key 毫无意义（每把都要等满超时）。
                    # 改用流式向同一上游重发一次并聚合——流式按 chunk 重置读计时器，不受总时长限制。
                    is_timeout = isinstance(e, httpx.ReadTimeout)
                    if is_timeout:
                        try:
                            logger.info("provider %s 读超时（%s），改用流式聚合同一上游重试", provider["name"], type(e).__name__)
                            agg_parsed, agg_usage = await _nonstream_via_stream(provider, model, req_body, used_key)
                            agg_parsed = merge_reasoning(agg_parsed)
                            _s = json.dumps(agg_parsed, ensure_ascii=False)
                            _s = restore_hermes_text(_s)
                            agg_parsed = json.loads(_s)
                            agg_parsed["model"] = model
                            record_success(k)
                            on_key_success(provider)
                            try:
                                _pt, _ct, _ch, _is_est = _usage_or_estimate(
                                    agg_usage, req_body.get("messages"),
                                    _extract_out_text(agg_parsed))
                                _rec4 = {
                                    "ts": time.time(), "model": model,
                                    "provider": provider["name"],
                                    "pt": _pt, "ct": _ct, "tt": _pt + _ct, "cache_pt": _ch,
                                }
                                _cl4 = {
                                    "time": time.strftime("%H:%M:%S"),
                                    "provider": provider["name"],
                                    "model": model,
                                    "status": "ok",
                                    "tokens": _pt + _ct,
                                    "cached": _ch,
                                }
                                if _is_est:
                                    _rec4["estimated"] = True
                                    _cl4["estimated"] = True
                                await append_usage(_rec4)
                                append_call_log(_cl4)
                            except Exception:
                                logger.exception("append_usage(stream-agg) failed")
                            try:
                                _msg = agg_parsed["choices"][0]["message"]
                                _c = _msg.get("content")
                                if vision_prelude and isinstance(_c, str) and _c:
                                    _msg["content"] = f"{vision_prelude.rstrip()}\n\n{_c}"
                                elif vision_prelude and isinstance(_c, str):
                                    _msg["content"] = vision_prelude.rstrip()
                            except (KeyError, IndexError, TypeError):
                                pass
                            logger.info("provider %s 流式聚合成功（非流式路径兜底）", provider["name"])
                            return JSONResponse(content=agg_parsed, status_code=200)
                        except Exception as agg_err:
                            logger.warning("provider %s 流式聚合兜底也失败: %s: %s", provider["name"], type(agg_err).__name__, agg_err)
                            # 聚合失败：不再换 Key（超时不是 Key 问题），直接路由下一候选
                            last_err = f"timeout+stream-agg-failed: {type(agg_err).__name__}"
                            append_call_log({
                                "time": time.strftime("%H:%M:%S"),
                                "provider": provider["name"],
                                "model": model,
                                "status": "fail",
                                "tokens": 0,
                                "error": "读超时(聚合兜底失败)",
                            })
                            break
                    logger.warning("forward error to %s: %s", provider["name"], e)
                    record_fail(k)
                    on_key_failure(provider, bad_key=used_key)
                    last_err = str(e)
                    append_call_log({
                        "time": time.strftime("%H:%M:%S"),
                        "provider": provider["name"],
                        "model": model,
                        "status": "fail",
                        "tokens": 0,
                        "error": "连接失败",
                    })
                    if key_tried < max_key_tries:
                        logger.info("provider %s 连接失败，换下一个 Key 重试（%d/%d）", provider["name"], key_tried, max_key_tries)
                        continue
                    break
                except Exception as e:
                    logger.exception("unexpected forward error to %s", provider["name"])
                    record_fail(k)
                    on_key_failure(provider, bad_key=used_key)
                    last_err = str(e)
                    append_call_log({
                        "time": time.strftime("%H:%M:%S"),
                        "provider": provider["name"],
                        "model": model,
                        "status": "fail",
                        "tokens": 0,
                        "error": "未知错误",
                    })
                    break

    # 请求参数错误（400 等）：透传上游状态码与错误详情，方便客户端修正请求
    if isinstance(last_err, str) and last_err.startswith("upstream 4"):
        try:
            code = int(last_err.split()[1].rstrip(":"))
            return JSONResponse(
                content={"error": {"message": last_err, "type": "invalid_request_error", "code": code}},
                status_code=code,
            )
        except Exception:
            pass
    raise HTTPException(502, f"所有候选模型均失败: {last_err}")


_models_cache = {"ts": 0, "data": None}
MODELS_CACHE_TTL = 30


# ============================================================
# 调用日志
# ============================================================
@app.get("/api/call-log")
async def get_call_log(hours: int = 0, _=Depends(verify_admin)):
    if hours > 0 and CALL_LOG_FILE.exists():
        cutoff = time.time() - hours * 3600
        entries = []
        try:
            with open(CALL_LOG_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line.strip())
                        if rec.get("ts", 0) >= cutoff:
                            entries.append(rec)
                    except Exception:
                        pass
        except Exception:
            pass
        # 按时间戳倒序，取最近的 200 条
        entries.sort(key=lambda x: x.get("ts", 0), reverse=True)
        return entries[:200]
    return list(call_log)


# ============================================================
# 配置 API（API Key、端口等）
# ============================================================
@app.put("/api/config")
async def update_config(request: Request, _=Depends(verify_admin)):
    """更新 local_api_key / usage_retention_days 等配置项"""
    global LOCAL_API_KEY
    body = await request.json()
    if "local_api_key" in body:
        new_key = body["local_api_key"].strip()
        if new_key:
            app_config["local_api_key"] = new_key
            LOCAL_API_KEY = new_key
            save_config()
            return {"ok": True, "key": new_key}
        else:
            raise HTTPException(400, "API Key 不能为空")
    if "usage_retention_days" in body:
        try:
            days = int(body["usage_retention_days"])
        except (TypeError, ValueError):
            raise HTTPException(400, "保留天数必须是数字")
        if days < 1 or days > 7200:
            raise HTTPException(400, "保留天数范围: 1-7200")
        app_config["usage_retention_days"] = days
        save_config()
        # 立即执行一次清理，应用新保留策略
        try:
            removed = _cleanup_usage_sync()
            if removed:
                logger.info("usage cleanup after retention change: removed %d records", removed)
        except Exception:
            pass
        return {"ok": True, "usage_retention_days": days}
    return {"ok": False, "error": "无可更新的字段"}


@app.get("/api/config/usage-retention")
async def get_usage_retention(_=Depends(verify_admin)):
    """获取当前消耗数据保留天数配置"""
    return {"usage_retention_days": get_usage_retention_days()}


@app.put("/api/port")
async def update_port(request: Request, _=Depends(verify_admin)):
    """更新端口并触发重启"""
    body = await request.json()
    new_port = int(body.get("port", 8000))
    if new_port < 1024 or new_port > 65535:
        raise HTTPException(400, "端口号范围: 1024-65535")
    app_config["port"] = new_port
    save_config()
    # 延迟重启，让 API 先返回
    def restart():
        time.sleep(0.5)
        if getattr(sys, 'frozen', False):
            subprocess.Popen([sys.executable], close_fds=True,
                           creationflags=0x00000008 if sys.platform == "win32" else 0)
        else:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve())], close_fds=True)
        os._exit(0)
    threading.Thread(target=restart, daemon=True).start()
    return {"ok": True, "port": new_port, "message": "端口已保存，程序即将重启"}


# ============================================================
# 开机自启动
# ============================================================
STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ModelGateway"


@app.get("/api/autostart")
async def get_autostart(_=Depends(verify_admin)):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0,
                            winreg.KEY_READ)
        winreg.QueryValueEx(key, VALUE_NAME)
        winreg.CloseKey(key)
        return {"enabled": True}
    except FileNotFoundError:
        return {"enabled": False}
    except Exception:
        return {"enabled": False}


@app.post("/api/autostart")
async def set_autostart(request: Request, _=Depends(verify_admin)):
    if winreg is None:
        return {"ok": False, "error": "不支持 Linux/WSL"}
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0,
                            winreg.KEY_SET_VALUE)
        if enabled:
            exe_path = sys.executable
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ,
                            f'"{exe_path}"')
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return {"ok": True, "enabled": enabled}
    except Exception as e:
        raise HTTPException(500, f"操作失败: {e}")


@app.api_route("/v1/models", methods=["GET"], dependencies=[Depends(verify_client)])
async def proxy_models():
    now = time.time()
    if _models_cache["data"] and now - _models_cache["ts"] < MODELS_CACHE_TTL:
        return _models_cache["data"]
    models_list = []

    # 自定义路由组作为可输出的模型
    for router_name in ROUTERS:
        models_list.append({
            "id": router_name,
            "object": "model",
            "owned_by": "Router",
            "available": True,
        })

    for p in providers:
        disabled = set(p.get("disabled_models", []))
        for m in p.get("models", []):
            if m in disabled:
                continue
            k = f"{p['name']}||{m}"
            st = health_status.get(k, {}).get("status")
            # 三态：unknown/None -> True（乐观），ok -> True，fail/error -> False
            available = st in (None, "unknown", "ok")
            ctx_len = get_context_length(m)
            models_list.append({
                "id": f"{p['name']}-{m}",
                "object": "model",
                "owned_by": p["name"],
                "available": available,
                "context_length": ctx_len,
                "max_position_embeddings": ctx_len,
                "max_model_len": ctx_len,
            })
    result = {"object": "list", "data": models_list}
    _models_cache["data"] = result
    _models_cache["ts"] = now
    return result


if __name__ == "__main__":
    import uvicorn
    import webview
    import time
    from PIL import Image, ImageDraw
    import pystray
    import msvcrt

    # ---- 清理上次更新的残留文件 ----
    _cleanup_old_exe()

    # ---- 读取端口配置 ----
    cfg = load_config()
    desired_port = cfg.get("port", 8000)

    # ---- 端口工具函数 ----
    def port_in_use(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0

    def kill_old_instance(port: int):
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.split()[-1]
                    if pid.isdigit():
                        subprocess.run(
                            ["taskkill", "/PID", pid, "/F"],
                            capture_output=True, text=True, timeout=10,
                        )
                        return True
        except Exception:
            pass
        return False

    def find_available_port(start: int, max_try: int = 100) -> int:
        for p in range(start, start + max_try):
            if not port_in_use(p):
                return p
        return start  # fallback

    # ---- 单实例限制（文件锁要最先，避免 config 被污染） ----
    AUTO_KILL = os.environ.get("GATEWAY_AUTO_KILL") == "1"

    # 如果是测试模式自动杀旧实例，先杀再拿锁
    if AUTO_KILL and port_in_use(desired_port):
        kill_old_instance(desired_port)
        time.sleep(1.5)

    LOCK_FILE = str(DATA_DIR / ".gateway.lock")
    try:
        _lock_fd = open(LOCK_FILE, "w")
        msvcrt.locking(_lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, IOError):
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, "网关客户端已在运行中，请勿重复启动。", "提示", 0x30
        )
        sys.exit(0)

    # 自动找可用端口（锁拿到了才能安全改 config）
    actual_port = find_available_port(desired_port)
    if actual_port != desired_port:
        cfg["port"] = actual_port
        atomic_write(CONFIG_FILE, json.dumps(cfg, indent=2))
        if not AUTO_KILL:
            logger.info("端口 %d 被占用，自动使用 %d", desired_port, actual_port)

    # ---- WebView2 环境检测（Win7 等旧系统自动安装） ----
    _webview2_ok = False
    try:
        import webview.platforms.edgechromium
        _webview2_ok = True
    except Exception:
        pass
    if not _webview2_ok:
        setup_exe = APP_DIR / "MicrosoftEdgeWebview2Setup.exe"
        if setup_exe.exists():
            logger.info("WebView2 未安装，开始静默安装...")
            try:
                subprocess.run(
                    [str(setup_exe), "/silent", "/install"],
                    capture_output=True, timeout=120,
                )
                logger.info("WebView2 安装完成")
            except Exception:
                logger.warning("WebView2 安装失败，尝试使用系统默认浏览器")

    # ---- 生成托盘图标 ----
    def create_tray_icon():
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(30, 144, 255))
        draw.polygon([(22, 20), (44, 32), (22, 44)], fill="white")
        return img

    state = {"window": None, "quitting": False}

    def on_show(icon, item):
        w = state["window"]
        if w:
            w.show()

    def on_quit(icon, item):
        state["quitting"] = True
        icon.stop()
        w = state["window"]
        if w:
            w.destroy()

    tray_icon = pystray.Icon(
        "model-gateway",
        create_tray_icon(),
        "无限额度监控网关",
        menu=pystray.Menu(
            pystray.MenuItem("显示窗口", on_show, default=True),
            pystray.MenuItem("退出", on_quit),
        ),
    )

    # ---- FastAPI 服务器（daemon 线程） ----
    def start_server(port: int):
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

    t = threading.Thread(target=start_server, args=(actual_port,), daemon=True)
    t.start()

    # ---- 轮询等待服务就绪（每 100ms 检查，最多等 5 秒） ----
    for _ in range(50):
        time.sleep(0.1)
        if port_in_use(actual_port):
            break

    # ---- 创建窗口并直接加载页面 ----
    url = f'http://127.0.0.1:{actual_port}/'
    window = webview.create_window(
        '无限额度监控网关', url, width=1200, height=800
    )
    state["window"] = window

    def on_closing():
        if state["quitting"]:
            return
        window.hide()
        return False

    window.events.closing += on_closing

    # ---- 启动系统托盘 ----
    threading.Thread(target=tray_icon.run, daemon=True).start()

    # ---- 启动 webview ----
    webview.start()

