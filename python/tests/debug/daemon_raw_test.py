"""直接测试 atomcode-daemon 的 /chat 端点
运行：D:\QuenPaw\python.exe tests/debug/daemon_raw_test.py
"""
import subprocess, time, json, sys, os, urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 启动 atomcode daemon
print("[1] Starting atomcode daemon on port 13459 ...")
proc = subprocess.Popen(
    [r"D:\AtomCode\atomcode.exe", "daemon", "--port", "13459", "--no-telemetry"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)

for i in range(15):
    time.sleep(1)
    try:
        r = urllib.request.Request("http://127.0.0.1:13459/health")
        with urllib.request.urlopen(r, timeout=3) as resp:
            if resp.status == 200:
                print("    Daemon ready!")
                break
    except:
        pass
else:
    print("    TIMEOUT")
    proc.kill()
    sys.exit(1)

# 测试 /chat
print("\n[2] POST /chat  (message: 'hello')")
try:
    r = urllib.request.Request(
        "http://127.0.0.1:13459/chat",
        data=json.dumps({"message": "hello"}).encode(),
        method="POST",
    )
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=60) as resp:
        raw = resp.read().decode(errors="replace")
    print("    Status: %s" % resp.status)
    print("    Content-Type: %s" % resp.headers.get("Content-Type"))
    print("    SSE events (first 3000 chars):")
    print(raw[:3000])
except Exception as e:
    print("    ERROR: %s" % e)

proc.terminate()
try:
    proc.wait(timeout=5)
except:
    proc.kill()
print("\nDone.")
