"""开发期配置管理：让开发者在一个页面上改 Key / Provider / 阈值 / YAML。

★ 首先说清楚：**这不是用户功能** ★
它存在的唯一理由是省掉"改 .env → 重启后端 → 再试"的循环。
访问控制有三道门（见 ``app/api/v1/dev.py``）：整条路由只在 ``ENV=development``
注册、需要 ``ADMIN_TOKEN``、没有 Token 时只允许本机。生产环境下这些端点**根本不存在**。

★ 几条刻意的边界（不是漏做）★

1. **只允许改白名单里的环境变量**（:data:`ENV_FIELDS`）。白名单之外一律拒绝 ——
   "把整个 .env 交给浏览器"比"少改一个键"危险得多。**刻意不包含**：
   ``DATABASE_URL`` / ``TEST_DATABASE_URL``（改它要重启进程，且面板的读写本身就用着库）、
   ``SESSION_SECRET``（会话签名密钥，工具界面不该碰）。
2. **Secret 只写不读**：读取时一律打码（``sk-***abcd``），界面拿不到明文，
   也不会有"把 Key 复制到浏览器"的路径。
3. **先校验再落盘，失败回滚**：YAML 先写再让 Pydantic 校验，不通过就把原内容写回去；
   环境变量同理，并且会尝试真正构造一次 ``Settings()`` 验证。
4. **热生效**：改完清空配置缓存，并把值写进 ``os.environ``
   （``pydantic-settings`` 里环境变量优先于 ``.env``，不写进去就会出现
   "面板显示改了、实际没生效"的假象）。

路径可注入（``env_path`` / ``config_dir``），因此这一层可以完全用临时目录单测。
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from app.core.config import (
    LimitsConfig,
    PricingConfig,
    ScoringConfig,
    SeedConfig,
    Settings,
    TtlConfig,
    clear_config_cache,
    get_limits_config,
    get_pricing_config,
    get_scoring_config,
    get_seed_config,
    get_settings,
    get_ttl_config,
)
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.paths import config_dir as default_config_dir
from app.core.paths import env_file

__all__ = [
    "CONFIG_LOADERS",
    "CONFIG_MODELS",
    "ENV_FIELDS",
    "EnvField",
    "config_file_names",
    "effective_snapshot",
    "env_payload",
    "mask_secret",
    "read_config_file",
    "update_env",
    "write_config_file",
]

log = get_logger("dev_config")

_KEY_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=")
_MAX_VALUE_CHARS = 500


@dataclass(frozen=True, slots=True)
class EnvField:
    """一个可编辑的环境变量。

    ``kind`` 决定界面控件与校验方式：
    ``secret``（只写不读）/ ``str`` / ``int`` / ``bool`` / ``choice``。
    """

    key: str
    label: str
    group: str
    kind: str = "str"
    hint: str = ""
    choices: tuple[str, ...] = ()


#: 可编辑的环境变量白名单。**加键之前先想清楚：它会不会让面板变成后门。**
#: 刻意排除（并在测试里钉住）：
#:   ``ENV``——改成 production 会当场关掉面板自身，还会切到生产校验规则；
#:   ``DATABASE_URL`` / ``TEST_DATABASE_URL``——改它必须重启进程，而且面板自己就用着库；
#:   ``SESSION_SECRET``——会话签名密钥，工具界面不该碰。
ENV_FIELDS: tuple[EnvField, ...] = (
    # ── LLM ──
    EnvField("LLM_PROVIDER", "LLM Provider", "LLM", "choice", "无 Key 时自动降级为规则引擎", ("deepseek", "openai", "anthropic", "disabled")),
    EnvField("DEEPSEEK_API_KEY", "DeepSeek API Key", "LLM", "secret", "填上才会真正调用模型"),
    EnvField("OPENAI_API_KEY", "OpenAI API Key", "LLM", "secret", "保留档位，当前代码未使用"),
    EnvField("ANTHROPIC_API_KEY", "Anthropic API Key", "LLM", "secret", "保留档位，当前代码未使用"),
    EnvField("LLM_BASE_URL", "LLM Base URL", "LLM", "str", "OpenAI 兼容端点"),
    EnvField("LLM_TIER_FAST_MODEL", "fast 档模型", "LLM", "str", "意图补全与叙事用它"),
    EnvField("LLM_TIER_STRONG_MODEL", "strong 档模型", "LLM", "str", "当前规划链路未使用 strong 档"),
    EnvField("LLM_TIMEOUT_S", "LLM 超时（秒）", "LLM", "int", "超时即降级到规则引擎"),
    EnvField("LLM_MAX_RETRIES", "LLM 重试次数", "LLM", "int", "schema 不符时重试次数"),
    # ── 搜索 ──
    EnvField("SEARCH_PROVIDER", "搜索 Provider", "搜索", "choice", "无 Key 时为 seed_only（只读本地知识库）", ("auto", "tavily", "seed_only")),
    EnvField("TAVILY_API_KEY", "Tavily API Key", "搜索", "secret"),
    EnvField("SERPER_API_KEY", "Serper API Key", "搜索", "secret"),
    EnvField("BING_SEARCH_API_KEY", "Bing Search API Key", "搜索", "secret"),
    EnvField("ENABLE_LOCAL_FETCH", "允许本地抓取", "搜索", "bool", "true 时才会直接抓网页"),
    EnvField("SEARCH_MAX_QUERIES_PER_PLAN", "单次规划最大查询数", "搜索", "int", "硬上限，也是成本熔断依据"),
    # ── 地图 ──
    EnvField("MAP_PROVIDER", "地图 Provider", "地图", "choice", "无 Key 时降级 osrm → haversine", ("auto", "amap", "osrm", "haversine")),
    EnvField("AMAP_WEB_KEY", "高德 Web Key", "地图", "secret"),
    EnvField("OSRM_BASE_URL", "OSRM Base URL", "地图", "str"),
    EnvField("NOMINATIM_USER_AGENT", "Nominatim User-Agent", "地图", "str", "公共实例要求可识别的 UA"),
    EnvField("MAP_MAX_CALLS_PER_PLAN", "单次规划最大地图调用", "地图", "int", "按次数熔断，与单价是否校准无关"),
    EnvField("WEATHER_PROVIDER", "天气 Provider", "地图", "choice", "open-meteo 免 Key", ("open_meteo", "disabled")),
    # ── 成本与限流 ──
    EnvField("PLAN_COST_CIRCUIT_BREAKER_CNY", "单次规划熔断（元）", "成本与限流", "str"),
    EnvField("SEARCH_COST_CIRCUIT_BREAKER_CNY", "搜索熔断（元）", "成本与限流", "str"),
    EnvField("RATE_LIMIT_COLD_PLANS_PER_DAY", "每会话每日冷规划上限", "成本与限流", "int"),
    EnvField("GLOBAL_DAILY_BUDGET_CNY", "全局每日预算（元）", "成本与限流", "str"),
    # ── 运行与后台 ──
    EnvField("BACKEND_PORT", "后端端口", "运行", "int"),
    EnvField("FRONTEND_URL", "前端地址", "运行", "str", "CORS 白名单按它放行"),
    EnvField("API_BASE_URL", "API 地址", "运行", "str"),
    EnvField("ADMIN_TOKEN", "后台 Token", "运行", "secret", "就是本页面自己的钥匙；留空时只允许本机访问"),
    EnvField("FAULT_INJECTION", "故障注入", "运行", "str", "仅测试用；生产禁止设置"),
)

_ALLOWED_KEYS = frozenset(field.key for field in ENV_FIELDS)

#: 可编辑的 YAML 配置 → 它对应的强校验模型（**改什么都要先过这一关**）。
#: 表里没有的文件一律不允许写，表里的也先校验再落盘。
CONFIG_MODELS: dict[str, type[BaseModel]] = {
    "scoring.yaml": ScoringConfig,
    "limits.yaml": LimitsConfig,
    "ttl.yaml": TtlConfig,
    "pricing.yaml": PricingConfig,
    "seed.yaml": SeedConfig,
}

#: 写完之后再让真正的加载器跑一遍（多一层保险：模型通过但加载路径另有约束时也能拦住）。
CONFIG_LOADERS: dict[str, Callable[[], Any]] = {
    "scoring.yaml": get_scoring_config,
    "limits.yaml": get_limits_config,
    "ttl.yaml": get_ttl_config,
    "pricing.yaml": get_pricing_config,
    "seed.yaml": get_seed_config,
}


# ════════════════════════════════════════════════════════════════════════════
# .env
# ════════════════════════════════════════════════════════════════════════════


def mask_secret(value: str) -> str:
    """Secret 的展示形式：只留前缀与末 4 位，中间一律打码。

    空值原样返回空串 —— 界面上"没配"与"配了但看不见"必须能区分。
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:3]}***{value[-4:]}"


