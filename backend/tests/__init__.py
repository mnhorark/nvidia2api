
import asyncio


def call_view(view, *args, **kwargs):
    """同步地调用一个 async 视图，返回它的响应。

    数据面视图改成 async 之后，测试不能再用 `view(request)` 拿响应
    （那只会得到一个 coroutine）。用 asyncio.run 起一个临时循环跑完它：
    测试里 `loop_offload.run_db` 会短路成同线程执行，所以不会跨循环持有连接。
    """
    return asyncio.run(view(*args, **kwargs))


async def resolve(value):
    """视图返回值可能是响应对象，也可能是协程。

    数据面视图已改 async，管理端 DRF 视图仍是同步的——同一个测试 helper
    会同时遇到两者，所以不能无条件 await（await 一个 Response 会抛
    "'Response' object can't be awaited"）。
    """
    import inspect
    return await value if inspect.isawaitable(value) else value
