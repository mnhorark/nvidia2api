"""思考强度（thinking / reasoning）参数的解析、归一化与上游透传。

NVIDIA 上游对"思考能力"没有唯一写法：有的模型吃
`chat_template_kwargs.thinking`，有的吃 `chat_template_kwargs.enable_thinking`，
强度档位用顶层 `reasoning_effort`，预算用顶层 `reasoning_budget`。客户端侧
（OpenAI SDK、各类中转、前端）发过来的形态更是五花八门。

本模块负责把各种写法收敛成上游能接受的形态，并按模型能力决定是否下发，
避免在不支持的模型上无脑发送导致 400。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("nvidia2api.thinking")

# 思考相关参数名。这些键不走通用透传，而是由本模块归一化后重新写入上游 body。
THINKING_PARAM_KEYS = frozenset({
    "chat_template_kwargs",
    "reasoning_effort",
    "reasoning_budget",
    "thinking",
    "enable_thinking",
    "thinking_budget",
    "clear_thinking",
    "extra_body",  # OpenAI SDK 的透传容器
})

# chat_template_kwargs 内部键的归类
_KWARG_SWITCH_KEYS = ("thinking", "enable_thinking")
_KWARG_EFFORT_KEYS = ("reasoning_effort",)
_KWARG_BUDGET_KEYS = ("reasoning_budget", "thinking_budget")

# 顶层开关键 / 预算键
_TOP_SWITCH_KEYS = ("thinking", "enable_thinking")
_TOP_BUDGET_KEYS = ("reasoning_budget", "thinking_budget")

# 客户端写法 -> NVIDIA 档位。off 表示显式关闭思考。
_EFFORT_ALIASES = {
    "none": "off", "off": "off", "disable": "off", "disabled": "off",
    "false": "off", "0": "off",
    "minimal": "low", "auto": "low", "low": "low",
    "balanced": "medium", "default": "medium", "medium": "medium",
    "high": "high",
    "max": "max", "maximum": "max", "ultra": "max", "xhigh": "max",
}

# 档位强度顺序，用于"钳制"到模型支持的档位集合
_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "max", "xhigh")

_TRUE = {"1", "true", "yes", "on", "enabled"}
_FALSE = {"0", "false", "no", "off", "disabled", "none"}


# ---------------------------------------------------------------------------
# 模型族思考能力知识库
# ---------------------------------------------------------------------------
# 不同 LLM 用不同的关键字控制思考强度，传入模型不认识的字段会直接 400。
# 参考 vLLM reasoning_outputs / Featherless chat-template-kwargs / 各家官方文档：
#   Qwen3        -> chat_template_kwargs.enable_thinking（默认开启）
#   GLM 4.5+/5   -> enable_thinking + reasoning_effort(low/high/max)
#   Gemma 4      -> enable_thinking（默认关闭）
#   DeepSeek     -> chat_template_kwargs.thinking + reasoning_effort(high/max)
#   Kimi K3      -> 仅顶层 reasoning_effort(low/high/max)，传 thinking 会报错
#   Kimi K2.x    -> thinking.type(enabled/disabled) + thinking.keep
#   MiniMax M3   -> chat_template_kwargs.thinking（混合思考）
#   Doubao 2.0   -> 顶层 reasoning_effort(minimal/low/medium/high)
#   Stepfun      -> enable_thinking
#   常开思考模型（DeepSeek-R1/Kimi-K2.7/grok-oss 等）无需也不应传关闭开关。

@dataclass(frozen=True)
class ThinkingCapability:
    """某个模型族支持的思考控制方式。"""
    # chat_template_kwargs 里使用的开关键；空元组表示不使用该机制
    toggle_keys: tuple[str, ...] = ("thinking", "enable_thinking")
    # 档位字段名（如 reasoning_effort）；None 表示该模型不支持档位
    effort_key: str | None = "reasoning_effort"
    # 允许的档位值（按强度升序）；用于把非法档位钳制到合法区间
    effort_values: tuple[str, ...] = ("low", "medium", "high", "max")
    supports_budget: bool = True          # 是否接受 reasoning_budget
    always_on: bool = False               # 始终思考，无法关闭
    thinking_type: bool = False           # Kimi 风格 thinking.type 开关
    default_effort: str | None = None     # 只开启未指定档位时的默认档位


# 按子串匹配，顺序优先（先精确后宽泛）
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
]

_DEFAULT_CAPABILITY = ThinkingCapability()


def resolve_capability(model_name: str = "") -> ThinkingCapability:
    """按模型名解析思考能力；未命中时回落到通用默认（双开关 + 档位）。"""
    name = (model_name or "").lower()
    for pattern, cap in _THINKING_CAPABILITIES:
        if pattern in name:
            return cap
    return _DEFAULT_CAPABILITY


def _clamp_effort(effort: str, allowed: tuple[str, ...]) -> str | None:
    """把档位钳制到模型允许的集合；无法识别且不允许时返回 None（不下发，避免 400）。"""
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
    # 强度最接近的允许值；平局时取更高档（如 medium 在 low/high 之间 -> high）
    nearest = min(idxs, key=lambda i: (abs(i - ei), -i))
    return _EFFORT_ORDER[nearest]


@dataclass
class ThinkingSpec:
    """归一化后的思考意图。所有字段为 None 表示客户端未表达任何意图。"""

    enabled: bool | None = None          # 是否开启思考
    effort: str | None = None            # low / medium / high / max
    budget: int | None = None            # 思考 token 预算
    template_kwargs: dict = field(default_factory=dict)  # 需原样透传的其余模板参数

    def is_set(self) -> bool:
        return (
            self.enabled is not None
            or self.effort is not None
            or self.budget is not None
            or bool(self.template_kwargs)
        )


# ---------------------------------------------------------------------------
# 值归一化
# ---------------------------------------------------------------------------

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
    """把 thinking 参数的各种客户端形态解析为 (enabled, budget)。

    - 布尔 / "enabled" / "on" ...
    - Anthropic 风格 dict: {"type": "enabled"|"disabled", "budget_tokens": N}
    - 数字：>0 视为开启（保留原数字档位语义由调用方处理）
    """
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
    """把各种档位写法收敛成 off / low / medium / high / max。

    识别不了的写法原样保留（小写）：中转层不该替上游决定哪些值合法，
    上游若拒绝会在响应里体现，届时可用 thinking_strip_models 规避。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # 数字档位：0 视为关闭，其余按 1/2/3、>=4 映射
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


