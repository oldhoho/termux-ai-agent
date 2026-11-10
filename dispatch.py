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
- universal_search(app_name, keyword): 在任意 App 中搜索指定的关键词。
- click_first_result(pkg, capture): 在搜索结果页智能挑选并点击第一个或最匹配的结果。
- open_app(package_or_action): 仅打开 App。
- deepseek_new_chat(): 打开DeepSeek并新建会话。

规则：
1. 只要用户要求“在某个App里搜xx”，输出 `[{"tool": "universal_search", "args": {"app_name": "App名", "keyword": "核心搜索词"}}]`。
2. **只要用户的意图是“进去/打开/播放/查看”某个搜索结果（包括群、视频、聊天、商品），必须组合动作！** 格式：`[{"tool": "universal_search", "args": {"app_name": "App名", "keyword": "搜索词"}}, {"tool": "click_first_result", "args": {"pkg": "App名"}}]`。
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

def auto_resolve_pkg(mcp, raw_name):
    pkg_map = load_pkg_map()
    name_lower = raw_name.lower()
    
    # 1. 精确匹配（最安全）
    if name_lower in pkg_map: return pkg_map[name_lower]
    if raw_name in pkg_map: return pkg_map[raw_name]
    
    # 2. 模糊匹配，但必须是完全包含关系，且避免把短词匹配到长词上
    for k, v in pkg_map.items():
        # 只有当用户的词长度大于等于字典词，或者字典词的长度跟用户输入差不多时才匹配
        if k == name_lower or (k in name_lower and len(k) >= len(name_lower)): 
            return v
            
    # 3. 如果还是找不到，去系统 pm 列表里搜
    print(f"   🔍 字典中未找到 [{raw_name}]，正在系统内搜索...")
    try:
        res = mcp.run_shell("~/rish -c 'pm list packages'")
        raw_pm = res.get("result", {}).get("structuredContent", {}).get("result", "")
        all_pkgs = re.findall(r"package:([a-zA-Z0-9_.]+)", raw_pm)
        for p in all_pkgs:
            if name_lower in p.lower():
                pkg_map[name_lower] = p
                with open(PKG_MAP_FILE, "w", encoding="utf-8") as f:
                    json.dump(pkg_map, f, ensure_ascii=False, indent=2)
                return p
    except Exception:
        pass
        
    return "NOT_FOUND"
