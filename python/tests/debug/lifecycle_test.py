"""daemon 生命周期测试：启动 → 存活 → 对话 → 关闭 → 确认退出
运行：D:\QuenPaw\python.exe tests/debug/lifecycle_test.py
"""
import subprocess, time, json, sys, os, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.daemon_launcher import DaemonLauncher

def check_health(port):
    try:
        r = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(r, timeout=2) as resp:
            return resp.status == 200
    except:
        return False

def check_port(port):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except:
        return False

print("=" * 50)
print("  Daemon Lifecycle Test")
print("=" * 50)

launcher = DaemonLauncher(port=13459)

# ---- 1. 启动 ----
print("\n[1] start_sync()")
launcher.start_sync(timeout=15)
pid = launcher.pid
print("    pid=%s is_running=%s health=%s" % (pid, launcher.is_running, check_health(13459)))
assert launcher.is_running, "daemon should be running"
assert check_health(13459), "daemon /health should be 200"

# ---- 2. 存活 5 秒 ----
print("\n[2] alive for 5s ...")
for i in range(5):
    time.sleep(1)
    poll_rc = launcher._process.poll() if launcher._process else "N/A"
    print("    [%ds] is_running=%s health=%s poll=%s" % (
        i+1, launcher.is_running, check_health(13459), poll_rc))
assert launcher.is_running, "daemon should still be running"

# ---- 3. 对话（直接对 daemon，绕过 proxy） ----
print("\n[3] direct chat to daemon ...")
try:
    r = urllib.request.Request(
        "http://127.0.0.1:13459/chat",
        data=json.dumps({"message": "hello"}).encode(),
        method="POST",
    )
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=15) as resp:
        raw = resp.read().decode(errors="replace")
    # 解析 SSE 提取文本
    text = ""
    for line in raw.split("\n"):
        if not line.startswith("data:"): continue
        try:
            d = json.loads(line[5:])
            if d.get("type") == "text":
                text += d.get("content", d.get("text", ""))
        except: pass
    print("    AI: %s" % text[:200])
    print("    is_running=%s after chat" % launcher.is_running)
except Exception as e:
    print("    FAILED: %s" % e)

# ---- 4. 关闭 ----
print("\n[4] stop_sync()")
launcher.stop_sync(timeout=5)
time.sleep(1)
print("    is_running=%s health=%s port_listening=%s" % (
    launcher.is_running, check_health(13459), check_port(13459)))

# ---- 5. 确认进程已退出 ----
print("\n[5] verify process exited")
if launcher._process is not None:
    rc = launcher._process.poll()
    print("    poll()=%s (None=still running)" % rc)
else:
    print("    _process=None (cleaned up)")

assert not launcher.is_running, "daemon should NOT be running after stop"
assert not check_health(13459), "daemon /health should fail after stop"
assert not check_port(13459), "port should be free after stop"

# ---- 6. 重启测试 ----
print("\n[6] restart_sync()")
launcher.restart_sync(timeout=15)
print("    pid=%s is_running=%s health=%s" % (launcher.pid, launcher.is_running, check_health(13459)))
assert launcher.is_running, "daemon should be running after restart"

# 清理
launcher.stop_sync()
time.sleep(1)
print("\n[7] final cleanup: is_running=%s" % launcher.is_running)

print("\n" + "=" * 50)
print("  ALL LIFECYCLE TESTS PASSED")
print("=" * 50)