def _key_of(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    match = _KEY_LINE.match(stripped)
    return match.group(1) if match else None


def _read_env_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key = _key_of(line)
        if key is None:
            continue
        _, _, raw = line.partition("=")
        values[key] = raw.strip()
    return values


def _atomic_write(path: Path, content: str) -> None:
    """先写临时文件再 ``replace``：避免写一半被读到（面板自身就在读这些文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _validate_value(field: EnvField, raw: str) -> str:
    value = raw.strip()
    if "\n" in value or "\r" in value:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"{field.key} 的值不能包含换行",
            hint="一个环境变量只能写一行。",
            context={"key": field.key},
        )
    if len(value) > _MAX_VALUE_CHARS:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"{field.key} 的值过长（{len(value)} 字符，上限 {_MAX_VALUE_CHARS}）",
            context={"key": field.key},
        )
    if value == "":
        return value  # 空值 = 清空该项（例如删掉 Key，回到降级模式）
    if field.kind == "int" and not value.lstrip("-").isdigit():
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"{field.key} 需要整数，收到 {value!r}",
            context={"key": field.key},
        )
    if field.kind == "bool" and value.lower() not in {"true", "false", "1", "0"}:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"{field.key} 需要 true/false，收到 {value!r}",
            context={"key": field.key},
        )
    if field.kind == "choice" and field.choices and value not in field.choices:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"{field.key} 只能是：{', '.join(field.choices)}",
            context={"key": field.key, "value": value},
        )
    return value


def env_payload(*, path: Path | None = None) -> list[dict[str, Any]]:
    """面板要展示的环境变量列表（Secret 已打码）。"""
    target = path or env_file()
    values = _parse_env(_read_env_text(target))
    payload: list[dict[str, Any]] = []
    for field in ENV_FIELDS:
        raw = values.get(field.key, "")
        is_set = bool(raw)
        payload.append(
            {
                "key": field.key,
                "label": field.label,
                "group": field.group,
                "kind": field.kind,
                "hint": field.hint,
                "choices": list(field.choices),
                "is_set": is_set,
                "value": mask_secret(raw) if field.kind == "secret" else raw,
            }
        )
    return payload


def update_env(
    updates: Mapping[str, str],
    *,
    path: Path | None = None,
    environ: dict[str, str] | None = None,
) -> list[str]:
    """更新 .env 中白名单内的键，并让改动**立即生效**。

    返回真正发生变化的键。任何一步失败都会把文件与进程环境恢复原状
    —— 面板不该把环境改成一个"下次启动才炸"的状态。
    """
    target = path or env_file()
    runtime = environ if environ is not None else os.environ

    unknown = sorted(key for key in updates if key not in _ALLOWED_KEYS)
    if unknown:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"不允许从面板修改：{', '.join(unknown)}",
            hint="面板只能改白名单里的键（数据库连接串与会话密钥刻意排除在外）。",
            context={"keys": unknown},
        )

    by_key = {field.key: field for field in ENV_FIELDS}
    validated = {
        key: _validate_value(by_key[key], value) for key, value in updates.items()
    }

    original_text = _read_env_text(target)
    original_values = _parse_env(original_text)
    changed = sorted(
        key for key, value in validated.items() if original_values.get(key, "") != value
    )
    if not changed:
        return []

    original_environ = {key: runtime.get(key) for key in validated}
    _atomic_write(target, _rewrite_env(original_text, validated))
    for key, value in validated.items():
        runtime[key] = value
    clear_config_cache()

    try:
        Settings()
    except Exception as exc:
        _atomic_write(target, original_text)
        for key, previous in original_environ.items():
            if previous is None:
                runtime.pop(key, None)
            else:
                runtime[key] = previous
        clear_config_cache()
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"配置未通过校验，已回滚：{exc}",
            hint="检查数值型变量的格式（例如超时必须是数字）。",
            context={"keys": changed},
        ) from exc

    log.info(
        "开发面板更新环境变量",
        extra={"event": "dev.env_updated", "context": {"keys": changed}},
    )
    return changed


def _rewrite_env(text: str, updates: Mapping[str, str]) -> str:
    """就地改行，保留注释与顺序；文件里没有的键追加到末尾。

    ★ 为什么不直接 ``dict → 文件`` 重写 ★
    ``.env.example`` 里大段注释解释了每个变量的含义（以及为什么某些 Key 不能进前端）。
    用解析后的字典重写会把它们全部抹掉，下一次有人打开 `.env` 就再也看不到那些说明了。
    """
    remaining = dict(updates)
    lines: list[str] = []
    for line in text.splitlines():
        key = _key_of(line)
        if key is not None and key in remaining:
            lines.append(f"{key}={remaining.pop(key)}")
        else:
            lines.append(line)
    if remaining:
        lines.append("")
        lines.append("# ── 由开发设置面板追加 ──")
        lines.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(lines).rstrip("\n") + "\n"


# ════════════════════════════════════════════════════════════════════════════
# config/*.yaml
# ════════════════════════════════════════════════════════════════════════════


def config_file_names(*, directory: Path | None = None) -> list[str]:
    """可编辑的配置文件名单（只列白名单里且真实存在的）。"""
    target = directory or default_config_dir()
    return sorted(name for name in CONFIG_LOADERS if (target / name).is_file())


def read_config_file(name: str, *, directory: Path | None = None) -> str:
    path = _config_path(name, directory=directory)
    if not path.is_file():
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"配置文件不存在：{name}",
            context={"name": name},
        )
    return path.read_text(encoding="utf-8")


def write_config_file(name: str, content: str, *, directory: Path | None = None) -> None:
    """写入 YAML 配置。

    ★ 顺序很重要：先校验**候选内容**，通过了才落盘 ★
    先写再校验、失败再回滚也能工作，但那意味着磁盘上曾经存在过一个非法配置 ——
    只要有一个读者恰好在那一瞬间读到了它，服务就会按非法配置启动/运行。
    """
    path = _config_path(name, directory=directory)
    if not path.is_file():
        raise AppError(ErrorCode.NOT_FOUND, f"配置文件不存在：{name}", context={"name": name})

    _validate_config_text(name, content)

    original = path.read_text(encoding="utf-8")
    _atomic_write(path, content)
    clear_config_cache()
    try:
        CONFIG_LOADERS[name]()
    except Exception as exc:  # pragma: no cover - 模型已过，这里只防加载路径另有约束
        _atomic_write(path, original)
        clear_config_cache()
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"{name} 加载失败，已回滚到修改前的内容：{exc}",
            hint="改完先跑 make test-unit：配置的 fail-fast 校验都在那层。",
            context={"name": name},
        ) from exc

    log.info(
        "开发面板更新配置文件",
        extra={"event": "dev.config_updated", "context": {"name": name, "bytes": len(content)}},
    )


def _validate_config_text(name: str, content: str) -> None:
    """用该文件的强校验模型检查候选内容；不通过则**一个字节都不写**。"""
    model = CONFIG_MODELS[name]
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"{name} 不是合法 YAML：{exc}",
            hint="注意缩进用空格；字符串里的冒号要加引号。",
            context={"name": name},
        ) from exc
    if not isinstance(raw, dict):
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"{name} 的顶层必须是映射（dict）",
            context={"name": name},
        )
    try:
        model.model_validate(raw)
    except Exception as exc:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"{name} 未通过校验（文件未被修改）：{exc}",
            hint="改完先跑 make test-unit：配置的 fail-fast 校验都在那层。",
            context={"name": name},
        ) from exc


def _config_path(name: str, *, directory: Path | None) -> Path:
    if name not in CONFIG_LOADERS:
        # 只认白名单里的名字：这样 ``../..`` 之类的路径穿越连门都进不来。
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"不允许编辑的配置文件：{name}",
            hint=f"可编辑：{', '.join(sorted(CONFIG_LOADERS))}",
            context={"name": name},
        )
    return (directory or default_config_dir()) / name


# ════════════════════════════════════════════════════════════════════════════
# 当前生效配置（只读）
# ════════════════════════════════════════════════════════════════════════════


def effective_snapshot() -> dict[str, Any]:
    """当前**真正生效**的配置（认证、阈值、降级模式、版本号）。

    面板如果只显示 `.env` 里的字面值，就答不了最重要的问题：
    "我改了以后，服务到底按什么在跑？"
    """
    settings = get_settings()
    limits = get_limits_config()
    return {
        "env": settings.env,
        "providers": {
            "llm": settings.llm_provider_effective,
            "search": settings.search_provider_effective,
            "map": settings.map_provider_effective,
            "weather": settings.weather_provider,
        },
        "llm": {
            "fast_model": settings.llm_tier_fast_model,
            "strong_model": settings.llm_tier_strong_model,
            "base_url": settings.llm_base_url,
            "timeout_s": settings.llm_timeout_s,
            "max_retries": settings.llm_max_retries,
            "secret_configured": bool(settings.deepseek_api_key),
        },
        "cost_limits": {
            "plan_total_cny": limits.cost.circuit_breaker.plan_total_cny,
            "plan_llm_calls": limits.cost.circuit_breaker.plan_llm_calls,
            "plan_map_calls": limits.cost.circuit_breaker.plan_map_calls,
            "global_daily_cny": limits.cost.global_daily_cny,
        },
        "rate_limits": {
            "requests_per_ip_per_minute": limits.rate_limit.requests_per_ip_per_minute,
            "cold_plans_per_ip_per_day": limits.rate_limit.cold_plans_per_ip_per_day,
            "cold_plans_per_session_per_day": limits.rate_limit.cold_plans_per_session_per_day,
        },
        "degraded_modes": settings.degraded_modes(),
        "config_versions": {
            "scoring": get_scoring_config().version,
            "limits": limits.version,
            "ttl": get_ttl_config().version,
            "pricing": get_pricing_config().version,
            "pricing_calibrated": get_pricing_config().is_fully_calibrated,
        },
    }
