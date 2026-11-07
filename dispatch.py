#!/usr/bin/env python3
import json, os, sys, time, urllib.request, urllib.error, re
import home_fingerprint as hf

MCP_URL = "http://127.0.0.1:3000/mcp"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MACROS_FILE = os.path.expanduser("~/.config/termux-mcp/macros.json")
PKG_MAP_FILE = os.path.expanduser("~/.config/termux-mcp/app_packages.json")

def load_env_file(path):
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except FileNotFoundError: pass

load_env_file(os.path.expanduser("~/.config/termux-mcp/env"))
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

def load_pkg_map():
    try:
        with open(PKG_MAP_FILE, encoding="utf-8") as f: return json.load(f)
    except: return {}

def load_macros():
    try:
        with open(MACROS_FILE, encoding="utf-8") as f: return json.load(f)
    except: return {}

PROMPT_PREFIX = '''你是手机操作调度器。只输出 JSON 数组，不解释、不输出 markdown。
可用宏：
- search_bilibili(keyword)
- deepseek_new_chat()
- open_app(package_or_action)

规则：
1. 只要用户提到了“B站”、“bilibili”、“哔哩哔哩”并且包含“搜索”、“搜”、“听”，你**必须且只能**输出：`[{"tool": "search_bilibili", "args": {"keyword": "搜索词"}}]`。该宏会自动打开B站并完成搜索点击。
2. 如果用户要求打开DeepSeek并新建会话，输出 `[{"tool": "deepseek_new_chat", "args": {}}]`。
3. 如果是其他App，输出 `[{"tool": "open_app", "args": {"package_or_action": "App名字"}}]`。
严格使用 `{"tool": "工具名", "args": {参数}}` 的格式。
用户指令：'''

class MCP:
    def __init__(self): self.sid = None
    def _post(self, payload):
        data = json.dumps(payload).encode()
        headers = {"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
        if self.sid: headers["mcp-session-id"] = self.sid
        req = urllib.request.Request(MCP_URL, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if not self.sid: self.sid = r.headers.get("mcp-session-id")
                body = r.read().decode()
        except Exception as e: return {"error": str(e)}
        for line in reversed(body.splitlines()):
            if line.startswith("data: "): return json.loads(line[6:])
        return {"error": "解析响应失败"}
    def initialize(self):
        return self._post({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"dispatch","version":"0.1"}}})
    def call(self, tool, args, req_id=2):
        return self._post({"jsonrpc":"2.0","id":req_id,"method":"tools/call","params":{"name":tool,"arguments":args}})
    def run_shell(self, cmd):
        return self.call("execute_command", {"command": cmd}, req_id=99)

