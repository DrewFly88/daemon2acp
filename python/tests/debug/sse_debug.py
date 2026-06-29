"""调试：抓 daemon2acp /chat 的原始 SSE 输出
运行：python tests/debug/sse_debug.py
"""
import asyncio, subprocess, sys, os, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import aiohttp

PORT, DPORT = "13458", "13459"
BASE = "http://127.0.0.1:" + PORT

async def main():
    env = {**os.environ, "PORT": PORT, "DAEMON2ACP_MODE": "proxy",
           "DAEMON2ACP_AUTO_START": "1", "DAEMON2ACP_DAEMON_PORT": DPORT, "LOG_LEVEL": "WARNING"}
    proc = subprocess.Popen([sys.executable, "server.py"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    for i in range(20):
        await asyncio.sleep(1)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(BASE + "/health", timeout=aiohttp.ClientTimeout(total=3)) as r:
                    b = await r.json()
                    if b.get("mode") == "proxy": break
        except: pass

    async with aiohttp.ClientSession() as http:
        async with http.post(BASE + "/acp", json={"jsonrpc":"2.0","id":1,"method":"session/new"}) as r:
            sid = (await r.json())["result"]["session"]["id"]

        print("Sending chat ...")
        timeout = aiohttp.ClientTimeout(total=120, sock_read=60)
        async with http.post(BASE + "/chat", json={"sessionId": sid, "message": "hello"}, timeout=timeout) as resp:
            print("Status:", resp.status)
            print("Content-Type:", resp.headers.get("Content-Type"))
            print("Raw SSE (first 1500 chars):\n")
            count = 0
            while True:
                line_bytes = await resp.content.readline()
                if not line_bytes: break
                line = line_bytes.decode("utf-8", errors="replace")
                print(repr(line))
                count += 1
                if count > 30: break

    proc.terminate()
    try: proc.wait(timeout=5)
    except: proc.kill()

asyncio.run(main())
