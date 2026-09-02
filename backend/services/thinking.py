"""
思考强度（thinking / reasoning）参数的解析、归一化与上游透传。

参考 RikkaHub 的 ChatCompletionsAPI.kt 设计：
- 按目标 host 分发思考参数（openrouter.ai / api.kilo.ai / integrate.api.nvidia.com 等）
- 支持多层嵌套 extra_body 展开
- 支持任意 agent 框架的思考参数格式（Claude/codex/zcode/dsh/grok build 等）

RikkaHub 的关键实现（ai/src/main/java/me/rerere/ai/provider/providers/openai/ChatCompletionsAPI.kt）：
- OpenRouter: reasoning: {effort: "none"/"low"/"medium"/"high"/"max"}
- 流式响应中 reasoning 内容通过 delta.reasoning 字段透传
- 支持 reasoning_effort 数值/字符串多种格式
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("nvidia2api.thinking")

# 思考相关参数名（全面覆盖各种 agent 框架）
THINKING_PARAM_KEYS = frozenset({
    "chat_template_kwargs", "reasoning_effort", "reasoning_budget",
    "thinking", "enable_thinking", "thinking_budget", "clear_thinking",
    "reasoning", "reasoning_content", "extra_body",
    "reasoning_effort_override", "reasoning_enabled", "reasoning_config",
    "thinking_config", "thinking_enabled", "thinking_level",
    "reasoning_level", "reasoning_mode", "reasoning_type",
    # Claude / Anthropic 特有
    "betas", "thinking_beta",
    # Codex / OpenAI 特有
    "reasoning_effort_value",
    # DSH / Agent 框架
    "reasoning_detail", "reasoning_details",
    # Grok
    "grok_thinking",
    # 通用嵌套
    "openai", "anthropic",
})

# 所有已知的思考相关键模式（用于全面提取）
_THINKING_KEY_PATTERNS = frozenset({
    "reasoning", "reasoning_effort", "reasoning_budget", "reasoning_content",
    "reasoning_enabled", "reasoning_config", "reasoning_level", "reasoning_mode",
    "reasoning_type", "reasoning_effort_override", "reasoning_effort_value",
    "reasoning_detail", "reasoning_details",
    "thinking", "thinking_budget", "thinking_config", "thinking_enabled",
    "thinking_level", "thinking_mode", "thinking_type",
    "enable_thinking", "enabled_thinking", "is_thinking",
    "chat_template_kwargs", "clear_thinking",
    "grok_thinking", "thinking_beta",
})

# chat_template_kwargs 内部键的归类
_KWARG_SWITCH_KEYS = ("thinking", "enable_thinking")
_KWARG_EFFORT_KEYS = ("reasoning_effort",)
_KWARG_BUDGET_KEYS = ("reasoning_budget", "thinking_budget")
_TOP_SWITCH_KEYS = ("thinking", "enable_thinking")
_TOP_BUDGET_KEYS = ("reasoning_budget", "thinking_budget")

# 客户端写法 -> NVIDIA 档位
_EFFORT_ALIASES = {
    "none": "off", "off": "off", "disable": "off", "disabled": "off",
    "false": "off", "0": "off",
    "minimal": "low", "auto": "low", "low": "low",
    "balanced": "medium", "default": "medium", "medium": "medium",
    "high": "high",
    "max": "max", "maximum": "max", "ultra": "max", "xhigh": "max",
    # Claude 风格
    "low_effort": "low", "medium_effort": "medium", "high_effort": "high",
}

_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "max", "xhigh")

_TRUE = {"1", "true", "yes", "on", "enabled"}
_FALSE = {"0", "false", "no", "off", "disabled", "none"}


@dataclass(frozen=True)
class ThinkingCapability:
    toggle_keys: tuple[str, ...] = ("thinking", "enable_thinking")
    effort_key: str | None = "reasoning_effort"
    effort_values: tuple[str, ...] = ("low", "medium", "high", "max")
    supports_budget: bool = True
    always_on: bool = False
    thinking_type: bool = False
    default_effort: str | None = None


_THINKING_CAPABILITIES: list[tuple[str, ThinkingCapability]] = [
    ("kimi-k3", ThinkingCapability(
        always_on=True, effort_key="reasoning_effort",
        effort_values=("low", "high", "max"), default_effort="max")),
    ("kimi-k2.7", ThinkingCapability(always_on=True, thinking_type=True)),
    ("kimi-k2", ThinkingCapability(thinking_type=True)),
    ("kimi", ThinkingCapability(thinking_type=True)),
    ("moonshot", ThinkingCapability(thinking_type=True)),
    ("deepseek-r1", ThinkingCapability(
        always_on=True, effort_key="reasoning_effort",
        effort_values=("high", "max"), default_effort="max")),
    ("deepseek", ThinkingCapability(
        toggle_keys=("thinking",), effort_key="reasoning_effort",
        effort_values=("high", "max"))),
    ("glm", ThinkingCapability(
        toggle_keys=("enable_thinking",), effort_key="reasoning_effort",
        effort_values=("low", "high", "max"))),
    ("qwen", ThinkingCapability(toggle_keys=("enable_thinking",))),
    ("gemma", ThinkingCapability(toggle_keys=("enable_thinking",))),
    ("minimax", ThinkingCapability(
        toggle_keys=("thinking",), effort_key="reasoning_effort",
        effort_values=("low", "medium", "high"))),
    ("step", ThinkingCapability(toggle_keys=("enable_thinking",))),
    ("doubao", ThinkingCapability(
        effort_key="reasoning_effort", effort_values=("minimal", "low", "medium", "high"),
        default_effort="medium")),
    ("grok", ThinkingCapability(always_on=True)),
    ("muse-spark", ThinkingCapability(
        effort_key="reasoning_effort",
        effort_values=("low", "medium", "high", "max"),
        default_effort="high", supports_budget=True,
    )),
]

_DEFAULT_CAPABILITY = ThinkingCapability()


def is_known_thinking_model(model_name: str = "") -> bool:
    """模型名是否命中思考能力表（有显式模式匹配）。"""
    name = (model_name or "").lower()
    return any(pattern in name for pattern, _ in _THINKING_CAPABILITIES)


def resolve_capability(model_name: str = "") -> ThinkingCapability:
    name = (model_name or "").lower()
    for pattern, cap in _THINKING_CAPABILITIES:
        if pattern in name:
            return cap
    return _DEFAULT_CAPABILITY


def _clamp_effort(effort: str, allowed: tuple[str, ...]) -> str | None:
    if not allowed:
        return None
    if effort in allowed:
        return effort
    if effort not in _EFFORT_ORDER:
        return None
    ei = _EFFORT_ORDER.index(effort)
    idxs = [i for i, v in enumerate(_EFFORT_ORDER) if v in allowed]
    if not idxs:
        return None
    nearest = min(idxs, key=lambda i: (abs(i - ei), -i))
    return _EFFORT_ORDER[nearest]


@dataclass
class ThinkingSpec:
    enabled: bool | None = None
    effort: str | None = None
    budget: int | None = None
    template_kwargs: dict = field(default_factory=dict)
    # 保留原始 reasoning 对象（用于 gateway 格式透传）
    raw_reasoning: dict | None = None

    def is_set(self) -> bool:
        return (
            self.enabled is not None or self.effort is not None
            or self.budget is not None or bool(self.template_kwargs)
            or self.raw_reasoning is not None
        )


def _as_bool(value) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
    return None


def _thinking_from_value(value) -> tuple[bool | None, int | None]:
    if isinstance(value, dict):
        t = str(value.get("type") or "").strip().lower()
        if t == "enabled":
            return True, _as_int(value.get("budget_tokens"))
        if t == "disabled":
            return False, None
        return None, None
    flag = _as_bool(value)
    return flag, None


def _as_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _normalize_effort(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        n = int(value)
        if n <= 0:
            return "off"
        return {1: "low", 2: "medium", 3: "high"}.get(n, "max")
    if not isinstance(value, str):
        return None
    low = value.strip().lower()
    if not low:
        return None
    return _EFFORT_ALIASES.get(low, low)


def _parse_reasoning(value) -> tuple[str | None, bool | None, dict | None]:
    """解析 reasoning 对象，返回 (effort, enabled, raw_dict)

    支持格式：
    - "high" -> ("high", True, None)
    - {"effort": "high"} -> ("high", True, {"effort": "high"})
    - {"enabled": True} -> (None, True, {"enabled": True})
    - {"type": "enabled", "budget_tokens": 8000} -> (None, True, {...})
    """
    if value is None:
        return None, None, None
    if isinstance(value, str):
        eff = _normalize_effort(value)
        if eff == "off":
            return None, False, None
        return eff, (True if eff else None), None
    if isinstance(value, bool):
        return None, bool(value), None
    if not isinstance(value, dict):
        return None, None, None
    eff_raw = value.get("effort")
    if eff_raw is None:
        eff_raw = value.get("reasoning_effort")
    eff = _normalize_effort(eff_raw) if eff_raw is not None else None
    if eff == "off":
        return None, False, value
    enabled = None
    t = str(value.get("type") or "").strip().lower()
    if t == "enabled":
        enabled = True
    elif t == "disabled":
        enabled = False
    elif eff is not None:
        enabled = True
    if "enabled" in value:
        fb = _as_bool(value["enabled"])
        if fb is not None:
            enabled = fb
    return eff, enabled, value


def _flatten(payload) -> dict:
    """Universal thinking param extraction from any nesting level.

    支持从任意嵌套层级提取思考参数：
    - 顶层: reasoning_effort, thinking, etc.
    - extra_body.reasoning_effort
    - extra_body.openai.reasoning_effort
    - extra_body.reasoning.effort
    - extra_body.anthropic.thinking
    - betas: ["thinking-2024-01-01"] (Claude 风格)
    """
    if not isinstance(payload, dict):
        return {}
    src = dict(payload)
    found_keys = []

    def _extract_recursive(d: dict, depth: int = 0) -> None:
        if not isinstance(d, dict) or depth > 10:
            return
        for key, value in list(d.items()):
            # extra_body 特殊处理：展开到顶层
            if key == "extra_body" and depth == 0:
                if isinstance(value, dict):
                    for k, v in value.items():
                        if k not in src:
                            src[k] = v
                            found_keys.append(f"extra_body.{k}")
                    _extract_recursive(value, depth + 1)
                continue
            # Claude betas 风格
            if key == "betas" and isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and "thinking" in item.lower():
                        if "enabled" not in src:
                            src["enabled"] = True
                            found_keys.append(f"betas.{item}")
                continue
            key_lower = key.lower()
            is_thinking_key = (
                key in THINKING_PARAM_KEYS or
                key in _THINKING_KEY_PATTERNS or
                "reasoning" in key_lower or
                "thinking" in key_lower
            )
            if is_thinking_key and key not in src:
                src[key] = value
                found_keys.append(f"{'  ' * depth}{key}")
            if isinstance(value, dict) and depth < 10:
                _extract_recursive(value, depth + 1)

    _extract_recursive(payload)
    return src


def parse(payload) -> ThinkingSpec:
    """从客户端请求体中提取思考意图。"""
    src = _flatten(payload)
    spec = ThinkingSpec()

    kwargs = src.get("chat_template_kwargs")
    if isinstance(kwargs, dict):
        for key, value in kwargs.items():
            if key in _KWARG_SWITCH_KEYS:
                flag, budget = _thinking_from_value(value)
                if flag is not None and spec.enabled is None:
                    spec.enabled = flag
                    if budget is not None and spec.budget is None:
                        spec.budget = budget
            elif key in _KWARG_EFFORT_KEYS:
                effort = _normalize_effort(value)
                if effort and spec.effort is None:
                    spec.effort = effort
            elif key in _KWARG_BUDGET_KEYS:
                budget = _as_int(value)
                if budget is not None and spec.budget is None:
                    spec.budget = budget
            else:
                spec.template_kwargs[key] = value

    for key in _TOP_SWITCH_KEYS:
        flag, budget = _thinking_from_value(src.get(key))
        if flag is not None and spec.enabled is None:
            spec.enabled = flag
            if budget is not None and spec.budget is None:
                spec.budget = budget

    # Claude betas 风格：betas 中包含 thinking 表示开启
    if src.get("enabled") is True and spec.enabled is None:
        spec.enabled = True

    effort = _normalize_effort(src.get("reasoning_effort"))
    if effort and spec.effort is None:
        spec.effort = effort
    for key in _TOP_BUDGET_KEYS:
        budget = _as_int(src.get(key))
        if budget is not None and spec.budget is None:
            spec.budget = budget

    raw_reasoning = src.get("reasoning")
    if raw_reasoning is not None:
        eff, flag, raw = _parse_reasoning(raw_reasoning)
        if eff and spec.effort is None:
            spec.effort = eff
        if flag is not None and spec.enabled is None:
            spec.enabled = flag
        if isinstance(raw, dict):
            spec.raw_reasoning = raw
            b = _as_int(raw.get("budget_tokens") or raw.get("reasoning_budget") or raw.get("thinking_budget"))
            if b is not None and spec.budget is None:
                spec.budget = b

    clear = _as_bool(src.get("clear_thinking"))
    if clear is not None:
        spec.template_kwargs.setdefault("clear_thinking", clear)

    if spec.effort == "off":
        if spec.enabled is None:
            spec.enabled = False
        spec.effort = None
    elif spec.effort is not None and spec.enabled is None:
        spec.enabled = True
    if spec.budget is not None and spec.enabled is None:
        spec.enabled = True

    return spec


def _passthrough_enabled(channel=None) -> bool:
    from services import sysconfig
    return bool(sysconfig.get("thinking_passthrough", channel))


def _is_stripped(model_name: str, channel=None) -> bool:
    from services import sysconfig
    raw = sysconfig.get("thinking_strip_models", channel) or ""
    name = (model_name or "").lower()
    for token in str(raw).split(","):
        token = token.strip().lower()
        if token and token in name:
            return True
    return False


def _default_effort(channel=None) -> str:
    from services import sysconfig
    return str(sysconfig.get("default_thinking_effort", channel) or "high").strip().lower()


def _get_host(channel=None) -> str:
    if channel is None:
        return ""
    base = str(getattr(channel, "base_url", "") or "")
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base)
        return parsed.host.lower()
    except Exception:
        return base.lower()


def _is_reasoning_gateway(host: str, model_name: str = "") -> bool:
    """判断是否为需要特殊处理 reasoning 格式的网关

    RikkaHub 的实现：
    - openrouter.ai: reasoning: {effort: "none"/"low"/"medium"/"high"/"max"}
    - api.kilo.ai: 类似 openrouter
    - muse-spark: 需要特殊处理
    """
    if not host:
        return False
    if "openrouter.ai" in host or "kilo" in host or "api.kilo.ai" in host:
        return True
    if "muse-spark" in model_name.lower() or "contributor" in model_name.lower():
        return True
    return False


def to_upstream(spec: ThinkingSpec, model_name: str = "", channel=None) -> dict:
    """把思考意图转成上游 body 片段；无需下发时返回 empty dict。

    参考 RikkaHub 的 ChatCompletionsAPI.kt 实现：
    - OpenRouter: reasoning: {effort: ...}
    - 其他网关: 类似格式
    """
    if not spec.is_set():
        return {}
    if not _passthrough_enabled(channel):
        return {}
    if _is_stripped(model_name, channel):
        return {}

    host = _get_host(channel)
    cap = resolve_capability(model_name)
    out: dict = {}
    enabled = spec.enabled

    if cap.always_on and enabled is False:
        enabled = None

    # 网关格式处理（RikkaHub 风格）
    if _is_reasoning_gateway(host, model_name):
        # 优先使用原始 reasoning 对象
        if spec.raw_reasoning and isinstance(spec.raw_reasoning, dict):
            out["reasoning"] = spec.raw_reasoning
            if spec.budget is not None and cap.supports_budget:
                out["reasoning"]["budget_tokens"] = spec.budget
            return out

        if enabled is False:
            out["reasoning"] = {"effort": "none"}
        elif enabled is True:
            if spec.effort:
                eff = _clamp_effort(spec.effort, ("low", "medium", "high", "max")) or spec.effort
                if spec.effort.lower() in ("off", "none"):
                    out["reasoning"] = {"effort": "none"}
                else:
                    out["reasoning"] = {"effort": eff}
            else:
                out["reasoning"] = {"enabled": True}
        elif spec.effort:
            eff = _clamp_effort(spec.effort, ("low", "medium", "high", "max")) or spec.effort
            out["reasoning"] = {"effort": eff}
        if "reasoning" in out:
            if spec.budget is not None and cap.supports_budget:
                out["reasoning"]["budget_tokens"] = spec.budget
            return out

    if cap.thinking_type:
        if enabled is True:
            out["thinking"] = {"type": "enabled"}
        elif enabled is False and not cap.always_on:
            out["thinking"] = {"type": "disabled"}
    elif not cap.always_on:
        kwargs = dict(spec.template_kwargs)
        if enabled is not None:
            for key in cap.toggle_keys:
                kwargs[key] = enabled
        if kwargs:
            out["chat_template_kwargs"] = kwargs

    if cap.effort_key:
        effort = spec.effort
        if effort is None and spec.enabled is True and spec.budget is None:
            effort = cap.default_effort or _default_effort(channel)
        if effort:
            eff = _clamp_effort(effort, cap.effort_values)
            if eff:
                out[cap.effort_key] = eff
    if spec.budget is not None and cap.supports_budget:
        out["reasoning_budget"] = spec.budget

    return out


def build_upstream(payload, model_name: str = "", channel=None) -> dict:
    return to_upstream(parse(payload), model_name, channel)
