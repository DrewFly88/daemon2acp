"""端到端测试：server.py 自动拉起 daemon → 通过 daemon2acp 对话

运行：D:\QuenPaw\python.exe tests/e2e/auto_start_test.py
"""
import asyncio, subprocess, sys, os, json, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import aiohttp

async def chat(http, base, msg, sid):
    full = ""
    tout = aiohttp.ClientTimeout(total=120, sock_read=60)
    async with http.post(base + "/chat", json={"sessionId": sid, "message": msg}, timeout=tout) as resp:
        etype = ""
        while True:
            lb = await resp.content.readline()
            if not lb: break
            line = lb.decode("utf-8", errors="replace").strip()
            if not line: etype = ""; continue
            if line.startswith(":"): continue
            if line.startswith("event:"): etype = line[6:].strip(); continue
            if not line.startswith("data:"): continue
            try: d = json.loads(line[5:].strip())
            except: continue
            if etype == "text": full += d.get("text", "")
            elif etype == "turn_end": break
    return full.strip()

async def main():
    print("=" * 50)
    print("  Auto-start daemon E2E test")
    print("=" * 50)

    env = {**os.environ,
        "PORT": "13458", "DAEMON2ACP_MODE": "proxy",
        "DAEMON2ACP_AUTO_START": "1", "DAEMON2ACP_DAEMON_PORT": "13459",
        "LOG_LEVEL": "WARNING"}
    proc = subprocess.Popen([sys.executable, "server.py"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:13458"

    # 等待就绪
    for i in range(20):
        await asyncio.sleep(1)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(base + "/health", timeout=aiohttp.ClientTimeout(total=3)) as r:
                    b = await r.json()
                    print("    [%ds] health: %s" % (i+1, json.dumps(b, ensure_ascii=False)[:200]))
                    if b.get("mode") == "proxy" and b.get("daemon", {}).get("status") == "ok":
                        print("    Ready!")
                        break
        except Exception as e:
            print("    [%ds] health FAIL: %s" % (i+1, str(e)[:80]))
    else:
        print("TIMEOUT")
        try:
            err = proc.stderr.read1(3000).decode(errors="replace")
            print("Server stderr:\n%s" % err[-2000:])
        except: pass
        proc.kill(); return

    async with aiohttp.ClientSession() as http:
        async with http.post(base + "/acp", json={"jsonrpc":"2.0","id":1,"method":"session/new"}) as r:
            sid = (await r.json())["result"]["session"]["id"]
        print("Session: %s..." % sid[:8])

        # 对话
        print("\n--- Chat via daemon2acp (auto-started daemon) ---")
        # 先检查 daemon 是否还活着
        try:
            async with http.get("http://127.0.0.1:13459/health", timeout=aiohttp.ClientTimeout(total=2)) as r:
                print("    daemon /health: %s" % (await r.text())[:80])
        except Exception as e:
            print("    daemon /health FAIL: %s" % e)
        tout = aiohttp.ClientTimeout(total=120, sock_read=60)
        async with http.post(base + "/chat", json={"sessionId": sid, "message": "hello"}, timeout=tout) as resp:
            raw = await resp.text()
        print("Raw SSE (first 500 chars):")
        print(raw[:500])

    proc.terminate()
    try: proc.wait(timeout=5)
    except: proc.kill()
    print("\nDone.")

asyncio.run(main())
