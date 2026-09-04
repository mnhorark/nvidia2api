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

# 客户端写法 -> 内部档位。
# 内部档位对齐 OpenRouter reasoning.effort 的完整梯度（2026-09 文档）：
#   none < minimal < low < medium < high < xhigh < max
# （off 是"显式关闭"的语义标记，parse() 中折算为 enabled=False）
_EFFORT_ALIASES = {
    "none": "off", "off": "off", "disable": "off", "disabled": "off",
    "false": "off", "0": "off",
    "minimal": "minimal", "min": "minimal", "tiny": "minimal",
    "auto": "low", "low": "low",
    "balanced": "medium", "default": "medium", "medium": "medium",
    "high": "high",
    "xhigh": "xhigh", "x-high": "xhigh", "extra_high": "xhigh",
    "extrahigh": "xhigh", "extra high": "xhigh",
    "max": "max", "maximum": "max", "ultra": "max",
    # Claude 风格
    "low_effort": "low", "medium_effort": "medium", "high_effort": "high",
}

# 档位强序（钳制/翻译用）：max 为最高档
_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# effort -> budget_tokens 档位表（Anthropic/Gemini/OpenRouter 风格预算翻译）。
# 参考锚点：Anthropic budget_tokens 下限 1024（extended thinking 文档）、
# Gemini thinkingBudget 24576 上限、Claude "think a lot" ~32k、64k 为
# extended thinking 常见上限。目标渠道只认预算不认档位时使用。
_EFFORT_BUDGET_TIERS: tuple[tuple[str, int], ...] = (
    ("minimal", 1024),
    ("low", 4096),
    ("medium", 8192),
    ("high", 16384),
    ("xhigh", 32768),
    ("max", 65536),
)

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
    # 预算的 chat_template_kwargs 注入键（vLLM 系模型控制思考量的
    # 标准通道是模板变量，如 Qwen3 的 thinking_budget）。设置后：
    # budget 意图（显式预算或 effort 档位换算）会同步写入
    # chat_template_kwargs[<budget_kwarg>]，与顶层字段双通道并存。
    budget_kwarg: str | None = None
    # effort 与 budget 互斥（qwen3.8-flash 特有约束：reasoning_effort 与
    # thinking_budget 同发整包 400）。True 时二者只发其一：预算意图
    # 归一到 effort 档位下发，effort 意图不再换算注入模板变量。
    effort_budget_exclusive: bool = False
    # 模型专属 预算->档位 换算表（qwen3.8-flash 官方口径：
    # 0-4096->low / 4097-16384->medium / 16385-262144->xhigh），
    # 形如 ((上限, 档位), ...)，升序，首达即归档。设置后优先于
    # 通用 budget_to_effort 换算（通用表是 Anthropic/CLAUDE 锚点，
    # 与 qwen 官方换算完全不同）。
    budget_effort_tiers: tuple[tuple[int, str], ...] = ()
    # 开关意图不发默认档（qwen 专用）：客户端只开 toggle 键、没给档位时
    # **不**注入默认 effort。仅词表特殊（通用档不在词表会 400）且开关
    # 通道已完整表达"开启"意图的模型需要。普通开关模型保持旧行为：
    # 开关 + 默认档双发（档位是它们的强度表达）。
    suppress_toggle_default: bool = False


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
    ("qwen", ThinkingCapability(
        toggle_keys=("enable_thinking",),
        # qwen3.8-flash 实测词表（2026-09 线上 400 实证）：仅认
        # xhigh/medium/low（服务端默认 xhigh）；high/max 经 _clamp_effort
        # 归一到 xhigh，minimal 归到 low。通用档 high 不在词表，
        # 原样下发会整包 400（req_68ec7675 案）。
        # 注意 default_effort 保持 None：qwen 是开关驱动模型，客户端
        # 只开 enable_thinking 时不发明档位（上游默认即 xhigh）。
        effort_key="reasoning_effort",
        effort_values=("low", "medium", "xhigh"),
        # qwen3.8-flash 互斥约束：reasoning_effort 与 thinking_budget
        # 不能同时下发（400），预算意图经官方换算表归到档位：
        # 0-4096->low / 4097-16384->medium / 16385-262144->xhigh。
        effort_budget_exclusive=True,
        budget_effort_tiers=((4096, "low"), (16384, "medium"),
                             (262144, "xhigh")),
        suppress_toggle_default=True,
    )),
    ("gemma", ThinkingCapability(toggle_keys=("enable_thinking",),
                                 effort_key=None)),
    ("minimax", ThinkingCapability(
        toggle_keys=("thinking",), effort_key="reasoning_effort",
        effort_values=("low", "medium", "high"))),
    ("step", ThinkingCapability(toggle_keys=("enable_thinking",),
                                effort_key=None)),
    ("doubao", ThinkingCapability(
        effort_key="reasoning_effort", effort_values=("minimal", "low", "medium", "high"),
        default_effort="medium")),
    ("grok", ThinkingCapability(always_on=True)),
    ("muse-spark", ThinkingCapability(
        effort_key="reasoning_effort",
        # 全档透传：zen 端点的档位支持是黑盒，静默改写不如让上游明确表态
        # （400 可见可诊断，静默忽略无法察觉）
        effort_values=("minimal", "low", "medium", "high", "xhigh", "max"),
        default_effort="high", supports_budget=True,
        # vLLM 系：强度同步写模板变量 thinking_budget（数值通道），
        # 双通道并存——zen 若忽略顶层 reasoning_effort，模板变量仍生效
        budget_kwarg="thinking_budget",
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
    """把档位钳制到目标模型支持集合的最近档（同距取更高档）。"""
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


def effort_to_budget(effort: str | None) -> int | None:
    """档位 -> 预算 token（目标渠道只认预算形态时使用）。"""
    for name, tokens in _EFFORT_BUDGET_TIERS:
        if effort == name:
            return tokens
    return None


def budget_to_effort(budget: int | None, tiers=None) -> str | None:
    """预算 token -> 档位（目标渠道只认档位形态时使用）。

    `tiers` 为模型专属换算表时按"首个天花板 >= 预算"归档（区间语义，
    如 qwen3.8-flash 官方口径 0-4096->low / 4097-16384->medium /
    16385-262144->xhigh）；缺省用通用表并按"离哪档最近"归档（预算
    20000 离 high(16384) 比离 xhigh(32768) 近得多，向上归档会近似
    双倍预算）。
    """
    if budget is None or budget <= 0:
        return "off"
    if tiers:
        for ceiling, name in tiers:
            if budget <= ceiling:
                return name
        return tiers[-1][1]
    tiers = _EFFORT_BUDGET_TIERS
    best_name, best_dist = tiers[-1][0], abs(budget - tiers[-1][1])
    for name, tokens in tiers:
        d = abs(budget - tokens)
        if d < best_dist:
            best_name, best_dist = name, d
    return best_name


@dataclass
class ThinkingSpec:
    enabled: bool | None = None
    effort: str | None = None
    budget: int | None = None
    template_kwargs: dict = field(default_factory=dict)
    # 保留原始 reasoning 对象（用于 gateway 格式透传）
    raw_reasoning: dict | None = None
    # 客户端是否**显式**发送过开关键（thinking/enable_thinking/reasoning
    # 对象等）：True=开关意图是客户端的真实表达；False=enabled 是 parse
    # 从档位/预算推断的——下游据此区分"传递"与"不发明"
    explicit_toggle: bool = False

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
        # 数值档位：1/2/3 对应 low/medium/high，4 及以上进入 xhigh/max
        return {1: "low", 2: "medium", 3: "high", 4: "xhigh"}.get(n, "max")
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

    # 客户端是否**显式**表达了开关意图（thinking/enable_thinking 等
    # 顶层/嵌套键真实出现过）：to_upstream 据此区分"传递显式开关"与
    # "从档位推断 enabled 后凭空合成开关"——后者是发明意图
    explicit_toggle = False

    kwargs = src.get("chat_template_kwargs")
    if isinstance(kwargs, dict):
        for key, value in kwargs.items():
            if key in _KWARG_SWITCH_KEYS:
                explicit_toggle = True
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
        if flag is not None:
            explicit_toggle = True
            if spec.enabled is None:
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
        if flag is not None:
            explicit_toggle = True
            if spec.enabled is None:
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

    spec.explicit_toggle = explicit_toggle
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
    """判断是否为需要 reasoning 对象格式的网关（OpenRouter/Kilo 系）。

    RikkaHub 的实现：
    - openrouter.ai: reasoning: {effort: "none"/"low"/"medium"/"high"/"max"}
    - api.kilo.ai: 类似 openrouter

    注意：muse-spark 曾被归入此分支（沿用 RikkaHub 注释"muse-spark 需要
    特殊处理"），但实测（2026-09，用户对照：kimi-k3 同参透传生效、muse 不
    生效）zen 端点对 reasoning 对象不响应——muse 已移出本分支，与 kimi
    同构走 reasoning_effort 直传（muse capability: effort_key=reasoning_effort，
    7 档词汇表经 _clamp_effort 适配）。
    """
    # host 与 model_name 双空才早退：渠道未知时仍可靠模型名识别网关
    if not host and not model_name:
        return False
    if "openrouter.ai" in host or "kilo" in host or "api.kilo.ai" in host:
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
    # OpenRouter reasoning.effort 完整梯度（2026-09 文档）：
    #   none / minimal / low / medium / high / xhigh / max
    _GATEWAY_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
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
                eff = _clamp_effort(spec.effort, _GATEWAY_EFFORTS) or spec.effort
                if spec.effort.lower() in ("off", "none"):
                    out["reasoning"] = {"effort": "none"}
                else:
                    out["reasoning"] = {"effort": eff}
            else:
                out["reasoning"] = {"enabled": True}
        elif spec.effort:
            eff = _clamp_effort(spec.effort, _GATEWAY_EFFORTS) or spec.effort
            out["reasoning"] = {"effort": eff}
        if "reasoning" in out:
            if spec.budget is not None and cap.supports_budget:
                out["reasoning"]["budget_tokens"] = spec.budget
            return out

    if cap.thinking_type:
        if enabled is True:
            out["thinking"] = {"type": "enabled"}
            # Claude 风格 thinking 对象接受 budget_tokens：
            # - 客户端给了预算 → 原样带入
            # - 只给了档位 → 按档位表翻译成预算下限（档位意图不丢失）。
            #   预算是数值尺度，可直接表达 minimal——不做档位钳制，
            #   否则 minimal 会被钳成 low 再翻译成 4096（翻倍）。
            if spec.budget is not None:
                out["thinking"]["budget_tokens"] = spec.budget
            elif spec.effort:
                tokens = effort_to_budget(spec.effort)
                if tokens is None:
                    eff = _clamp_effort(spec.effort, cap.effort_values)
                    tokens = effort_to_budget(eff or spec.effort)
                if tokens:
                    out["thinking"]["budget_tokens"] = tokens
        elif enabled is False and not cap.always_on:
            out["thinking"] = {"type": "disabled"}
    elif not cap.always_on:
        # 开关注入的边界：区分"传递客户端意图"与"发明客户端意图"。
        # - off（enabled=False）：**必须**注入 toggle=false——deepseek/
        #   muse 等默认开启思考的模型，不发 false 关不掉；
        # - on：仅当客户端**显式**发过开关注入（spec.explicit_toggle）。
        #   推断出的 True（从档位/预算合成）不注入——档位已是开启表达，
        #   旧行为对 "reasoning_effort: xhigh" 凭空合成
        #   thinking:true+enable_thinking:true 属发明意图（严格上游
        #   400 风险面，语义与档位重复），并曾连带合成
        #   thinking_budget=32768 挤占窗口（zcode muse 案）。
        # 直接构造 ThinkingSpec 的内部调用方（无显式标记）同归推断态。
        kwargs = dict(spec.template_kwargs)
        toggle_wanted = (enabled is False or spec.explicit_toggle)
        if toggle_wanted:
            for key in cap.toggle_keys:
                kwargs[key] = enabled
        if kwargs:
            out["chat_template_kwargs"] = kwargs

    if cap.effort_key:
        effort = spec.effort
        # 开关意图（只开了 enable_thinking/thinking，无档位无预算）是否
        # 值得注入默认档位：默认注入（旧行为——普通开关模型靠"开关+默认
        # 档"表达强度）；仅显式声明 suppress_toggle_default 的模型跳过
        # （qwen3.8-flash：词表特殊 xhigh/medium/low，注入的默认档 high
        # 不在词表整包 400（req_68ec7675 案），且上游以开关为准，
        # 发明档位属添加客户端未给的语义）。
        inject_default = not cap.suppress_toggle_default
        if effort is None and spec.enabled is True and spec.budget is None \
                and inject_default:
            effort = cap.default_effort or _default_effort(channel)
        # 预算意图在"只认档位"的渠道上回落为最近档位（预算档位表反向翻译；
        # 模型有专属换算表时用官方区间语义）
        if effort is None and spec.budget is not None and not cap.supports_budget:
            effort = budget_to_effort(spec.budget, cap.budget_effort_tiers)
        # effort/budget 互斥渠道（qwen3.8-flash）：预算意图整体归到档位
        # 通道，避免两个互斥字段同时出现整包 400
        if effort is None and spec.budget is not None \
                and cap.effort_budget_exclusive:
            effort = budget_to_effort(spec.budget, cap.budget_effort_tiers)
        if effort:
            eff = _clamp_effort(effort, cap.effort_values)
            if eff:
                out[cap.effort_key] = eff
    if spec.budget is not None and cap.supports_budget \
            and not cap.effort_budget_exclusive:
        out["reasoning_budget"] = spec.budget

    # budget_kwarg 双通道：把**客户端显式给的**预算意图写进
    # chat_template_kwargs——vLLM 系上游只认模板变量，顶层字段会被
    # 静默忽略（zen muse 实测）。
    # 2026-09 收紧：档位意图**不再**经换算表合成预算注入。旧行为对
    # "reasoning_effort: xhigh" 凭空合成 thinking_budget=32768——
    # 32K 思考预算挤占模型上下文窗口，正是 zcode 场景 muse 请求耗时
    # 暴涨/上游窗口拒绝的推手。档位意图只发档位；预算意图只在客户端
    # 显式给预算时走双通道。
    # 互斥渠道例外：effort 档位已在顶层下发时**不再**注入模板变量，
    # 否则 reasoning_effort + thinking_budget 同发（qwen3.8-flash 400）。
    if cap.budget_kwarg and not cap.effort_budget_exclusive:
        tokens = spec.budget
        if tokens:
            kwargs = out.setdefault("chat_template_kwargs", {})
            kwargs[cap.budget_kwarg] = tokens

    return out


def build_upstream(payload, model_name: str = "", channel=None) -> dict:
    return to_upstream(parse(payload), model_name, channel)
