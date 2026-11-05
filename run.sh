#!/bin/sh
# 直接运行（服务器/NAS 裸机部署）
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo "首次运行：创建虚拟环境并安装依赖..."
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
fi

PORT="${DOUYIN_PORT:-8080}"
echo "启动服务: http://0.0.0.0:$PORT"
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers 1
