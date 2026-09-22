"""测试全局配置：让版本号与判定依据等 INFO 日志在测试运行时可见。"""

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
