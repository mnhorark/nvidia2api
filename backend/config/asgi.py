import os
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# 这里原本写着 `os.environ.setdefault("ASGI_THREADS", "64")`，并配了一段注释
# 说"asgiref 的 thread-sensitive 执行器默认只有 min(32, cpu+4) 个线程，抬到 64"。
# **两件事都是错的，而且错得会让人做出危险的判断**，所以整段删掉而不是改数值：
#
# 1. `ASGI_THREADS` 这个环境变量不存在。`grep -r ASGI_THREADS` 在已安装的
#    asgiref 与 Django 包里各 0 命中——它不控制任何东西。
# 2. `min(32, cpu+4)` 描述的是 `thread_sensitive=False` 时
#    `loop.run_in_executor(None, ...)` 用的事件循环默认池。同步视图不走那条路。
#    asgiref 3.11.1 `sync.py:402` 是
#    `single_thread_executor = ThreadPoolExecutor(max_workers=1)`，
#    `:481` 在 `thread_sensitive=True` 时选中它；`:470` 即便有
#    ThreadSensitiveContext，新建的也是 `max_workers=1`。
#    **thread-sensitive 路径上没有任何一处可以 >1，也没有任何配置能改它。**
#
# 而 Django `ASGIHandler._get_response_async` 对所有非协程视图都用
# `sync_to_async(view, thread_sensitive=True)`。本项目的每一个入口——
# 数据面 chat/responses/anthropic/count_tokens/models、管理面全部
# `/api/admin/*`、以及 `/healthz` 与 `/metrics`——实测都是同步视图。
# 所以真实模型是：**全系统共用一条 OS 线程**，唯一例外是流式生成器的迭代体
# （`StreamingHttpResponse` 的异步迭代 + `ASGIHandler` 的 `aclosing(aiter(...))`，
# 那部分跑在事件循环上）。
#
# 这条线程就是吞吐上限，也是故障域：任何一段同步视图代码挂住，
# 整个网关（含健康检查）一起挂。根治办法是把数据面视图改成 async
# 并去掉 `race_chat` 里的 `asyncio.run`，见 docs/architecture-review-2026-09b.md
# 第六节第 11 项——它必须先补上第 12 项的测试，否则会把"冻结一条线程"
# 换成"每个非流式请求泄漏 max_routes_per_request 个 RPM 槽位"。

application = get_asgi_application()
