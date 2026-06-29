"""调试：检查 daemon 子进程是否存活
运行：D:\QuenPaw\python.exe tests/debug/daemon_alive_check.py
"""
import asyncio, subprocess, sys, os, time, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import aiohttp, urllib.request

PORT, DPORT = "13458", "13459"
BASE = "http://127.0.0.1:" + PORT

async def main():
    env = {**os.environ, "PORT": PORT, "DAEMON2ACP_MODE": "proxy",
           "DAEMON2ACP_AUTO_START": "1", "DAEMON2ACP_DAEMON_PORT": DPORT, "LOG_LEVEL": "INFO"}
    proc = subprocess.Popen([sys.executable, "server.py"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # 等待就绪
    for i in range(20):
        await asyncio.sleep(1)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(BASE + "/health", timeout=aiohttp.ClientTimeout(total=3)) as r:
                    b = await r.json()
                    if b.get("mode") == "proxy":
                        print("daemon2acp ready, daemon=%s" % json.dumps(b.get("daemon",{}), ensure_ascii=False) if True else "")
                        break
        except: pass

    # 等几秒看 daemon 是否还活着
    for i in range(5):
        await asyncio.sleep(1)
        try:
            r = urllib.request.Request("http://127.0.0.1:%s/health" % DPORT)
            with urllib.request.urlopen(r, timeout=3) as resp:
                print("[%ds] daemon /health: %s" % (i+1, resp.read().decode()[:100]))
        except Exception as e:
            print("[%ds] daemon /health: FAILED: %s" % (i+1, e))

    # 尝试直接对 daemon 发 chat
    print("\nDirect chat to daemon on port %s ..." % DPORT)
    try:
        r = urllib.request.Request(
            "http://127.0.0.1:%s/chat" % DPORT,
            data=json.dumps({"message": "hello"}).encode(),
            method="POST",
        )
        r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode(errors="replace")
        print("Direct daemon response (first 500): %s" % raw[:500].encode("utf-8",errors="replace").decode("utf-8"))
    except Exception as e:
        print("Direct daemon chat FAILED: %s" % e)

    # 通过 daemon2acp 发 chat
    print("\nChat via daemon2acp on port %s ..." % PORT)
    try:
        async with aiohttp.ClientSession() as http:
            timeout = aiohttp.ClientTimeout(total=60, sock_read=30)
            async with http.post(BASE + "/chat", json={"message": "hello"}, timeout=timeout) as resp:
                raw = await resp.text()
            print("daemon2acp response (first 500): %s" % raw[:500].encode("utf-8",errors="replace").decode("utf-8"))
    except Exception as e:
        print("daemon2acp chat FAILED: %s" % e)

    proc.terminate()
    try: proc.wait(timeout=5)
    except: proc.kill()

asyncio.run(main())
