import os
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# asgiref 的 thread-sensitive 同步视图执行器默认只有
# min(32, cpu+4) 个线程；本项目的 admin 列表 / dashboard / 代理测速都是
# "同步视图 + SQLite" 的短任务，几十并发下线程池排队是延迟大头。
# 默认抬到 64（可用 ASGI_THREADS 环境变量覆盖）。竞速/流式路径本身跑在
# 独立事件循环里，不受此线程池约束。
os.environ.setdefault("ASGI_THREADS", "64")

application = get_asgi_application()