def _run_macro(tool, macro, pkg, args, mcp):
    import time as _t, re as _re
    def _wh():
        res = mcp.run_shell("wm size")
        raw = res.get("result", {}).get("structuredContent", {}).get("result", "")
        mm = _re.search(r"(\d+)x(\d+)", raw)
        return (int(mm.group(1)), int(mm.group(2))) if mm else (1260, 2800)

    W, H = _wh()
    for i, atom in enumerate(macro["atoms"], 1):
        a = atom["atom"]
        print(f"   ⚙️ Atom {i} {a}")
        _atom_start_t = _t.time()

        if a == "open_app":
            apkg = atom.get("pkg", pkg)
            mcp.run_shell(f"~/rish -c 'monkey -p {apkg} -c android.intent.category.LAUNCHER 1'")
            _t.sleep(2.0)
        elif a == "tap_element_by_config":
            el_name = atom.get("name")
            anchors = hf.load_anchors()
            elements = anchors.get(pkg, {}).get("elements", {})
            if el_name in elements:
                fx, fy = elements[el_name]
                print(f"      -> 点击配置坐标 {el_name} ({int(W*fx)}, {int(H*fy)})")
                mcp.run_shell(f"~/rish -c 'input tap {int(W*fx)} {int(H*fy)}'")
                _t.sleep(1.0)
            else:
                print(f"      ❌ 配置中未找到元素 {el_name}")
        elif a == "tap":
            fx, fy = atom.get("xy", [0.5, 0.5])
            mcp.run_shell(f"~/rish -c 'input tap {int(W*fx)} {int(H*fy)}'")
            _t.sleep(1.5)
            print(f"      -> 点击屏幕 ({int(W*fx)}, {int(H*fy)})")
        elif a == "input_submit":
            text = atom.get("text", "").replace("{keyword}", args.get("keyword", ""))
            print(f"      -> 准备输入: {text}")
            mcp.call("clipboard", {"action": "write", "text": text})
            _t.sleep(0.1)
            cmd_chain = (f"input tap {int(W*0.5)} {int(H*0.08)} && sleep 0.8 && " + "input keyevent 67 && " * 15 + "input keyevent 279 && sleep 0.8 && input keyevent 66")
            print("      -> 极速连发：点击、清空、粘贴、回车...")
            mcp.run_shell(f"~/rish -c '{cmd_chain}'")
            _t.sleep(2.0)
        elif a == "wait":
            sec = atom.get("seconds", 2.0)
            print(f"      -> 等待 {sec} 秒...")
            _t.sleep(sec)
            
        elif a == "wait_and_capture":
            # 等待指定秒数，并在触发 capture 前检查是不是播放页
            sec = atom.get("seconds", 3.0)
            _t.sleep(sec)
            print(f"      -> 等待完毕，检查页面状态...")
            
            pt_check = hf.classify_page_quick(pkg, mcp)
            if pt_check in ["player", "detail"]:
                print(f"      ⚠️ 警告：页面已自动跳转至 {pt_check}，放弃抓取 {atom.get('capture')} 废卡！")
            else:
                # 正常触发抓取逻辑
                cap_type = atom.get("capture")
                if cap_type:
                    pt_data = hf.load_page_types()
                    if not (pkg in pt_data and cap_type in pt_data[pkg]):
                        print(f"      📸 开始抓取: {pkg} -> {cap_type}")
                        import subprocess as _sp
                        script = "/data/data/com.termux/files/home/termux-mcp/capture_worker.py"
                        log_file = open("/data/data/com.termux/files/home/termux-mcp/web.log", "a")
                        _sp.Popen(["python3", script, pkg, cap_type], stdout=log_file, stderr=log_file)
                        _t.sleep(0.2)
        else:
            print(f"      ✗ 未知 atom: {a}")

        if atom.get("capture"):
            cap_type = atom.get("capture")
            pt_data = hf.load_page_types()
            if not (pkg in pt_data and cap_type in pt_data[pkg]):
                print(f"      📸 后台静默收集页面特征: {pkg} -> {cap_type}")
                import subprocess as _sp
                script = "/data/data/com.termux/files/home/termux-mcp/capture_worker.py"
                log_file = open("/data/data/com.termux/files/home/termux-mcp/web.log", "a")
                _sp.Popen(["python3", script, pkg, cap_type], stdout=log_file, stderr=log_file)
                _t.sleep(0.2)

