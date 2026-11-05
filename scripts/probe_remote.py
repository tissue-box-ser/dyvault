"""探测远程飞牛环境：系统、Python、Docker、目录、权限。"""

import os
import sys

import paramiko

HOST = os.environ.get("FNOS_HOST", "192.168.1.10")
PORT = int(os.environ.get("FNOS_PORT", "22"))
USER = os.environ.get("FNOS_USER", "nasuser")
PASSWORD = os.environ.get("FNOS_PASS", "")

CMDS = [
    ("系统", "uname -a"),
    ("发行版", "cat /etc/os-release 2>/dev/null | head -4"),
    ("当前用户", "id"),
    ("Python", "python3 --version 2>&1; which python3"),
    ("Docker", "docker --version 2>&1; docker compose version 2>&1 | head -2"),
    ("内存", "free -m | head -2"),
    ("磁盘", "df -h / /vol1 /vol2 2>/dev/null | grep -v Filesystem"),
    ("存储卷", "ls -d /vol* 2>/dev/null"),
    ("家目录", "echo $HOME; ls -la $HOME | head -10"),
    ("系统版本", "cat /etc/trim_version 2>/dev/null; cat /etc/version 2>/dev/null; hostname"),
]


def main() -> int:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(HOST, port=PORT, username=USER, password=PASSWORD, timeout=20)
    except Exception as exc:
        print(f"SSH 连接失败: {type(exc).__name__}: {exc}")
        return 1

    for name, cmd in CMDS:
        stdin, stdout, stderr = client.exec_command(cmd, timeout=25)
        out = stdout.read().decode("utf-8", "replace").strip()
        err = stderr.read().decode("utf-8", "replace").strip()
        print(f"=== {name} ===")
        print(out or "(空)")
        if err and "not found" not in err:
            print(f"[stderr] {err}")
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
