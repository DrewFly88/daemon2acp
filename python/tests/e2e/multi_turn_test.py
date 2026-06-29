"""多轮对话测试 — 手动启动 daemon，daemon2acp 连接已有 daemon

5轮对话：创建文件 → 追加内容 → 修改内容 → 读回验证 → 删除文件
运行：D:\QuenPaw\python.exe tests/e2e/multi_turn_test.py
"""
import asyncio, subprocess, sys, os, json, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import aiohttp

DAEMON_PORT = "13459"
D2ACP_PORT = "13458"
DAEMON_BASE = "http://127.0.0.1:" + DAEMON_PORT
D2ACP_BASE = "http://127.0.0.1:" + D2ACP_PORT

async def chat(http, message, sid):
    """通过 daemon2acp /chat 发消息，收集完整 AI 文本"""
    full_text = ""
    timeout = aiohttp.ClientTimeout(total=300, sock_read=120)
    async with http.post(D2ACP_BASE + "/chat",
                         json={"sessionId": sid, "message": message},
                         timeout=timeout) as resp:
        event_type = ""
        while True:
            line_bytes = await resp.content.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                event_type = ""
                continue
            if line.startswith(":"): continue
            if line.startswith("event:"):
                event_type = line[6:].strip()
                continue
            if not line.startswith("data:"): continue
            data_str = line[5:].strip()
            try:
                d = json.loads(data_str)
            except: continue
            if event_type == "text":
                full_text += d.get("text", "")
            elif event_type == "turn_end":
                break
    return full_text.strip()

async def main():
    print("=" * 60)
    print("  Multi-turn Proxy Test (manual daemon)")
    print("=" * 60)

    # 1. 手动启动 atomcode daemon
    print("\n[0a] Starting atomcode daemon on port %s ..." % DAEMON_PORT)
    daemon_proc = subprocess.Popen(
        [r"D:\AtomCode\atomcode.exe", "daemon",
         "--port", DAEMON_PORT, "--no-telemetry"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for i in range(15):
        await asyncio.sleep(1)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(DAEMON_BASE + "/health",
                                 timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status == 200:
                        print("    Daemon ready!")
                        break
        except: pass
    else:
        print("    TIMEOUT"); daemon_proc.kill(); return

    # 2. 启动 daemon2acp (proxy, 不自动拉起)
    print("[0b] Starting daemon2acp on port %s (proxy, no auto-start) ..." % D2ACP_PORT)
    env = {**os.environ,
        "PORT": D2ACP_PORT,
        "DAEMON2ACP_MODE": "proxy",
        "DAEMON2ACP_AUTO_START": "0",
        "DAEMON2ACP_DAEMON_URL": DAEMON_BASE,
        "LOG_LEVEL": "WARNING",
    }
    d2acp_proc = subprocess.Popen(
        [sys.executable, "server.py"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    for i in range(10):
        await asyncio.sleep(1)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(D2ACP_BASE + "/health",
                                 timeout=aiohttp.ClientTimeout(total=3)) as r:
                    b = await r.json()
                    if b.get("mode") == "proxy":
                        print("    daemon2acp ready! mode=proxy")
                        break
        except: pass
    else:
        print("    TIMEOUT"); d2acp_proc.kill(); daemon_proc.kill(); return

    async with aiohttp.ClientSession() as http:
        # 创建会话
        async with http.post(D2ACP_BASE + "/acp",
                             json={"jsonrpc":"2.0","id":1,"method":"session/new"}) as r:
            sid = (await r.json())["result"]["session"]["id"]
        print("    Session: %s" % sid)

        rounds = [
            ("[1] Create test_acp.md",
             '请在 D:\\代码\\ 目录下创建一个名为 test_acp.md 的文件，写入以下内容：\n\n# ACP Test\n\nThis is a test file created by daemon2acp.\n\n请确认创建成功。'),
            ("[2] Append content",
             '请在刚才创建的 test_acp.md 文件末尾追加以下内容：\n\n## Section 2\n\n- Item A\n- Item B\n- Item C\n\n请确认追加成功。'),
            ("[3] Modify content",
             '请把 test_acp.md 中的 "Item B" 改为 "Item B (modified)"，同时在 Section 2 下面加一行：\n\n> This line was added in round 3.\n\n请确认修改成功。'),
            ("[4] Read back & verify",
             '请读取 D:\\代码\\test_acp.md 的完整内容并显示给我。我需要确认之前所有的修改都正确保存了。'),
            ("[5] Delete file",
             '请删除刚才创建的 D:\\代码\\test_acp.md 文件，确认删除成功。'),
        ]

        for title, msg in rounds:
            print("\n" + "-" * 60)
            print(title)
            print("-" * 60)
            text = await chat(http, msg, sid)
            print("    AI: " + text[:500].replace("\n", "\n    "))

        # 本地文件验证
        fpath = r"D:\代码\test_acp.md"
        print("\n" + "=" * 60)
        print("  File verification")
        print("=" * 60)
        if os.path.isfile(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            print("  File exists (%d chars):" % len(content))
            print("  " + content.strip().replace("\n", "\n  "))
            checks = {
                "# ACP Test": "# ACP Test" in content,
                "Section 2": "Section 2" in content,
                "Item A": "Item A" in content,
                "Item B modified": "modified" in content,
                "Item C": "Item C" in content,
                "round 3": "round 3" in content.lower(),
            }
            print("\n  Checks:")
            all_ok = True
            for label, ok in checks.items():
                print("    [%s] %s" % ("OK" if ok else "MISS", label))
                if not ok: all_ok = False
            print("  >>> %s" % ("All checks PASSED!" if all_ok else "Some checks FAILED."))
        else:
            print("  File NOT found (deleted successfully).")

    # 清理
    print("\n[Cleanup]")
    d2acp_proc.terminate()
    try: d2acp_proc.wait(timeout=5)
    except: d2acp_proc.kill()
    # 优雅关闭 daemon
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(DAEMON_BASE + "/shutdown",
                         timeout=aiohttp.ClientTimeout(total=3))
    except: pass
    try: daemon_proc.wait(timeout=5)
    except: daemon_proc.kill()
    print("  Done.")

asyncio.run(main())
