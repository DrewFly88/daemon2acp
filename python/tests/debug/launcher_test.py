"""测试 DaemonLauncher 自动拉起 daemon（使用旧的 async start() API）
运行：D:\QuenPaw\python.exe tests/debug/launcher_test.py

注意：此脚本使用旧的 async start()/stop() API，保留作历史参考。
当前推荐用 tests/debug/lifecycle_test.py（sync API）。
"""
import asyncio, sys, os, json, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.daemon_launcher import DaemonLauncher

async def main():
    launcher = DaemonLauncher(host="127.0.0.1", port=13459)

    print("[1] Starting daemon via DaemonLauncher ...")
    try:
        launcher.start_sync(timeout=15.0)
        print("    Started! pid=%s, url=%s" % (launcher.pid, launcher.base_url))
    except Exception as e:
        print("    FAILED: %s" % e)
        return

    # 等几秒看是否还活着
    for i in range(8):
        await asyncio.sleep(1)
        alive = launcher.is_running
        import urllib.request
        try:
            r = urllib.request.Request("http://127.0.0.1:13459/health")
            with urllib.request.urlopen(r, timeout=2) as resp:
                h = resp.read().decode()[:80]
            print("    [%ds] pid=%s alive=%s health=%s" % (i+1, launcher.pid, alive, h))
        except Exception as e:
            print("    [%ds] pid=%s alive=%s health=FAIL: %s" % (i+1, launcher.pid, alive, str(e)[:60]))

    # 尝试发 chat
    print("\n[2] POST /chat ...")
    import aiohttp
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post("http://127.0.0.1:13459/chat",
                                 json={"message": "hello"},
                                 timeout=aiohttp.ClientTimeout(total=60, sock_read=45)) as resp:
                text = ""
                while True:
                    lb = await resp.content.readline()
                    if not lb: break
                    line = lb.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":"): continue
                    if not line.startswith("data:"): continue
                    try:
                        d = json.loads(line[5:].strip())
                        if d.get("type") == "text":
                            text += d.get("content", d.get("text", ""))
                        elif d.get("type") == "done":
                            break
                    except: pass
                print("    AI response: %s" % text[:200])
    except Exception as e:
        print("    FAILED: %s" % e)

    print("\n[3] Stopping daemon ...")
    launcher.stop_sync()
    print("    Done.")

asyncio.run(main())
