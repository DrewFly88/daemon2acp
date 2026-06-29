"""2轮对话测试 — proxy模式，验证上下文连续性
运行：D:\QuenPaw\python.exe tests/e2e/quick_test.py
"""
import asyncio, subprocess, sys, os, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import aiohttp

DAEMON_PORT, D2ACP_PORT = "13459", "13458"

async def chat(http, msg, sid):
    full = ""
    tout = aiohttp.ClientTimeout(total=180, sock_read=90)
    async with http.post(f"http://127.0.0.1:{D2ACP_PORT}/chat",
                         json={"sessionId": sid, "message": msg}, timeout=tout) as resp:
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
    # 启动 daemon
    dp = subprocess.Popen([r"D:\AtomCode\atomcode.exe", "daemon",
         "--port", DAEMON_PORT, "--no-telemetry"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for i in range(15):
        await asyncio.sleep(1)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"http://127.0.0.1:{DAEMON_PORT}/health",
                                 timeout=aiohttp.ClientTimeout(total=2)) as r:
                    if r.status == 200: print("Daemon ready"); break
        except: pass
    else: print("Daemon TIMEOUT"); dp.kill(); return

    # 启动 daemon2acp
    env = {**os.environ, "PORT": D2ACP_PORT, "DAEMON2ACP_MODE": "proxy",
           "DAEMON2ACP_AUTO_START": "0",
           "DAEMON2ACP_DAEMON_URL": f"http://127.0.0.1:{DAEMON_PORT}",
           "LOG_LEVEL": "WARNING"}
    ap = subprocess.Popen([sys.executable, "server.py"], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for i in range(10):
        await asyncio.sleep(1)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"http://127.0.0.1:{D2ACP_PORT}/health",
                                 timeout=aiohttp.ClientTimeout(total=2)) as r:
                    b = await r.json()
                    if b.get("mode") == "proxy": print("daemon2acp ready"); break
        except: pass
    else: print("daemon2acp TIMEOUT"); ap.kill(); dp.kill(); return

    async with aiohttp.ClientSession() as http:
        async with http.post(f"http://127.0.0.1:{D2ACP_PORT}/acp",
                             json={"jsonrpc":"2.0","id":1,"method":"session/new"}) as r:
            sid = (await r.json())["result"]["session"]["id"]
        print(f"Session: {sid[:8]}...")

        # Round 1
        print("\n--- Round 1: Create file ---")
        t = await chat(http,
            '请在 D:\\代码\\ 下创建 test_acp.md，内容为：# ACP Test\n\nHello from daemon2acp.\n\n创建后请确认。',
            sid)
        print(f"AI: {t[:300]}")

        # Round 2
        print("\n--- Round 2: Read back (verify context) ---")
        t = await chat(http,
            '请读取 D:\\代码\\test_acp.md 的内容并显示给我，确认文件存在且内容正确。',
            sid)
        print(f"AI: {t[:300]}")

    # 验证
    fp = r"D:\代码\test_acp.md"
    print("\n--- Local file check ---")
    if os.path.isfile(fp):
        with open(fp, "r", encoding="utf-8") as f: c = f.read()
        print(f"EXISTS ({len(c)} chars): {c.strip()[:200]}")
        ok = "ACP Test" in c and "Hello" in c
        print(f"Content check: {'PASS' if ok else 'FAIL'}")
    else:
        print("NOT FOUND")

    # 清理
    ap.terminate(); dp.terminate()
    try: ap.wait(timeout=3)
    except: ap.kill()
    try: dp.wait(timeout=3)
    except: dp.kill()
    print("\nDone.")

asyncio.run(main())