def ask_deepseek(user_text):
    if not DEEPSEEK_KEY: sys.exit("错误: 未设置 DEEPSEEK_API_KEY")
    payload = {"model":"deepseek-chat","messages":[{"role":"user","content":PROMPT_PREFIX + user_text}],"temperature":0.1}
    req = urllib.request.Request(DEEPSEEK_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type":"application/json","Authorization":f"Bearer {DEEPSEEK_KEY}"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read().decode())
    content = resp["choices"][0]["message"]["content"].strip()
    usage = resp.get("usage", {}) or {}
    return content, {
        "prompt": int(usage.get("prompt_tokens", 0)),
        "completion": int(usage.get("completion_tokens", 0)),
        "total": int(usage.get("total_tokens", 0)),
        "cache_hit": int(usage.get("prompt_cache_hit_tokens", 0)),
        "cache_miss": int(usage.get("prompt_cache_miss_tokens", 0)),
        "model": resp.get("model", "deepseek-chat"),
    }

MCP_TOOLS = {"open_app","click_by_text","click_by_id","click_by_class","find_element",
    "wait_for_element","get_ui_state","get_current_app","clipboard","text_to_speech",
    "show_toast","send_notification","open_url","set_volume","toggle_torch","vibrate",
    "get_device_info","get_battery_status","get_wifi_info","get_storage_info","get_screen_size"}

def _run_macro(tool, macro, pkg, args, mcp):
    import time as _t, re as _re

    def _wh():
        res = mcp.run_shell("wm size")
        raw = res.get("result", {}).get("structuredContent", {}).get("result", "")
        mm = _re.search(r"(\d+)x(\d+)", raw)
        return (int(mm.group(1)), int(mm.group(2))) if mm else (1260, 2800)
        
    def wait_for_change(old_xml, timeout=3.0):
        """轮询等待屏幕内容发生变化，一旦变化立刻返回 True，超时返回 False。"""
        start = _t.time()
        while _t.time() - start < timeout:
            _t.sleep(0.3)
            new_xml = hf.dump_ui_xml(mcp)
            if new_xml and new_xml != old_xml:
                return True
        return False

    def find_element(xml, spec):
        nodes = hf.parse_nodes(xml)
        W, H = _wh()
        if spec.get("region") == "top":
            nodes = [n for n in nodes if n["cy"] < H * 0.20]
        elif spec.get("region") == "content":
            nodes = [n for n in nodes if H * 0.15 < n["cy"] < H * 0.85]
        if "text_contains" in spec:
            kw = hf._norm(spec["text_contains"])
            nodes = [n for n in nodes if kw in hf._norm(n["text"]) or kw in hf._norm(n["desc"])]
        
        prefer = spec.get("prefer")
        if prefer == "wide_input":
            nodes = [n for n in nodes if (n["bounds"][2]-n["bounds"][0]) > W * 0.5 and n["cy"] < H * 0.2]
            nodes.sort(key=lambda n: (n["bounds"][2]-n["bounds"][0]), reverse=True)
        elif prefer == "video_card":
            filtered = []
            for n in nodes:
                w = n["bounds"][2] - n["bounds"][0]
                h = n["bounds"][3] - n["bounds"][1]
                if w > W * 0.8 and 300 < h < 900 and n["bounds"][1] > 300:
                    filtered.append(n)
            filtered.sort(key=lambda n: n["bounds"][1])
            if filtered: return filtered[0]
            return None
        return nodes[0] if nodes else None

    # ================= 核心：严格冷启动建卡 =================
    anchors = hf.load_anchors()
    if pkg not in anchors and pkg != "unknown":
        print(f"   🌱 发现新应用 ({pkg})，执行强制冷启动建卡...")
        mcp.call("show_toast", {"text": f"发现新应用({pkg})，正在强制冷启动采集特征..."})
        print(f"__NEED_HUMAN__系统正在收集【{pkg}】的界面特征，请稍候（约 8 秒），不要操作屏幕。")
        
        # 强制杀进程，然后拉起
        mcp.run_shell(f"~/rish -c 'am force-stop {pkg}'")
        _t.sleep(1.0)
        mcp.run_shell(f"~/rish -c 'monkey -p {pkg} -c android.intent.category.LAUNCHER 1'")
        _t.sleep(5.0)
        
        # 调用建卡脚本，确保等到正确的特征才返回
        script = "/data/data/com.termux/files/home/termux-mcp/capture_worker.py"
        import subprocess as _sp
        proc = _sp.Popen(["python3", script, pkg, "home"], stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True)
        for line in proc.stdout:
            if line.strip(): print(f"      [建卡] {line.strip()}")
        proc.wait()
        print(f"   ✅ 建卡完成，继续执行宏主流程...")

    W, H = _wh()

    for i, atom in enumerate(macro["atoms"], 1):
        a = atom["atom"]
        print(f"   ⚙️ Atom {i} {a}")

        if a == "open_app":
            apkg = atom.get("pkg", pkg)
            mcp.run_shell(f"~/rish -c 'monkey -p {apkg} -c android.intent.category.LAUNCHER 1'")
            _t.sleep(1.5)
            
        elif a == "tap":
            fx, fy = atom.get("xy", [0.5, 0.5])
            mcp.run_shell(f"~/rish -c 'input tap {int(W*fx)} {int(H*fy)}'")
            _t.sleep(1.5)
            print(f"      -> 点击屏幕 ({int(W*fx)}, {int(H*fy)})")
            
        elif a == "tap_element_by_config":
            el_name = atom.get("name")
            anchors = hf.load_anchors()
            cfg = anchors.get(pkg, {})
            elements = cfg.get("elements", {})
            if el_name in elements:
                fx, fy = elements[el_name]
                mcp.run_shell(f"~/rish -c 'input tap {int(W*fx)} {int(H*fy)}'")
                _t.sleep(1.5)
                print(f"      -> 点击配置坐标 {el_name} ({int(W*fx)}, {int(H*fy)})")
            else:
                print(f"      ❌ 配置中未找到元素 {el_name}，跳过")
                
        elif a == "tap_element":
            xml = hf.dump_ui_xml(mcp)
            pt, _ = hf.classify_page(pkg, mcp, xml)
            
            if pt == "unknown":
                print(f"      ⚠️ 当前页面判定为: unknown")
                print(f"      🔍 按一次返回键尝试归位...")
                mcp.run_shell("~/rish -c 'input keyevent 4'")
                _t.sleep(1.5)
                xml = hf.dump_ui_xml(mcp)
                pt, _ = hf.classify_page(pkg, mcp, xml)
                if pt == "unknown":
                    print(f"      ❌ 归位失败，跳过此步，不再乱点")
                    continue
            else:
                print(f"      ✅ 当前页面判定为: {pt}")
                
            el = find_element(xml, atom.get("find", {}))
            if not el:
                print(f"      ❌ 未找到目标元素，跳过此步")
                continue
            
            cx, cy = el["cx"], el["cy"]
            if cy > H * 0.85:
                print(f"      ⚠️ 目标坐标({cx},{cy})在底部边缘，取消点击")
                continue
                
            print(f"      -> 点击 ({cx},{cy}) text={el['text']!r} desc={el['desc']!r}")
            mcp.run_shell(f"~/rish -c 'input tap {cx} {cy}'")
            _t.sleep(1.5)
            
        elif a == "input_submit":
            text = atom.get("text", "").replace("{keyword}", args.get("keyword", ""))
            print(f"      -> 准备输入: {text}")
            
            # 记录点击前的屏幕
            xml_before = hf.dump_ui_xml(mcp)
            
            # 点击搜索框
            mcp.run_shell(f"~/rish -c 'input tap {int(W*0.5)} {int(H*0.08)}'")
            
            # 等键盘弹出（屏幕发生变化）
            if wait_for_change(xml_before, timeout=2.0):
                print("      ✅ 键盘已弹出")
            else:
                print("      ⚠️ 键盘未弹出，强行继续...")
            
            # 暴力删除（极速发送，每次间隔缩短到 0.02 秒）
            print("      -> 正在清空输入框...")
            for _ in range(30):
                mcp.run_shell("~/rish -c 'input keyevent 67'")
                _t.sleep(0.02)
            
            # 塞入剪贴板并粘贴
            xml_before_paste = hf.dump_ui_xml(mcp)
            mcp.call("clipboard", {"action": "write", "text": text})
            _t.sleep(0.2)
            mcp.run_shell("~/rish -c 'input keyevent 279'")
            
            # 等待文字粘贴成功
            if wait_for_change(xml_before_paste, timeout=2.0):
                print("      ✅ 文字已粘贴")
            else:
                print("      ⚠️ 粘贴未生效，尝试重试...")
                mcp.run_shell("~/rish -c 'input keyevent 279'")
                _t.sleep(1.0)
                
            # 回车触发搜索
            xml_before_enter = hf.dump_ui_xml(mcp)
            mcp.run_shell("~/rish -c 'input keyevent 66'")
            
            # 等搜索结果加载
            if wait_for_change(xml_before_enter, timeout=4.0):
                print("      ✅ 页面已刷新")
            else:
                print("      ⚠️ 搜索结果可能较慢")
        else:
            print(f"      ✗ 未知 atom: {a}")

    print(f"   ✅ 宏 {tool} 执行完毕")

def main():
    user_text = " ".join(sys.argv[1:]).strip()
    if not user_text: sys.exit("用法: python3 dispatch.py <指令>")

    print(f"\n[{time.strftime('%H:%M:%S')}] 🧠 正在请求 DeepSeek...")
    try:
        plan_str, usage = ask_deepseek(user_text)
    except Exception as e:
        print(f"❌ 请求 DeepSeek 失败: {e}")
        return

    print("__TOKEN_USAGE__" + json.dumps(usage, ensure_ascii=False))
    plan_str = plan_str.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    
    try: plan = json.loads(plan_str)
    except Exception as e: print(f"❌ JSON 解析失败: {e}"); return
        
    print(f"📋 初始计划: {json.dumps(plan, ensure_ascii=False)}")

    mcp = MCP()
    if "error" in mcp.initialize(): print("❌ MCP 初始化失败"); return

    for i, step in enumerate(plan, 1):
        tool = step.get("tool") or step.get("action") or step.get("name")
        args = step.get("args") or step.get("arguments") or step.get("params") or {}
        print(f"\n▶️ 步骤 {i}: {tool}")
        
        if tool == "open_app":
            pkg = args.get("package_or_action", "")
            pkg_map = load_pkg_map()
            if pkg.lower() in pkg_map: pkg = pkg_map[pkg.lower()]
            elif pkg in pkg_map: pkg = pkg_map[pkg]
            print(f"   -> 拉起: {pkg}")
            mcp.run_shell(f"~/rish -c 'monkey -p {pkg} -c android.intent.category.LAUNCHER 1'")
            time.sleep(2.0)
            
        elif tool in load_macros():
            macro = load_macros()[tool]
            pkg = macro["pkg"]
            print(f"   -> 执行宏 {tool}: pkg={pkg}")
            _run_macro(tool, macro, pkg, args, mcp)

        elif tool in MCP_TOOLS:
            result = mcp.call(tool, args, req_id=100+i)
            time.sleep(1.5)
            print(f"   ✅ 结果: {json.dumps(result, ensure_ascii=False)[:100]}")
        else:
            print(f"   ✗ 未知工具: {tool}")

    print(f"\n[{time.strftime('%H:%M:%S')}] 🏁 任务完成！")

if __name__ == "__main__":
    main()
