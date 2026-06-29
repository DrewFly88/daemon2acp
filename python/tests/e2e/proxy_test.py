"""Proxy 模式启动测试 — 启动 server.py (proxy)，发送请求，查看回复
运行：D:\QuenPaw\python.exe tests/e2e/proxy_test.py
"""
import subprocess, time, json, sys, os, urllib.request, urllib.error

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

PORT = "13458"
DAEMON_PORT = "13459"

print("[1] Starting server in PROXY mode on port %s ..." % PORT)
env = {**os.environ,
    "PORT": PORT,
    "DAEMON2ACP_MODE": "proxy",
    "DAEMON2ACP_AUTO_START": "1",
    "DAEMON2ACP_DAEMON_PORT": DAEMON_PORT,
    "LOG_LEVEL": "INFO",
}
proc = subprocess.Popen(
    [sys.executable, "server.py"],
    env=env,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)

base = "http://127.0.0.1:" + PORT

def req(method, path, data=None, timeout_sec=10):
    url = base + path
    body = json.dumps(data).encode() if data else None
    r = urllib.request.Request(url, data=body, method=method)
    if body:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=timeout_sec) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return resp.status, json.loads(resp.read())
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return -1, str(e)

print("[2] Waiting for server + atomcode daemon ...")
for i in range(20):
    time.sleep(1)
    status, body = req("GET", "/health")
    if status == 200 and body.get("mode") == "proxy":
        print("    Ready! mode=proxy, daemon=%s" % json.dumps(body.get("daemon",{}), ensure_ascii=False))
        break
    if i == 19:
        print("    TIMEOUT")
        proc.kill()
        sys.exit(1)

print("\n[3] POST /acp  (session/new)")
status, body = req("POST", "/acp", {"jsonrpc": "2.0", "id": 2, "method": "session/new"})
sid = body.get("result", {}).get("session", {}).get("id", "?")
print("    Session ID: %s" % sid)

print("\n[4] POST /chat  (message: '你好，请用一句话介绍你自己')")
try:
    r = urllib.request.Request(
        base + "/chat",
        data=json.dumps({"sessionId": sid, "message": "你好，请用一句话介绍你自己"}).encode(),
        method="POST",
    )
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=60) as resp:
        raw = resp.read().decode(errors="replace")
    print("    Status: %s" % resp.status)
    print("    SSE Events:")
    event_count = 0
    full_text = ""
    for block in raw.split("\n\n"):
        lines = block.strip().split("\n")
        if not lines or not lines[0]:
            continue
        event_type = ""
        data_str = ""
        for line in lines:
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_str = line[5:].strip()
        if data_str:
            event_count += 1
            try:
                d = json.loads(data_str)
                if event_type == "text":
                    full_text += d.get("text", "")
                    print("      [%d text] %s" % (event_count, d.get("text", "")[:100]))
                elif event_type == "reasoning":
                    print("      [%d reasoning] %s" % (event_count, d.get("text", "")[:100]))
                elif event_type == "tool_use":
                    print("      [%d tool_use] name=%s" % (event_count, d.get("name")))
                elif event_type == "tool_result":
                    print("      [%d tool_result]" % event_count)
                elif event_type == "turn_end":
                    print("      [%d turn_end] stopReason=%s" % (event_count, d.get("stopReason")))
                elif event_type == "error":
                    print("      [%d error] %s" % (event_count, d.get("message","")[:100]))
                else:
                    print("      [%d %s] %s" % (event_count, event_type, json.dumps(d)[:80]))
            except json.JSONDecodeError:
                print("      [%d %s] %s" % (event_count, event_type, data_str[:80]))
    if event_count == 0:
        print("      (no events) Raw: %s" % raw[:500])
    if full_text:
        print("\n    Full AI response text:\n    ---\n    %s\n    ---" % full_text)
except Exception as e:
    print("    ERROR: %s" % e)

print("\n[5] Shutting down ...")
proc.terminate()
try:
    proc.wait(timeout=8)
except:
    proc.kill()
print("    Done.")
