"""把项目部署到飞牛 fnOS（venv + systemd），可重复执行（幂等）。

用法：
    FNOS_PASS='密码' python scripts/deploy_fnos.py
可选环境变量：
    FNOS_HOST / FNOS_PORT / FNOS_USER / FNOS_PASS
    FNOS_APP_DIR  默认 /vol1/1000/programs/douyin-dl
    FNOS_PORT_APP 默认 8080
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
HOST = os.environ.get("FNOS_HOST", "192.168.1.10")
SSH_PORT = int(os.environ.get("FNOS_PORT", "22"))
USER = os.environ.get("FNOS_USER", "nasuser")
PASSWORD = os.environ.get("FNOS_PASS", "")
APP_DIR = os.environ.get("FNOS_APP_DIR", "/vol1/1000/programs/douyin-dl")
WEB_PORT = os.environ.get("FNOS_PORT_APP", "8080")
SERVICE = "douyin-dl"
PIP_MIRROR = "https://mirrors.aliyun.com/pypi/simple/"

INCLUDE = ["core", "app", "cli.py", "requirements.txt"]
EXCLUDE = {"__pycache__", ".venv", "data", "dist", ".tmp", ".git", "tests"}


def pack() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in INCLUDE:
            src = ROOT / name
            if src.is_file():
                tar.add(src, arcname=name)
                continue
            for path in sorted(src.rglob("*")):
                if path.is_dir() or any(part in EXCLUDE for part in path.parts):
                    continue
                if path.suffix == ".pyc":
                    continue
                tar.add(path, arcname=str(path.relative_to(ROOT)).replace("\\", "/"))
    return buf.getvalue()


class Remote:
    def __init__(self, client: paramiko.SSHClient):
        self.client = client

    def run(self, cmd: str, sudo: bool = False, timeout: int = 900, quiet: bool = False) -> tuple[str, str, int]:
        full = f"cd /tmp 2>/dev/null; {cmd}"
        if sudo:
            full = f"cd /tmp 2>/dev/null; echo '{PASSWORD}' | sudo -S -p '' bash -c {shell_quote(cmd)}"
        stdin, stdout, stderr = self.client.exec_command(full, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        if not quiet:
            for line in (out.strip().splitlines() or [])[-12:]:
                print(f"    {line}")
            for line in (err.strip().splitlines() or [])[-6:]:
                if "Could not chdir" in line or "sudo: unable to resolve" in line:
                    continue
                print(f"    ! {line}")
        return out, err, rc


def shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def main() -> int:
    if not PASSWORD:
        print("缺少 FNOS_PASS 环境变量")
        return 2

    archive = pack()
    print(f"本地源码包 {len(archive) / 1024:.1f} KB")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=SSH_PORT, username=USER, password=PASSWORD, timeout=20)
    remote = Remote(client)

    try:
        print("\n[1/6] 上传源码")
        sftp = client.open_sftp()
        sftp.putfo(io.BytesIO(archive), "/tmp/douyin-dl-src.tar.gz")
        sftp.close()
        print("    已上传 /tmp/douyin-dl-src.tar.gz")

        print("\n[2/6] 创建目录并解包")
        remote.run(
            f"mkdir -p {APP_DIR}/app {APP_DIR}/data {APP_DIR}/downloads && "
            f"tar xzf /tmp/douyin-dl-src.tar.gz -C {APP_DIR}/app",
            sudo=True,
        )
        remote.run(f"chown -R {USER}:Users {APP_DIR}", sudo=True)

        print("\n[3/6] 创建虚拟环境并安装依赖（首次约 1-3 分钟）")
        _, _, rc = remote.run(
            f"cd {APP_DIR}/app && "
            f"[ -x .venv/bin/python ] || python3 -m venv .venv; "
            f".venv/bin/python -m pip install -q --upgrade pip -i {PIP_MIRROR}; "
            f".venv/bin/python -m pip install -q -r requirements.txt -i {PIP_MIRROR} && "
            f".venv/bin/python -c 'import fastapi,uvicorn,httpx;print(\"deps ok\")'",
            timeout=1200,
        )
        if rc != 0:
            print("    依赖安装失败")
            return 1

        print("\n[4/6] 写入 systemd 服务")
        unit = f"""[Unit]
Description=Douyin Batch Downloader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={USER}
Group=Users
WorkingDirectory={APP_DIR}/app
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai
Environment=HOME={APP_DIR}/data
Environment=DOUYIN_DATA_DIR={APP_DIR}/data
Environment=DOUYIN_DOWNLOAD_ROOT={APP_DIR}/downloads
ExecStart={APP_DIR}/app/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port {WEB_PORT} --workers 1
Restart=always
RestartSec=5
StandardOutput=append:{APP_DIR}/data/service.log
StandardError=append:{APP_DIR}/data/service.log

[Install]
WantedBy=multi-user.target
"""
        sftp = client.open_sftp()
        with sftp.open("/tmp/douyin-dl.service", "w") as fp:
            fp.write(unit)
        sftp.close()
        remote.run(
            f"mv /tmp/douyin-dl.service /etc/systemd/system/{SERVICE}.service && "
            f"chmod 644 /etc/systemd/system/{SERVICE}.service && "
            f"systemctl daemon-reload && systemctl enable {SERVICE} && systemctl restart {SERVICE}",
            sudo=True,
        )

        print("\n[5/6] 等待服务启动")
        healthy = False
        for _ in range(20):
            time.sleep(2)
            out, _, rc = remote.run(f"curl -fsS -m 3 http://127.0.0.1:{WEB_PORT}/api/health", quiet=True)
            if rc == 0 and '"ok":true' in out:
                healthy = True
                print(f"    本地健康检查通过: {out.strip()}")
                break
        if not healthy:
            print("    服务未就绪，最近日志：")
            remote.run(f"tail -n 30 {APP_DIR}/data/service.log; systemctl status {SERVICE} --no-pager -l | head -20")

        print("\n[6/6] 状态汇总")
        remote.run(f"systemctl is-active {SERVICE}; systemctl is-enabled {SERVICE}")
        remote.run(f"du -sh {APP_DIR}/app/.venv {APP_DIR} 2>/dev/null; df -h /vol1 | tail -1")

        out, _, _ = remote.run(f"curl -sS -m 5 http://127.0.0.1:{WEB_PORT}/api/state | head -c 200", quiet=True)
        print(f"    API 抽样: {out.strip()[:180]}")
        print(f"\n部署完成：http://{HOST}:{WEB_PORT}/")
        print(f"程序目录：{APP_DIR}/app")
        print(f"数据目录：{APP_DIR}/data")
        print(f"下载目录：{APP_DIR}/downloads")
        return 0 if healthy else 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
