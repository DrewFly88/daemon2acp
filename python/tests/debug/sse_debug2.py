"""简单调试：一轮对话，打印原始 SSE
运行：D:\QuenPaw\python.exe tests/debug/sse_debug2.py
"""
import asyncio, subprocess, sys, os, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import aiohttp

async def main():
    env = {**os.environ, "PORT": "13458", "DAEMON2ACP_MODE": "proxy",
           "DAEMON2ACP_AUTO_START": "1", "DAEMON2ACP_DAEMON_PORT": "13459", "LOG_LEVEL": "INFO"}
    proc = subprocess.Popen([sys.executable, "server.py"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    for i in range(20):
        await asyncio.sleep(1)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("http://127.0.0.1:13458/health", timeout=aiohttp.ClientTimeout(total=3)) as r:
                    b = await r.json()
                    if b.get("mode") == "proxy":
                        print("Ready! daemon=%s" % b.get("daemon",{}).get("version","?"))
                        break
        except: pass

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120, sock_read=60)) as http:
        # chat
        print("\n--- POST /chat ---")
        async with http.post("http://127.0.0.1:13458/chat",
                             json={"message": "say hello"}) as resp:
            print("Status:", resp.status)
            raw = b""
            for _ in range(50):
                chunk = await resp.content.read(4096)
                if not chunk: break
                raw += chunk
                # 看看有没有 turn_end
                if b"turn_end" in raw: break
            text = raw.decode("utf-8", errors="replace")
            print("Raw SSE (%d chars):" % len(text))
            print(text[:2000])

    proc.terminate()
    try: proc.wait(timeout=5)
    except: proc.kill()

asyncio.run(main())
