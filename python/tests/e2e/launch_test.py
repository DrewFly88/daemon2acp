"""启动测试 — 启动 server.py，发送请求，查看回复，然后关闭（mock 模式）
运行：D:\QuenPaw\python.exe tests/e2e/launch_test.py
"""
import subprocess, time, json, sys, os, urllib.request, urllib.error

os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# 1. 启动服务（后台子进程）
print("[1] Starting server on port 13458 ...")
proc = subprocess.Popen(
    [sys.executable, "server.py"],
    env={**os.environ, "PORT": "13458", "DAEMON2ACP_MODE": "mock"},
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)

# 2. 等待就绪
time.sleep(2)
base = "http://127.0.0.1:13458"

def req(method, path, data=None):
    url = base + path
    body = json.dumps(data).encode() if data else None
    r = urllib.request.Request(url, data=body, method=method)
    if body:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return resp.status, json.loads(resp.read())
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return -1, str(e)

# 3. 健康检查
print("\n[2] GET /health")
status, body = req("GET", "/health")
print(f"    Status: {status}")
print(f"    Body:   {json.dumps(body, indent=2, ensure_ascii=False)}")

# 4. ACP initialize
print("\n[3] POST /acp  (initialize)")
status, body = req("POST", "/acp", {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": 1}
})
print(f"    Status: {status}")
print(f"    Body:   {json.dumps(body, indent=2, ensure_ascii=False)}")

# 5. session/new
print("\n[4] POST /acp  (session/new)")
status, body = req("POST", "/acp", {"jsonrpc": "2.0", "id": 2, "method": "session/new"})
print(f"    Status: {status}")
sid = body.get("result", {}).get("session", {}).get("id", "?")
print(f"    Session ID: {sid}")

# 6. POST /chat (SSE 流)
print(f"\n[5] POST /chat  (message: '你好，请介绍一下你自己')")
try:
    r = urllib.request.Request(
        base + "/chat",
        data=json.dumps({"sessionId": sid, "message": "你好，请介绍一下你自己"}).encode(),
        method="POST",
    )
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=15) as resp:
        raw = resp.read().decode(errors="replace")
    print(f"    Status: {resp.status}")
    print(f"    SSE Events:")
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
            try:
                d = json.loads(data_str)
                # 精简输出
                if event_type == "text":
                    print(f"      [{event_type}] {d.get('text', '')[:80]}")
                elif event_type == "reasoning":
                    print(f"      [{event_type}] {d.get('text', '')[:80]}")
                elif event_type == "tool_use":
                    print(f"      [{event_type}] name={d.get('name')}, input={json.dumps(d.get('input',{}))[:60]}")
                elif event_type == "tool_result":
                    print(f"      [{event_type}] content={json.dumps(d.get('content',''))[:60]}")
                elif event_type == "turn_end":
                    print(f"      [{event_type}] stopReason={d.get('stopReason')}")
                else:
                    print(f"      [{event_type}] {json.dumps(d)[:80]}")
            except json.JSONDecodeError:
                print(f"      [{event_type}] {data_str[:80]}")
except Exception as e:
    print(f"    ERROR: {e}")

# 7. 关闭服务
print("\n[6] Shutting down server ...")
proc.terminate()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
print("    Server stopped.")
print("\nDone!")