def main():
    import time as _t
    user_text = " ".join(sys.argv[1:]).strip()
    if not user_text: sys.exit("用法: python3 dispatch.py <指令>")

    print(f"\n[{_t.strftime('%H:%M:%S')}] 🧠 正在请求 DeepSeek...")
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
        if not hf.is_screen_on(mcp):
            print("\n🛑 警告：屏幕已锁定，强制中断整个任务！")
            break

        tool = step.get("tool") or step.get("action") or step.get("name")
        args = step.get("args") or step.get("arguments") or step.get("params") or {}
        print(f"\n▶️ 步骤 {i}: {tool}")
        
        if tool == "universal_search":
            app_name = args.get("app_name", "")
            keyword = args.get("keyword", "")
            
            # 🚨 反脑补防火墙 1：如果用户原话里有“QQ”，AI 却给了“QQ音乐”，强行掰回来
            if "QQ" in user_text or "qq" in user_text:
                if "音乐" not in user_text and "音乐" in app_name:
                    print(f"   ⚠️ 检测到 AI 脑补（用户要 QQ，AI 给了 {app_name}），强制纠正为 QQ...")
                    app_name = "QQ"
                    
            # 🚨 反脑补防火墙 2：如果用户原话里有“微信”，AI 给了“微信读书”，强行掰回
            if "微信" in user_text:
                if "读书" not in user_text and "读书" in app_name:
                    print(f"   ⚠️ 检测到 AI 脑补（用户要微信，AI 给了 {app_name}），强制纠正为 微信...")
                    app_name = "微信"
            
            pkg = auto_resolve_pkg(mcp, app_name)
            if pkg == "NOT_FOUND":
                print(f"   ❌ 失败：手机里找不到名为【{app_name}】的应用，请检查输入！")
                mcp.call("show_toast", {"text": f"找不到应用: {app_name}"})
                continue
            print(f"   -> 🌟 万能搜索: 目标 {pkg}, 关键词 {keyword}")
            
            anchors = hf.load_anchors()
            if pkg not in anchors:
                print(f"   -> 🌱 本地无锚点地图，直接执行强制冷启动探索建卡...")
                mcp.call("show_toast", {"text": f"探索新应用({pkg})，请稍候..."})
                mcp.run_shell(f"~/rish -c 'am force-stop {pkg}'")
                _t.sleep(1.0)
                mcp.run_shell(f"~/rish -c 'monkey -p {pkg} -c android.intent.category.LAUNCHER 1'")
                _t.sleep(4.0)
                
                script = "/data/data/com.termux/files/home/termux-mcp/capture_worker.py"
                import subprocess as _sp
                proc = _sp.Popen(["python3", script, pkg, "home"], stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True)
                for line in proc.stdout:
                    if line.strip(): print(f"      [建卡] {line.strip()}")
                proc.wait()
                
                anchors = hf.load_anchors()
                if pkg not in anchors:
                    print("   ⚠️ 建卡失败，自动降级为 MCP 原生文本查找...")
                    mcp.run_shell(f"~/rish -c 'monkey -p {pkg} -c android.intent.category.LAUNCHER 1'")
                    _t.sleep(2.0)
                    mcp.call("click_by_text", {"text": "搜索"}, req_id=200)
                    _t.sleep(1.5)
                    mcp.call("clipboard", {"action": "write", "text": keyword})
                    _t.sleep(0.5)
                    mcp.run_shell("~/rish -c 'input keyevent 279 && input keyevent 66'")
                    _t.sleep(2.0)
                    print("   ✅ 原生降级流程执行完毕")
                    continue
                print("   ✅ 建卡成功，已获取最新锚点地图。")
                
            current_pkg = hf.get_current_pkg(mcp)
            if not current_pkg or pkg not in current_pkg:
                print(f"   -> 🚀 目标不在前台，准备拉起应用...")
                mcp.run_shell(f"~/rish -c 'monkey -p {pkg} -c android.intent.category.LAUNCHER 1'")
                _t.sleep(2.0)
                
            print(f"   -> 🔍 快速校验当前所处页面层级...")
            for back_step in range(2):
                pt = hf.classify_page_quick(pkg, mcp)
                print(f"   -> 识别结果: {pt}")
                if pt in ["home", "search", "result"]:
                    print(f"   ✅ 已定位到可操作页面: {pt}，无需退回。")
                    break
                current_pkg = hf.get_current_pkg(mcp)
                if not current_pkg or pkg not in current_pkg:
                    print("   ⚠️ 已退出应用，强拉回前台...")
                    mcp.run_shell(f"~/rish -c 'monkey -p {pkg} -c android.intent.category.LAUNCHER 1'")
                    _t.sleep(2.0)
                    break
                print(f"   -> 🔙 未检测到搜索能力，尝试退回... (第 {back_step+1} 次)")
                mcp.run_shell("~/rish -c 'input keyevent 4'")
                _t.sleep(1.0)

            pt_data = hf.load_page_types()
            has_search = (pkg in pt_data and "search" in pt_data[pkg])
            has_result = (pkg in pt_data and "result" in pt_data[pkg])
            atoms_list = []
            cfg_elements = anchors[pkg].get("elements", {})
            search_btn_name = None
            for el_name in cfg_elements:
                if "搜索" in el_name or "输入" in el_name:
                    search_btn_name = el_name
                    break
            if search_btn_name:
                print(f"   -> 🎯 定位到搜索入口: [{search_btn_name}]")
                atoms_list.append({"atom": "tap_element_by_config", "name": search_btn_name})
            else:
                print(f"   -> ⚠️ 盲点顶部区域...")
                atoms_list.append({"atom": "tap", "xy": [0.5, 0.08]})
            if not has_search:
                atoms_list.append({"atom": "wait", "seconds": 1.0, "capture": "search"})
            else:
                atoms_list.append({"atom": "wait", "seconds": 0.3})
            atoms_list.append({"atom": "input_submit", "text": keyword})
            if not has_result:
                # 改用动态检查，不再死等 2 秒
                atoms_list.append({"atom": "wait_and_capture", "seconds": 3.0, "capture": "result"})
            else:
                atoms_list.append({"atom": "wait", "seconds": 0.5})
            dynamic_macro = {"pkg": pkg, "atoms": atoms_list}
            _run_macro(tool, dynamic_macro, pkg, args, mcp)
            continue
            
        elif tool == "click_first_result":
            print(f"   -> 🌟 启动 AI 智能选片...")
            W, H = 1260, 2800
            
            pt_now = hf.classify_page_quick(pkg, mcp)
            txt_check = hf.get_quick_texts(mcp)
            
            if pt_now not in ["result", "search", "home"] and not any(k in txt_check for k in ["来自群", "搜索结果", "综合", "聊天记录", "联系人"]):
                print(f"      ⚠️ 当前页面状态异常({pt_now})且无结果页特征，强行中止点击！")
                continue
                
            res = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=999)
            raw = res.get("result", {}).get("content", [{}])[0].get("text", "")
            
            if not raw:
                print("      ❌ 无法获取屏幕交互元素，放弃。")
                continue
                
            # 重新提取，这次要把坐标一起抓出来！
            # 格式匹配：[序号] "文字" (x,y) 或者 [序号] 图标 "文字" (x,y)
            pattern = r'(\d+)\]\s*(?:🖱️)?\s*\"([^\"]+)\"\s*\((\d+),(\d+)\)'
            matches = re.findall(pattern, raw)
            
            valid_nodes = []
            for idx_str, text, x, y in matches:
                text = text.strip()
                # 过滤无效文本
                if 6 <= len(text) <= 50 and not re.match(r'^[a-zA-Z0-9_\s,()\[\]]+$', text):
                    if text not in ["search_bar_input", "search_icon", "clear_query", "action_bar_root"]:
                        valid_nodes.append({
                            "text": text,
                            "x": int(x),
                            "y": int(y)
                        })
            
            if not valid_nodes:
                print("      ⚠️ 没找到可用的带坐标节点，放弃。")
                continue

            print(f"      📋 提取到带坐标的候选节点: {[n['text'] for n in valid_nodes[:5]]}")
            print("      🧠 正在请求 AI 选择序号...")
            
            options_text = ""
            for idx, node in enumerate(valid_nodes):
                options_text += f"{idx}: {node['text']}\n"
                
            prompt = f"""当前屏幕上有以下候选内容（包含序号）：
{options_text}
用户当前指令是：{user_text}。
请从中挑选最匹配的一个。**你必须且只能输出这个选项的数字序号**（例如：3）。不要输出任何文字，不要输出原话，只输出数字！
"""
            try:
                selected_idx_str, _ = ask_deepseek(prompt)
                match_idx = re.search(r'\d+', selected_idx_str)
                if match_idx:
                    idx = int(match_idx.group(0))
                    if 0 <= idx < len(valid_nodes):
                        target = valid_nodes[idx]
                        print(f"      🎯 AI 选中的序号是: {idx}，对应文字: [{target['text']}]")
                        print(f"      🚀 正在精准点击坐标 ({target['x']}, {target['y']})...")
                        mcp.run_shell(f"~/rish -c 'input tap {target['x']} {target['y']}'")
                        _t.sleep(3.0)
                        print("      ✅ 坐标点击完成！")
                    else:
                        print(f"      ⚠️ AI 返回的序号 {idx} 越界了。")
                else:
                    print(f"      ⚠️ AI 没有输出数字序号（AI 原话: {selected_idx_str}）。")
            except Exception as e:
                print(f"      ❌ AI 选片失败: {e}")
                
            print(f"   ✅ 操作完成")
            cap_type = args.get("capture")
            if cap_type:
                pt_data = hf.load_page_types()
                if not (pkg in pt_data and cap_type in pt_data[pkg]):
                    print(f"      📸 后台静默收集页面特征: {pkg} -> {cap_type}")
                    import subprocess as _sp
                    script = "/data/data/com.termux/files/home/termux-mcp/capture_worker.py"
                    log_file = open("/data/data/com.termux/files/home/termux-mcp/web.log", "a")
                    _sp.Popen(["python3", script, pkg, cap_type], stdout=log_file, stderr=log_file)
                    _t.sleep(0.2)

        elif tool == "open_app":
            pkg = args.get("package_or_action", "")
            
            # 🚨 反脑补防火墙（open_app 版）
            if "QQ" in user_text or "qq" in user_text:
                if "音乐" not in user_text and "音乐" in pkg:
                    print(f"   ⚠️ 检测到 AI 脑补，强制纠正为 QQ...")
                    pkg = "QQ"
            if "微信" in user_text:
                if "读书" not in user_text and "读书" in pkg:
                    pkg = "微信"
            pkg = auto_resolve_pkg(mcp, pkg)
            print(f"   -> 拉起: {pkg}")
            mcp.run_shell(f"~/rish -c 'monkey -p {pkg} -c android.intent.category.LAUNCHER 1'")
            _t.sleep(2.0)
            current_pkg = hf.get_current_pkg(mcp)
            if not current_pkg or pkg not in current_pkg:
                print(f"   ⚠️ 首次拉起未成功，尝试 am start 纠偏...")
                mcp.run_shell(f"~/rish -c 'am start -n {pkg}/.MainActivity'")
                _t.sleep(2.0)
            current_pkg = hf.get_current_pkg(mcp)
            if current_pkg and pkg in current_pkg:
                print(f"   ✅ {pkg} 已成功在前台")
            else:
                print(f"   ❌ {pkg} 启动失败")
            
        elif tool in load_macros():
            macro = load_macros()[tool]
            pkg = macro["pkg"]
            print(f"   -> 执行宏 {tool}: pkg={pkg}")
            _run_macro(tool, macro, pkg, args, mcp)

        elif tool in MCP_TOOLS:
            result = mcp.call(tool, args, req_id=100+i)
            _t.sleep(1.5)
            print(f"   ✅ 结果: {json.dumps(result, ensure_ascii=False)[:100]}")
        else:
            print(f"   ✗ 未知工具: {tool}")

    print(f"\n[{_t.strftime('%H:%M:%S')}] 🏁 任务完成！")

MCP_TOOLS = {"open_app","click_by_text","click_by_id","click_by_class","find_element",
    "wait_for_element","get_ui_state","get_current_app","clipboard","text_to_speech",
    "show_toast","send_notification","open_url","set_volume","toggle_torch","vibrate",
    "get_device_info","get_battery_status","get_wifi_info","get_storage_info","get_screen_size"}

if __name__ == "__main__":
    main()