def _flatten(payload) -> dict:
    """合并顶层与 extra_body（OpenAI SDK 透传容器），顶层优先。"""
    if not isinstance(payload, dict):
        return {}
    src = dict(payload)
    extra = src.get("extra_body")
    if isinstance(extra, dict):
        for key, value in extra.items():
            src.setdefault(key, value)
    return src


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def parse(payload) -> ThinkingSpec:
    """从客户端请求体中提取思考意图。"""
    src = _flatten(payload)
    spec = ThinkingSpec()

    # 1) chat_template_kwargs：识别已知键，其余原样保留
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

    # 2) 顶层开关
    for key in _TOP_SWITCH_KEYS:
        flag, budget = _thinking_from_value(src.get(key))
        if flag is not None and spec.enabled is None:
            spec.enabled = flag
            if budget is not None and spec.budget is None:
                spec.budget = budget

    # 3) 顶层档位与预算
    effort = _normalize_effort(src.get("reasoning_effort"))
    if effort and spec.effort is None:
        spec.effort = effort
    for key in _TOP_BUDGET_KEYS:
        budget = _as_int(src.get(key))
        if budget is not None and spec.budget is None:
            spec.budget = budget

    # 4) 是否保留思考过程（GLM 等模型用）
    clear = _as_bool(src.get("clear_thinking"))
    if clear is not None:
        spec.template_kwargs.setdefault("clear_thinking", clear)

    # 5) 意图补全：档位/预算隐含开启，off 显式关闭
    if spec.effort == "off":
        if spec.enabled is None:
            spec.enabled = False
        spec.effort = None
    elif spec.effort is not None and spec.enabled is None:
        spec.enabled = True
    if spec.budget is not None and spec.enabled is None:
        spec.enabled = True

    return spec


# ---------------------------------------------------------------------------
# 上游下发
# ---------------------------------------------------------------------------

def _passthrough_enabled() -> bool:
    from services import sysconfig
    return bool(sysconfig.get("thinking_passthrough"))


def _is_stripped(model_name: str) -> bool:
    """命中剥离名单的模型不发送任何思考参数（上游会 400）。"""
    from services import sysconfig
    raw = sysconfig.get("thinking_strip_models") or ""
    name = (model_name or "").lower()
    for token in str(raw).split(","):
        token = token.strip().lower()
        if token and token in name:
            return True
    return False


def _default_effort() -> str:
    """客户端只开启思考但未指定档位时使用的默认档位。"""
    from services import sysconfig
    return str(sysconfig.get("default_thinking_effort") or "medium").strip().lower()


def to_upstream(spec: ThinkingSpec, model_name: str = "") -> dict:
    """把思考意图转成上游 body 片段；无需下发时返回空 dict。

    按模型族能力（resolve_capability）选择下发关键字：
    - 只发该模型认识的开关键（Qwen/GLM 用 enable_thinking、DeepSeek 用 thinking、
      Kimi K2.x 用 thinking.type、常开模型不传开关）；
    - 档位钳制到模型允许的取值（如 DeepSeek 只支持 high/max，客户端传 low 就提为 high），
      避免"非法关键字/非法取值"导致整个请求 400。
    """
    if not spec.is_set():
        return {}
    if not _passthrough_enabled():
        logger.info("thinking params dropped: passthrough disabled")
        return {}
    if _is_stripped(model_name):
        logger.info("thinking params dropped: model %s is in strip list", model_name)
        return {}

    cap = resolve_capability(model_name)
    out: dict = {}
    enabled = spec.enabled

    if cap.always_on and enabled is False:
        # 常开思考模型无法关闭：不发关闭开关，仅保留档位意图
        enabled = None

    if cap.thinking_type:
        # Kimi 风格 thinking.type 开关（仅 enabled/disabled）
        if enabled is True:
            out["thinking"] = {"type": "enabled"}
        elif enabled is False and not cap.always_on:
            out["thinking"] = {"type": "disabled"}
    elif not cap.always_on:
        # 非"常开"模型才发开关键；常开模型（K3/R1/grok 等）传开关会报错
        kwargs = dict(spec.template_kwargs)
        if enabled is not None:
            for key in cap.toggle_keys:
                kwargs[key] = enabled
        if kwargs:
            out["chat_template_kwargs"] = kwargs

    if cap.effort_key:
        effort = spec.effort
        if effort is None and spec.enabled is True and spec.budget is None:
            # 客户端只开启思考未指定档位时，自动映射默认档位
            effort = cap.default_effort or _default_effort()
        if effort:
            eff = _clamp_effort(effort, cap.effort_values)
            if eff:
                out[cap.effort_key] = eff
    if spec.budget is not None and cap.supports_budget:
        out["reasoning_budget"] = spec.budget
    return out


def build_upstream(payload, model_name: str = "") -> dict:
    """parse + to_upstream：视图里通常只需要这一步。"""
    return to_upstream(parse(payload), model_name)
