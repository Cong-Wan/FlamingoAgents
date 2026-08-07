'''
Author: wilbur
Version: 1.1
Date: 2026-08-05
Description: 启动入口：校验 FLAMINGO_WEB_TOKEN（未设置则报错退出），读 FLAMINGO_WEB_HOST/PORT，单 worker 起 uvicorn。
            v1.1 随包改名 flamingoWeb → webApp 调整为从 webApp.backend.server 导入 app。
'''

import os
import sys

import uvicorn


def main() -> None:
    token = os.environ.get('FLAMINGO_WEB_TOKEN', '')
    if not token:
        print('启动失败：未设置环境变量 FLAMINGO_WEB_TOKEN（静态 Bearer Token 强制显式配置）。', file=sys.stderr)
        sys.exit(1)
    host = os.environ.get('FLAMINGO_WEB_HOST', '0.0.0.0')
    port = int(os.environ.get('FLAMINGO_WEB_PORT', '8787'))
    from webApp.backend.server import app
    # agent 缓存、活跃流登记、索引文件锁全是进程内内存态，多 worker 会各自持有一份导致状态错乱，写死单 worker。
    uvicorn.run(app, host=host, port=port, workers=1)


if __name__ == '__main__':
    main()
