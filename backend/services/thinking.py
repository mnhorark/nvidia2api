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

_TRUE = {"1", "true", "yes", "on", "enabled"}
_FALSE = {"0", "false", "no", "off", "disabled", "none"}


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
    """把思考意图转成上游 body 片段；无需下发时返回空 dict。"""
    if not spec.is_set():
        return {}
    if not _passthrough_enabled():
        logger.info("thinking params dropped: passthrough disabled")
        return {}
    if _is_stripped(model_name):
        logger.info("thinking params dropped: model %s is in strip list", model_name)
        return {}

    out: dict = {}
    kwargs = dict(spec.template_kwargs)
    if spec.enabled is not None:
        # 两种开关都写：DeepSeek/Nemotron 系认 thinking，Qwen/GLM 系认 enable_thinking
        kwargs["thinking"] = spec.enabled
        kwargs["enable_thinking"] = spec.enabled
    if kwargs:
        out["chat_template_kwargs"] = kwargs
    if spec.effort:
        out["reasoning_effort"] = spec.effort
    elif spec.enabled is True:
        # 客户端只开启思考未指定档位时，自动映射默认档位（如 Trae 只传 {"type":"enabled"}）
        effort = _default_effort()
        if effort and effort != "off":
            out["reasoning_effort"] = effort
    if spec.budget is not None:
        out["reasoning_budget"] = spec.budget
    return out


def build_upstream(payload, model_name: str = "") -> dict:
    """parse + to_upstream：视图里通常只需要这一步。"""
    return to_upstream(parse(payload), model_name)
