#!/usr/bin/env python3
import json, os, sys, time, urllib.request, urllib.error, re
import home_fingerprint as hf

import time as _time_mod
_CALL_STATS = {}

_STAGE_T0 = None
_STAGE_NAME = None
_STAGE_STATS = {}

def stage(name):
    global _STAGE_T0, _STAGE_NAME
    now = _time_mod.time()
    if _STAGE_NAME is not None and _STAGE_T0 is not None:
        _STAGE_STATS[_STAGE_NAME] = _STAGE_STATS.get(_STAGE_NAME, 0.0) + (now - _STAGE_T0)
    _STAGE_NAME = name
    _STAGE_T0 = now

def print_stages():
    global _STAGE_T0, _STAGE_NAME
    if _STAGE_NAME is not None and _STAGE_T0 is not None:
        _STAGE_STATS[_STAGE_NAME] = _STAGE_STATS.get(_STAGE_NAME, 0.0) + (_time_mod.time() - _STAGE_T0)
        _STAGE_NAME = None
    if not _STAGE_STATS:
        return
    print("\n⏱️ 阶段耗时:")
    total = sum(_STAGE_STATS.values())
    for k in sorted(_STAGE_STATS.keys(), key=lambda x: -_STAGE_STATS[x]):
        v = _STAGE_STATS[k]
        pct = (v / total * 100) if total else 0
        print(f"   {k}: {v:.1f}s ({pct:.0f}%)")
    print(f"   合计: {total:.1f}s")

_SLEEP_TOTAL = 0.0
def _tsleep(sec):
    global _SLEEP_TOTAL
    _SLEEP_TOTAL += sec
    _time_mod.sleep(sec)

def _stat(key, elapsed):
    s = _CALL_STATS.setdefault(key, {"n": 0, "t": 0.0})
    s["n"] += 1
    s["t"] += elapsed

def print_stats():
    print("\n📊 调用统计:")
    total = 0.0
    for k in sorted(_CALL_STATS.keys()):
        s = _CALL_STATS[k]
        print(f"   {k}: {s['n']} 次, {s['t']:.1f}s")
        total += s["t"]
    print(f"   工具调用合计: {total:.1f}s")
    print(f"   sleep 合计: {_SLEEP_TOTAL:.1f}s")
    print(f"   DeepSeek 调用: {_CALL_STATS.get('__deepseek__', {}).get('n', 0)} 次, {_CALL_STATS.get('__deepseek__', {}).get('t', 0.0):.1f}s")
    print(f"   总计: {total + _SLEEP_TOTAL + _CALL_STATS.get('__deepseek__', {}).get('t', 0.0):.1f}s")

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

PINYIN_HINTS = {
    "淘宝": "taobao", "支付宝": "alipay", "京东": "jd", "快手": "kuaishou",
    "微博": "weibo", "小红书": "xhs", "钉钉": "dingtalk", "知乎": "zhihu",
    "美团": "meituan", "饿了么": "ele", "高德": "amap", "地图": "amap",
    "网易云": "netease", "云音乐": "netease", "酷狗": "kugou", "QQ音乐": "qqmusic",
    "夸克": "quark", "浏览器": "browser", "相机": "camera", "相册": "gallery",
    "文件": "file", "时钟": "clock", "日历": "calendar", "计算器": "calculator",
}

AD_TEXTS = ("跳过", "关闭", "×", "X", "广告", "我知道了", "以后再说", "暂不",
            "允许", "拒绝", "稍后", "回复", "忽略", "不再提醒")

def _pinyin_hint(name):
    n = name.strip().lower()
    for k, v in PINYIN_HINTS.items():
        if k.lower() == n:
            return v
    return None

PROMPT_PREFIX = '''你是手机操作调度器。只输出 JSON 数组，不解释、不输出 markdown。
可用宏：
- universal_search(app_name, keyword): 在任意 App 中搜索指定的关键词。
- click_first_result(pkg, capture): 在搜索结果页智能挑选并点击第一个或最匹配的结果。
- open_app(package_or_action): 仅打开 App。
- read_and_report(pkg, target_page, question): 打开/导航到指定页面，抓取页面内容后用 AI 总结回复用户问题。**用户只想"看看有什么/有什么新内容/了解一下"时用这个，不点击卡片。** target_page 是页面类型（home/search/result/detail）或 "current"（当前页不导航）。
- assemble_and_run(pkg): 积木组装。给 AI 当前屏幕可点文本 + 可用动作（tap_text/press_back/swipe/wait/input_text/get_screen/summarize），AI 现场组装动作序列。**适合"去某页看某 Tab / 滑动找内容 / 多步导航"等复杂场景**，比 read_and_report 灵活但慢。
- deepseek_new_chat(): 打开DeepSeek并新建会话。

规则：
0. **先判断用户意图（intent）**：
   - `browse_and_report`：用户只想"看看有什么/有什么新内容/了解一下/推荐一下"，**不要求打开具体内容**。
     - **单页总结**（用户说"看看首页/看看有什么/当前页"）→ `[{"tool": "read_and_report", "args": {"pkg": "App名", "target_page": "home", "question": "用户原话"}}]`
     - **多步导航**（用户说"去排行榜看预约榜""滑到下面看看""进某个 Tab 看"）→ `[{"tool": "assemble_and_run", "args": {"pkg": "App名"}}]`
   - `search_and_open`：用户要搜索并打开某个结果。
   - `operate`：用户要执行操作（点按钮、发消息、播放等）。
   - `open_only`：只打开 App。
1. 只要用户要求“在某个App里搜xx”，输出 `[{"tool": "universal_search", "args": {"app_name": "App名", "keyword": "核心搜索词"}}]`。
2. **用户的意图是进去/打开/播放/查看/找/听/看某个具体内容或分类（如：听歌、看相声、找游戏、搜新闻）时**，必须组合动作：`[{"tool": "universal_search", "args": {"app_name": "App名", "keyword": "核心搜索词"}}, {"tool": "click_first_result", "args": {"pkg": "App名"}}]`。`click_first_result` 不要传 index，内部用 AI 从候选里选。
3. **用户要浏览/挑选 App 首页内容（不指定具体搜索词）时**，必须 `open_app` + `click_first_result`，且 `target_page="home"`：`[{"tool": "open_app", "args": {"package_or_action": "App名"}}, {"tool": "click_first_result", "args": {"pkg": "App名", "target_page": "home"}}]`。
4. 搜索后点结果，`click_first_result` 的 `target_page="result"`（默认，可省略）。
**禁止在 click_first_result 的 args 里传 index，传了也会被忽略。用户说的“第 N 个”由 click_first_result 内部的 AI 从候选里判断。**
严格使用 `{"tool": "工具名", "args": {参数}}` 的格式。
用户指令：'''

def assemble_and_run(mcp, user_text, pkg=None):
    """
    积木组装：把「当前屏幕可点文本 + 可用动作 + 用户需求」给 AI，AI 输出动作序列，逐步执行。
    pkg 为空时，从用户话里解析 App 名。
    """
    # 1. 解析 App（如果没给）
    if not pkg:
        # 简单解析：用户话里的 App 名，交给 auto_resolve_pkg
        # 先尝试从现有 plan 上下文拿，拿不到用 DeepSeek 解析
        pass
    if not pkg:
        print("   ❌ assemble_and_run 需要 pkg")
        return

    # 2. 拉起 App（确保在前台）
    if not safe_launch(mcp, pkg):
        print("   ❌ 拉起失败")
        return

    ctx = {"user_text": user_text, "screens": []}
    history = []

    for step in range(8):
        # 3. 抓当前屏幕
        _r = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=930 + step)
        _raw = _r.get("result", {}).get("content", [{}])[0].get("text", "")
        _lines = [ln.strip() for ln in (_raw or "").splitlines() if '"' in ln]
        _ui_block = "\n".join(_lines[:40])
        if not _ui_block:
            print("   ❌ 抓不到屏幕文本")
            return

        # 4. AI 规划：给积木 + 当前屏幕 + 用户需求 + 已执行历史
        _prompt = f"""用户需求：{user_text}
当前 App：{pkg}
已执行动作：{json.dumps(history, ensure_ascii=False) if history else "无"}
当前屏幕可点文本（**只能从下面列表里选 tap_text 的目标文本，列表里没有的绝对不要选**）：
{_ui_block}

可用动作：
- tap_text(text): 点击屏幕上某个文本
- press_back(): 按返回键
- swipe(direction): 滑动，direction=up/down/left/right
- wait(seconds): 等待
- input_text(text): 在当前输入框输入
- get_screen(): 重新抓屏幕（点击/滑动后）
- summarize(question): 把当前屏幕总结回复用户

请输出下一步**一个**动作的 JSON（只输出一个动作，不要数组），格式：
{{"action": "tap_text", "text": "排行榜"}}
如果已经完成（summarize 后），输出 {{"action": "done"}}。
如果用户只想"看看/了解"，最后用 summarize，不要 tap 内容卡。
"""
        try:
            _resp, _ = ask_deepseek(_prompt, system="你是手机操作调度器，只输出一个 JSON 动作，不解释。")
            _resp = _resp.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            _act = json.loads(_resp)
        except Exception as e:
            print(f"   ❌ AI 规划失败: {e}")
            return

        action = _act.get("action", "")
        print(f"   🔧 步骤 {step+1}: {json.dumps(_act, ensure_ascii=False)}")
        if action == "done":
            print("   ✅ 完成")
            return
        # 连续两次相同动作  停止（动作无效，继续也没用）
        if len(history) >= 1 and history[-1] == _act:
            print(f"    连续两次相同动作，停止（可能无效）")
            return
        history.append(_act)

        ok, msg = _exec_action(mcp, pkg, action, _act, ctx, screen_raw=_raw)
        if not ok:
            _fail_count = ctx.get("fail_count", 0) + 1
            ctx["fail_count"] = _fail_count
            if _fail_count >= 2:
                print("    连续两次动作失败，停止")
                return
            print(f"   ❌ 动作失败: {msg}")
            return
        if action == "summarize":
            print("   ✅ summarize 完成，结束组装")
            return
        # 单步操作（点X/打开X/进入X/切到X），tap_text 成功后直接完成
        _single_step = any(k in user_text for k in ("点", "打开", "进入", "切到", "跳转到", "按下"))
        if action == "tap_text" and _single_step:
            print(f"   ✅ 单步操作完成（tap_text 成功）")
            return
        _tsleep(0.8)

    print("   ⚠️ 达到 8 步上限")

def _exec_action(mcp, pkg, action, args, ctx, screen_raw=None):
    """
    执行单个动作。ctx 是执行上下文（dict），用于 get_screen 后追加新屏幕文本。
    返回 (ok, message)。ok=False 时中止序列。
    """
    a = action
    if a == "tap_text":
        target = args.get("text", "")
        if not target:
            return False, "tap_text 缺 text"
        # 防幻觉：目标必须在当前屏幕文本里（允许去掉（输入框）后缀）
        if screen_raw and target not in screen_raw:
            _base = target.replace("（输入框）", "").strip()
            if _base and _base in screen_raw:
                target = _base
            else:
                return False, f"目标 [{target}] 不在当前屏幕（防幻觉）"
        # 1. 先查 layout（text + desc，稳定区域直接点，省 get_ui_state）
        _anchors = hf.load_anchors().get(pkg, {})
        _pt_all = hf.load_page_types().get(pkg, {})
        _layouts = [_anchors.get("layout")] + [_p.get("layout") for _p in _pt_all.values()]
        _hit_el = None
        _hit_region = None
        for _lay in _layouts:
            if not _lay:
                continue
            for _region in _lay.get("regions", []):
                for _el in _region.get("elements", []):
                    _t = _el.get("text", "")
                    _d = _el.get("desc", "")
                    if target == _t or target == _d or (target and _d and target in _d):
                        _hit_el = _el
                        _hit_region = _region.get("name", "")
                        break
                if _hit_el:
                    break
            if _hit_el:
                break
        if _hit_el and _hit_region in ("top_bar", "tab_bar", "bottom_nav") or (_hit_el and _hit_region.startswith("top_bar")):
            W, H = hf.get_screen_size(mcp)
            _cx, _cy = int(W * _hit_el["x"]), int(H * _hit_el["y"])
            print(f"      👆 tap_text [{target}] layout命中({_hit_region}) @ ({_cx},{_cy})")
            mcp.run_shell(f"~/rish -c 'input tap {_cx} {_cy}'")
            hf.invalidate_quick_cache()
            return True, ""
        # 2. 锚点没命中，get_ui_state 找坐标（兜底）
        if screen_raw:
            _raw = screen_raw
        else:
            _r = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=920)
            _raw = _r.get("result", {}).get("content", [{}])[0].get("text", "")
        _hit = None
        for _ln in (_raw or "").splitlines():
            if target in _ln and '"' in _ln:
                _m = re.search(r'\((\d+),(\d+)\)', _ln)
                if _m:
                    _hit = (int(_m.group(1)), int(_m.group(2)))
                    break
        if not _hit:
            return False, f"屏幕没找到 [{target}]"
        print(f"      👆 tap_text [{target}] 扫描 @ {_hit}")
        mcp.run_shell(f"~/rish -c 'input tap {_hit[0]} {_hit[1]}'")
        hf.invalidate_quick_cache()
        return True, ""

    if a == "press_back":
        print(f"      🔙 press_back")
        mcp.run_shell("~/rish -c 'input keyevent 4'")
        hf.invalidate_quick_cache()
        return True, ""

    if a == "press_home":
        print(f"      🏠 press_home")
        mcp.run_shell("~/rish -c 'input keyevent 3'")
        hf.invalidate_quick_cache()
        return True, ""

    if a == "swipe":
        d = args.get("direction", "up")
        W, H = hf.get_screen_size(mcp)
        if d == "up":
            cmd = f"input swipe {W//2} {int(H*0.8)} {W//2} {int(H*0.3)} 300"
        elif d == "down":
            cmd = f"input swipe {W//2} {int(H*0.3)} {W//2} {int(H*0.8)} 300"
        elif d == "left":
            cmd = f"input swipe {int(W*0.8)} {H//2} {int(W*0.2)} {H//2} 300"
        else:
            cmd = f"input swipe {int(W*0.2)} {H//2} {int(W*0.8)} {H//2} 300"
        print(f"      ↔️ swipe {d}")
        mcp.run_shell(f"~/rish -c '{cmd}'")
        hf.invalidate_quick_cache()
        return True, ""

    if a == "wait":
        sec = float(args.get("seconds", 1.0))
        print(f"      ⏳ wait {sec}s")
        _tsleep(sec)
        return True, ""

    if a == "input_text":
        text = args.get("text", "")
        if not text:
            return False, "input_text 缺 text"
        print(f"      ⌨️ input_text [{text}]")
        mcp.call("clipboard", {"action": "write", "text": text})
        _tsleep(0.1)
        mcp.run_shell("~/rish -c 'input keyevent 279'")
        hf.invalidate_quick_cache()
        return True, ""

    if a == "get_screen":
        _r = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=921)
        _raw = _r.get("result", {}).get("content", [{}])[0].get("text", "")
        _texts = re.findall(r'"([^"]+)"', _raw or "")
        _joined = " ".join(_texts)[:2000]
        ctx.setdefault("screens", []).append(_joined)
        print(f"      📸 get_screen ({len(_texts)} 节点)")
        return True, ""

    if a == "summarize":
        q = args.get("question", ctx.get("user_text", ""))
        # summarize 必须重新抓屏幕（点击后页面可能变了）
        _r = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=922)
        _raw = _r.get("result", {}).get("content", [{}])[0].get("text", "")
        _texts = re.findall(r'"([^"]+)"', _raw or "")
        _joined = " ".join(_texts)[:2000]
        if not _joined:
            return False, "summarize 抓屏为空"
        _prompt = f"用户在 {pkg} 问：{q}\n当前页面内容：{_joined}\n请用中文简洁总结，只基于页面内容，不要编造。"
        try:
            _summary, _ = ask_deepseek(_prompt, system="你是信息总结助手，只基于给定页面内容回答，不编造。")
        except Exception as e:
            return False, f"summarize AI 失败: {e}"
        print(f"\n📝 [总结] {_summary}\n")
        try:
            _report_file = os.path.expanduser("~/.config/termux-mcp/last_report.json")
            with open(_report_file, "w", encoding="utf-8") as _f:
                json.dump({"ts": _time_mod.strftime("%Y-%m-%d %H:%M:%S"), "pkg": pkg,
                           "app": pkg, "question": q, "summary": _summary}, _f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        try:
            import subprocess as _sp
            _sp.Popen(["termux-notification", "--title", f"📝 {pkg} 总结", "--content", _summary])
        except Exception:
            pass
        return True, ""

    return False, f"未知动作: {a}"

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
    def _raw_call(self, tool, args, req_id=2):
        """不走锁屏检查，仅供 is_screen_on 内部使用"""
        _t0 = _time_mod.time()
        try:
            return self._post({"jsonrpc":"2.0","id":req_id,"method":"tools/call","params":{"name":tool,"arguments":args}})
        finally:
            _stat(f"raw:{tool}", _time_mod.time() - _t0)

    def run_shell(self, cmd):
        # 锁屏时拒绝所有底层 shell（防锁屏瞬间操作系统 UI 卡死）
        try:
            if not hf.is_screen_on(self):
                print(f"   🛑 锁屏中，跳过 shell: {cmd[:50]}")
                return {"error": "screen_locked"}
        except Exception:
            pass
        _t0 = _time_mod.time()
        _key = "shell"
        if "uiautomator dump" in cmd: _key = "shell:ui_dump"
        elif "input tap" in cmd: _key = "shell:input_tap"
        elif "input keyevent" in cmd: _key = "shell:input_keyevent"
        elif "monkey" in cmd or "am start" in cmd: _key = "shell:launch"
        elif "force-stop" in cmd: _key = "shell:force_stop"
        elif "pm enable" in cmd: _key = "shell:pm_enable"
        elif "isKeyguardShowing" in cmd: _key = "shell:dumpsys_lock"
        elif "mCurrentFocus" in cmd: _key = "shell:dumpsys_focus"
        elif "dumpsys" in cmd: _key = "shell:dumpsys"
        elif "wm size" in cmd: _key = "shell:wm_size"
        try:
            return self.call("execute_command", {"command": cmd}, req_id=99)
        finally:
            _stat(_key, _time_mod.time() - _t0)

    def call(self, tool, args, req_id=2):
        # 锁屏时拒绝 MCP 工具调用（get_ui_state/clipboard/show_toast 等）
        # is_screen_on 内部走 _raw_call，不经过这里
        if tool != "execute_command":
            try:
                if not hf.is_screen_on(self):
                    print(f"   🛑 锁屏中，跳过 tool: {tool}")
                    return {"error": "screen_locked"}
            except Exception:
                pass
            _t0 = _time_mod.time()
            try:
                return self._post({"jsonrpc":"2.0","id":req_id,"method":"tools/call","params":{"name":tool,"arguments":args}})
            finally:
                _stat(f"mcp:{tool}", _time_mod.time() - _t0)
        return self._post({"jsonrpc":"2.0","id":req_id,"method":"tools/call","params":{"name":tool,"arguments":args}})

def _is_keyboard_shown(mcp):
    """系统级判断键盘是否弹出（mInputShown）。键盘弹=已有输入焦点，通用不依赖 App。"""
    try:
        r = mcp.run_shell("~/rish -c 'dumpsys input_method | grep mInputShown'")
        raw = r.get("result", {}).get("structuredContent", {}).get("result", "")
        return "mInputShown=true" in raw
    except Exception:
        return False

def ask_deepseek(user_text, system=None):
    if not DEEPSEEK_KEY: sys.exit("错误: 未设置 DEEPSEEK_API_KEY")
    _ad_t0 = _time_mod.time()
    sys_prompt = system if system is not None else PROMPT_PREFIX
    payload = {"model":"deepseek-chat","messages":[{"role":"system","content":sys_prompt},{"role":"user","content":user_text}],"temperature":0.1}
    req = urllib.request.Request(DEEPSEEK_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type":"application/json","Authorization":f"Bearer {DEEPSEEK_KEY}"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read().decode())
    content = resp["choices"][0]["message"]["content"].strip()
    usage = resp.get("usage", {}) or {}
    _stat("__deepseek__", _time_mod.time() - _ad_t0)
    return content, {
        "prompt": int(usage.get("prompt_tokens", 0)),
        "completion": int(usage.get("completion_tokens", 0)),
        "total": int(usage.get("total_tokens", 0)),
        "cache_hit": int(usage.get("prompt_cache_hit_tokens", 0)),
        "cache_miss": int(usage.get("prompt_cache_miss_tokens", 0)),
        "model": resp.get("model", "deepseek-chat"),
    }

def _dehallucinate_global(candidate, user):
    c = (candidate or "").strip().lower()
    u = (user or "").strip().lower()
    if not c or not u:
        return candidate
    if c in u:
        return candidate
    best = None
    for token in re.findall(r'[\u4e00-\u9fa5a-zA-Z]{2,}', user):
        if token.lower() in c:
            if best is None or len(token) < len(best):
                best = token
    return best if best else candidate

def auto_resolve_pkg(mcp, raw_name):
    pkg_map = load_pkg_map()
    name_lower = raw_name.strip().lower()

    if name_lower in pkg_map:
        return pkg_map[name_lower]
    if raw_name in pkg_map:
        return pkg_map[raw_name]

    for k, v in pkg_map.items():
        if k.lower() == name_lower:
            return v
        if k.lower() in name_lower or name_lower in k.lower():
            return v

    hint = _pinyin_hint(raw_name)
    if hint:
        for k, v in pkg_map.items():
            if hint in k.lower() or hint in v.lower():
                return v

    print(f"   🔍 字典中未找到 [{raw_name}]，正在系统内搜索（含禁用应用）...")
    try:
        res = mcp.run_shell("~/rish -c 'pm list packages -u'")
        raw_pm = res.get("result", {}).get("structuredContent", {}).get("result", "")
        all_pkgs = re.findall(r"package:([a-zA-Z0-9_.]+)", raw_pm)
        targets = [name_lower]
        if hint:
            targets.append(hint)
        for p in all_pkgs:
            pl = p.lower()
            for t in targets:
                if t and t in pl:
                    pkg_map[raw_name] = p
                    with open(PKG_MAP_FILE, "w", encoding="utf-8") as f:
                        json.dump(pkg_map, f, ensure_ascii=False, indent=2)
                    print(f"   ✅ 系统匹配到: {p}")
                    return p
    except Exception as e:
        print(f"   ⚠️ pm list 失败: {e}")

    return "NOT_FOUND"

def ensure_pkg_enabled(mcp, pkg):
    mcp.run_shell(f"~/rish -c 'pm enable {pkg}'")
    _tsleep(0.5)

def dismiss_ads(mcp, max_rounds=1, pkg=None):
    # 播放页/详情页不扫广告（避免误关播放控制）；无锚点 App 跳过判页面（省 6s）
    if pkg and pkg in hf.load_anchors():
        try:
            _pt = hf.classify_page_quick(pkg, mcp)
            if _pt in ("detail", "player"):
                return
        except Exception:
            pass
    for _ in range(max_rounds):
        try:
            res = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=500)
            raw = res.get("result", {}).get("content", [{}])[0].get("text", "")
            if not raw:
                return
            # 广告关闭按钮通常在屏幕中上部（y < 0.5H），底部（y > 0.85H）的是导航/横幅
            W, H = hf.get_screen_size(mcp)
            hit = None
            for line in raw.splitlines():
                if any(t in line for t in AD_TEXTS):
                    m = re.search(r'\((\d+),(\d+)\)', line)
                    if m:
                        _x, _y = int(m.group(1)), int(m.group(2))
                        if _y > H * 0.85:
                            # 底部导航区的"关闭"类文本，大概率不是广告，跳过
                            continue
                        hit = (_x, _y)
                        break
            if not hit:
                return
            print(f"    关闭广告/弹窗 ({hit[0]}, {hit[1]})")
            mcp.run_shell(f"~/rish -c 'input tap {hit[0]} {hit[1]}'")
            _tsleep(1.0)
            # 点完确认还在原页，跳走就按返回
            if pkg:
                try:
                    _pt_after = hf.classify_page_ai(pkg, mcp)
                    if _pt_after in ("detail", "player"):
                        print(f"    点广告后跳到 {_pt_after}，按返回")
                        mcp.run_shell("~/rish -c 'input keyevent 4'")
                        _tsleep(0.8)
                        hf.invalidate_quick_cache()
                except Exception:
                    pass
        except Exception:
            return

def safe_launch(mcp, pkg):
    ensure_pkg_enabled(mcp, pkg)
    # 已在前台：不重新拉起、不扫广告（避免重置当前页/误点元素）
    _cur0 = hf.get_current_pkg(mcp)
    if _cur0 and pkg in _cur0:
        hf.invalidate_quick_cache()
        return True
    mcp.run_shell(f"~/rish -c 'monkey -p {pkg} -c android.intent.category.LAUNCHER 1'")
    _tsleep(2.5)
    dismiss_ads(mcp, pkg=pkg)
    current = hf.get_current_pkg(mcp)
    if not current or pkg not in current:
        mcp.run_shell(f"~/rish -c 'am start -n {pkg}/.MainActivity'")
        _tsleep(2.0)
        current = hf.get_current_pkg(mcp)
    hf.invalidate_quick_cache()
    return bool(current and pkg in current)

def _run_macro(tool, macro, pkg, args, mcp):
    import time as _t, re as _re
    def _wh():
        # 复用 hf.get_screen_size 缓存，避免每次 wm size
        return hf.get_screen_size(mcp)

    _t_wh0 = _time_mod.time()
    W, H = _wh()
    print(f"   🔧 _wh() 耗时 {_time_mod.time() - _t_wh0:.2f}s")
    for i, atom in enumerate(macro["atoms"], 1):
        a = atom["atom"]
        _t0_atom = _time_mod.time()
        print(f"   ⚙️ Atom {i} {a} @ {_t.strftime('%H:%M:%S')}")
        _atom_start_t = _t.time()

        if a == "open_app":
            apkg = atom.get("pkg", pkg)
            if not safe_launch(mcp, apkg):
                print(f"      ❌ 无法拉起 {apkg}，中止宏")
                return False
        elif a == "tap_element_by_config":
            el_name = atom.get("name")
            anchors = hf.load_anchors()
            elements = anchors.get(pkg, {}).get("elements", {})
            _cur_xy = None
            try:
                _r_tap = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=950)
                _raw_tap = _r_tap.get("result", {}).get("content", [{}])[0].get("text", "")
                for _ln in (_raw_tap or "").splitlines():
                    if el_name in _ln and '"' in _ln:
                        _m = re.search(r'\((\d+),(\d+)\)', _ln)
                        if _m:
                            _cur_xy = (int(_m.group(1)), int(_m.group(2)))
                            break
            except Exception:
                pass
            if _cur_xy:
                print(f"      -> 点击 {el_name} 当前坐标 {_cur_xy}")
                mcp.run_shell(f"~/rish -c 'input tap {_cur_xy[0]} {_cur_xy[1]}'")
                _tsleep(0.3)
            else:
                if _is_keyboard_shown(mcp):
                    print(f"       没找到 {el_name}，但键盘已弹出，跳过点击直接输入")
                else:
                    print(f"       当前屏幕没找到 {el_name}，中止（不盲点）")
                    return False
        elif a == "tap":
            fx, fy = atom.get("xy", [0.5, 0.5])
            mcp.run_shell(f"~/rish -c 'input tap {int(W*fx)} {int(H*fy)}'")
            _tsleep(0.5)
            print(f"      -> 点击屏幕 ({int(W*fx)}, {int(H*fy)})")
        elif a == "input_submit":
            text = atom.get("text", "").replace("{keyword}", args.get("keyword", ""))
            print(f"      -> 准备输入: {text}")
            _kbd = _is_keyboard_shown(mcp)
            if _kbd:
                # 键盘已弹  已有输入焦点（VIA 类），跳过 dump/tap，直接粘贴回车
                print("       键盘已弹，跳过聚焦，直接粘贴")
            else:
                # 键盘没弹  找 EditText 聚焦（TapTap 类）
                _ed_cx, _ed_cy = int(W * 0.5), int(H * 0.08)
                try:
                    _xml_in = hf.dump_ui_xml(mcp)
                    if _xml_in:
                        for _n in hf.parse_nodes(_xml_in):
                            if "edittext" in (_n.get("class") or "").lower():
                                _ed_cx, _ed_cy = _n["cx"], _n["cy"]
                                print(f"       聚焦 EditText @ ({_ed_cx},{_ed_cy}) cls={_n.get('class')} rid={_n.get('resource_id')} text={repr(_n['text'][:20])} bounds={_n['bounds']}")
                                break
                    else:
                        print("       dump 失败且键盘未弹，无法可靠定位输入框，中止")
                        return
                except Exception:
                    pass
                mcp.run_shell(f"~/rish -c 'input tap {_ed_cx} {_ed_cy}'")
                _tsleep(0.4)
            # 全选 + 粘贴 + 回车
            mcp.call("clipboard", {"action": "write", "text": text})
            _tsleep(0.1)
            cmd_all = "input keycombination 113 29 && input keyevent 279 && input keyevent 66"
            mcp.run_shell(f"~/rish -c '{cmd_all}'")
            print("      -> 一次 shell：全选+粘贴+回车")
            _tsleep(0.5)
            hf.invalidate_quick_cache()
        elif a == "wait":
            sec = atom.get("seconds", 2.0)
            print(f"      -> 等待 {sec} 秒...")
            _tsleep(sec)
            
        elif a == "wait_and_capture":
            # 等待指定秒数，并在触发 capture 前检查是不是播放页
            sec = atom.get("seconds", 3.0)
            _tsleep(sec)
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
                        _proc_cap = _sp.Popen(["python3", script, pkg, cap_type], stdout=log_file, stderr=log_file)
                        try:
                            _proc_cap.wait(timeout=30)
                        except Exception:
                            pass
                        hf.invalidate_quick_cache()
        else:
            print(f"      ✗ 未知 atom: {a}")

        print(f"      ⏱️ Atom {i} {a} 耗时 {_time_mod.time() - _t0_atom:.2f}s")
        # wait_and_capture atom 内部已处理 capture，不重复
        if atom.get("capture") and a != "wait_and_capture":
            cap_type = atom.get("capture")
            pt_data = hf.load_page_types()
            if not (pkg in pt_data and cap_type in pt_data[pkg]):
                print(f"      📸 后台静默收集页面特征: {pkg} -> {cap_type}")
                import subprocess as _sp
                script = "/data/data/com.termux/files/home/termux-mcp/capture_worker.py"
                log_file = open("/data/data/com.termux/files/home/termux-mcp/web.log", "a")
                _sp.Popen(["python3", script, pkg, cap_type], stdout=log_file, stderr=log_file)
                _tsleep(0.2)
    print(f"   🔧 _run_macro 结束")

def main():
    import time as _t
    user_text = " ".join(sys.argv[1:]).strip()
    if not user_text: sys.exit("用法: python3 dispatch.py <指令>")

    stage("1.DeepSeek规划")
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
            stage("2.搜索-拉起")
            print(f"   -> 🌟 万能搜索: 目标 {pkg}, 关键词 {keyword}")
            
            anchors = hf.load_anchors()
            if pkg not in anchors:
                print(f"   -> 🌱 本地无锚点，强杀冷启动建卡（仅首次探索场景）...")
                mcp.call("show_toast", {"text": f"探索新应用({pkg})，请稍候..."})
                # 无锚点首次探索：强杀保证从首页开始，避免历史页面污染建卡
                ensure_pkg_enabled(mcp, pkg)
                mcp.run_shell(f"~/rish -c 'am force-stop {pkg}'")
                _tsleep(1.0)
                mcp.run_shell(f"~/rish -c 'monkey -p {pkg} -c android.intent.category.LAUNCHER 1'")
                _tsleep(4.0)
                dismiss_ads(mcp, pkg=pkg)
                hf.invalidate_quick_cache()
                _cur = hf.get_current_pkg(mcp)
                if not _cur or pkg not in _cur:
                    print(f"   ❌ 冷启动失败，中止任务")
                    return

                script = "/data/data/com.termux/files/home/termux-mcp/capture_worker.py"
                import subprocess as _sp
                proc = _sp.Popen(["python3", script, pkg, "home"], stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True)
                for line in proc.stdout:
                    if line.strip(): print(f"      [建卡] {line.strip()}")
                proc.wait()

                anchors = hf.load_anchors()
                if pkg not in anchors:
                    print("   ❌ 建卡失败且无锚点地图，按规则中止（不盲点降级）")
                    mcp.call("show_toast", {"text": f"建卡失败: {pkg}"})
                    return
                print("   ✅ 建卡成功，已获取最新锚点地图。")
                
            current_pkg = hf.get_current_pkg(mcp)
            if not current_pkg or pkg not in current_pkg:
                print(f"   -> 🚀 目标不在前台，准备拉起应用...")
                mcp.run_shell(f"~/rish -c 'monkey -p {pkg} -c android.intent.category.LAUNCHER 1'")
                _tsleep(2.0)
                
            stage("3.搜索-页面判定退避")
            print(f"   -> 🔍 快速校验当前所处页面层级...")
            unknown_streak = 0
            located = False
            max_back = 5
            for _ in range(max_back):
                pt = hf.classify_page_quick(pkg, mcp, skip_fallback=True)
                print(f"   -> 识别结果: {pt}")
                if pt in ["home", "search"]:
                    if pt == "home":
                        _ai_pt = hf.classify_page_ai(pkg, mcp)
                        if _ai_pt not in ("home", "unknown"):
                            print(f"    quick 判 home，AI 复核为 {_ai_pt}，继续退避")
                            pt = _ai_pt
                        else:
                            located = True
                            break
                    else:
                        located = True
                        break
                current_pkg = hf.get_current_pkg(mcp)
                if not current_pkg or pkg not in current_pkg:
                    if not hf.is_screen_on(mcp):
                        print(f"   🛑 锁屏中，中止任务（不操作锁屏界面）")
                        return
                    if not safe_launch(mcp, pkg):
                        print(f"   ❌ 应用被弹出且拉不回，中止任务")
                        return
                    continue
                print(f"   -> 🔙 非基础页({pt})第 {unknown_streak}/{max_back} 次，返回键退避...")
                mcp.run_shell("~/rish -c 'input keyevent 4'")
                hf.invalidate_quick_cache()
                _tsleep(0.5)
            if not located:
                print(f"   ❌ 连续 {max_back} 次无法回到基础页面，中止当前任务（不盲点）")
                mcp.call("show_toast", {"text": "页面异常，任务中止"})
                return

            pt_data = hf.load_page_types()
            has_search = (pkg in pt_data and "search" in pt_data[pkg])
            has_result = (pkg in pt_data and "result" in pt_data[pkg])
            atoms_list = []
            cfg_elements = anchors[pkg].get("elements", {})
            # 通用优先级：精确"搜索"按钮 > 含"搜索"排除输入框 > 含"输入" > 含"搜索"兜底
            search_btn_name = None
            keys = list(cfg_elements.keys())
            if "搜索" in keys:
                search_btn_name = "搜索"
            else:
                for k in keys:
                    if "搜索" in k and "栏" not in k and "框" not in k and "输入" not in k:
                        search_btn_name = k
                        break
                if not search_btn_name:
                    for k in keys:
                        if "输入" in k or "框" in k:
                            search_btn_name = k
                            break
                if not search_btn_name:
                    for k in keys:
                        if "搜索" in k:
                            search_btn_name = k
                            break
            if not search_btn_name:
                print(f"   ❌ 锚点中没有搜索入口，中止（禁止盲点）")
                mcp.call("show_toast", {"text": f"{pkg} 缺少搜索锚点"})
                return
            stage("4.搜索-输入提交")
            print(f"   -> 🎯 定位到搜索入口: [{search_btn_name}]")
            atoms_list.append({"atom": "tap_element_by_config", "name": search_btn_name})
            if not has_search:
                atoms_list.append({"atom": "wait", "seconds": 1.0, "capture": "search"})
            else:
                atoms_list.append({"atom": "wait", "seconds": 0.3})
            atoms_list.append({"atom": "input_submit", "text": keyword})
            if not has_result:
                # 改用动态检查，不再死等 2 秒
                atoms_list.append({"atom": "wait_and_capture", "seconds": 3.0, "capture": "result"})
            else:
                atoms_list.append({"atom": "wait", "seconds": 0.8, "capture": "result"})
            dynamic_macro = {"pkg": pkg, "atoms": atoms_list}
            _run_macro(tool, dynamic_macro, pkg, args, mcp)
            continue
            
        elif tool == "click_first_result":
            stage("5.选片-候选抓取")
            print(f"   -> 🌟 启动 AI 智能选片...")
            _app_profile = {}
            try:
                _anchors_all = hf.load_anchors()
                _app_profile = _anchors_all.get(pkg, {}).get("profile", {}) or {}
            except Exception:
                pass
            if _app_profile:
                print(f"   🧬 profile: {_app_profile.get('app_kind')} / {_app_profile.get('content_kind')}")

            # 直接 get_ui_state，一次抓屏同时判页面 + 拿候选
            _res0 = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=999)
            _raw0 = _res0.get("result", {}).get("content", [{}])[0].get("text", "")
            if not _raw0:
                print("      ❌ 无法获取屏幕交互元素，中止")
                return
            # 系统弹窗检查（复用 _raw0，不额外抓屏）
            _popup_hit = None
            for _ln in _raw0.splitlines():
                if any(t in _ln for t in ("允许", "拒绝", "稍后", "回复", "忽略", "不再提醒")):
                    _m = re.search(r'\((\d+),(\d+)\)', _ln)
                    if _m:
                        _popup_hit = (int(_m.group(1)), int(_m.group(2)))
                        break
            if _popup_hit:
                print(f"      🚨 系统弹窗 ({_popup_hit[0]},{_popup_hit[1]})，点击关闭")
                mcp.run_shell(f"~/rish -c 'input tap {_popup_hit[0]} {_popup_hit[1]}'")
                _tsleep(0.5)
                hf.invalidate_quick_cache()
                _res0 = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=999)
                _raw0 = _res0.get("result", {}).get("content", [{}])[0].get("text", "")
            _joined0 = " ".join(re.findall(r'"([^"]+)"', _raw0))
            for _kw in ("首页", "关注"):
                if _kw in _joined0:
                    print(f"      🔍 [判定调试] _joined0 含 '{_kw}'")
                else:
                    print(f"      🔍 [判定调试] _joined0 不含 '{_kw}'，_joined0 长度={len(_joined0)}")
            pt_now = hf._classify_from_text(pkg, _joined0)
            # 目标页面：用户意图决定，不是无脑退避
            _target_page = args.get("target_page", "result")
            # 按 page_types 的 parent 链导航（建卡时记录退避路径）
            if pt_now != _target_page:
                _pt_data = hf.load_page_types().get(pkg, {})
                _chain = []
                _cur = pt_now
                for _ in range(5):
                    _cur_data = _pt_data.get(_cur, {})
                    _parent = _cur_data.get("parent")
                    if not _parent:
                        # search 默认上级是 home（建卡时没传 from_page 的兜底）
                        if _cur == "search" and _target_page == "home":
                            _parent = "home"
                        else:
                            break
                    _chain.append(_parent)
                    _cur = _parent
                    if _cur == _target_page:
                        break
                if _cur == _target_page and _chain:
                    print(f"      🧭 导航链: {pt_now} → {' → '.join(_chain)}")
                    for _ in _chain:
                        mcp.run_shell("~/rish -c 'input keyevent 4'")
                        hf.invalidate_quick_cache()
                        _tsleep(0.5)
                    # 重新抓确认到达，未到达继续按返回键（最多 3 次）
                    for _retry in range(3):
                        _res0 = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=999)
                        _raw0 = _res0.get("result", {}).get("content", [{}])[0].get("text", "")
                        _joined0 = " ".join(re.findall(r'"([^"]+)"', _raw0)) if _raw0 else ""
                        pt_now = hf._classify_from_text(pkg, _joined0) if _joined0 else "unknown"
                        print(f"      🧭 导航后页面: {pt_now}（重试 {_retry+1}/3）")
                        if pt_now == _target_page:
                            break
                        mcp.run_shell("~/rish -c 'input keyevent 4'")
                        hf.invalidate_quick_cache()
                        _tsleep(0.5)
                elif pt_now == "search" and _target_page == "result":
                    print(f"      ⏳ search 等 result，等 1.0s")
                    hf.invalidate_quick_cache()
                    _tsleep(1.0)
                    _res0 = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=999)
                    _raw0 = _res0.get("result", {}).get("content", [{}])[0].get("text", "")
                    _joined0 = " ".join(re.findall(r'"([^"]+)"', _raw0)) if _raw0 else ""
                    pt_now = hf._classify_from_text(pkg, _joined0) if _joined0 else "unknown"
                else:
                    print(f"      ⚠️ 无 parent 链从 {pt_now} 到 {_target_page}，用当前页继续")
            txt_check = _joined0
            if pt_now not in ["result", "search", "home", "detail", "player"]:
                # 页面类型未知时，不立即中止，先用 get_ui_state 看有没有足够可点节点
                _res_check = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=998)
                _raw_check = _res_check.get("result", {}).get("content", [{}])[0].get("text", "")
                _clickable = re.findall(r'\]\s*(🖱️)\s*"([^"]+)"', _raw_check or "")
                if len(_clickable) < 3:
                    print(f"      ❌ 页面状态异常({pt_now})且可点节点不足({len(_clickable)})，中止点击")
                    return
                print(f"      ⚠️ 页面类型未知({pt_now})，但检测到 {len(_clickable)} 个可点节点，继续")

            # 通用 Tab 纠偏：如果 result.elements 有默认 Tab，且当前页没命中它，点回默认 Tab
            pt_data = hf.load_page_types().get(pkg, {})
            result_elements = pt_data.get("result", {}).get("elements", {})
            if result_elements and pt_now == "result":
                # 默认 Tab 排除导航类元素（返回/关闭/菜单/更多/分享/设置）
                NAV_WORDS = ("返回", "关闭", "菜单", "更多", "分享", "设置", "退出", "搜索", "取消")
                default_tab_name = None
                for k in result_elements.keys():
                    if not any(nw in k for nw in NAV_WORDS):
                        default_tab_name = k
                        break
                if not default_tab_name:
                    default_tab_name = next(iter(result_elements))
                default_tab_xy = result_elements[default_tab_name]
                if default_tab_name not in txt_check:
                    print(f"      🔁 当前页未命中默认 Tab [{default_tab_name}]，尝试切回")
                    W, H = hf.get_screen_size(mcp)
                    mcp.run_shell(f"~/rish -c 'input tap {int(W*default_tab_xy[0])} {int(H*default_tab_xy[1])}'")
                    hf.invalidate_quick_cache()
                    _tsleep(2.0)
                    print(f"      🔁 已点击默认 Tab [{default_tab_name}]")

            raw = _raw0  # 复用前面已抓的 get_ui_state，不重复抓
            _candidate_raw = raw

            pattern = r'(\d+)\]\s*(?:🖱️)?\s*"([^"]+)"\s*\((\d+),(\d+)\)'
            matches = re.findall(pattern, raw)

            W, H = hf.get_screen_size(mcp)
            top_bar_y = int(H * 0.12)
            bottom_kb_y = int(H * 0.88)
            valid_nodes = []
            seen_y = []
            for _, text, x, y in matches:
                text = text.strip()
                yy = int(y)
                xx = int(x)
                _orig = text
                if not text or text.isdigit():
                    continue
                if len(text) > 40:
                    continue
                if len(text) < 2:
                    # 只杀 1 字噪声（G//数字）；2 字可能是内容标题（原神/王者），
                    # 交给后续 Tab 正则 + AI 选片排除
                    continue
                if yy < top_bar_y or yy > bottom_kb_y:
                    continue
                if text in {"search_bar_input", "search_icon", "clear_query", "action_bar_root"}:
                    continue
                # 排除用户指令里的短关键词本身，但只在顶部输入区排（结果卡标题常等于关键词）
                if len(text) <= 4 and text in user_text and yy < top_bar_y:
                    continue
                # 通用排除：Tab 标签（含 ,N之M,标签 格式）
                if re.search(r',\d+之\d+,标签', text):
                    continue
                # 通用排除：纯播放量/日期（数字+万亿，或 · 日期）
                if re.match(r'^[\d.]+[万亿]?$', text):
                    continue
                if re.match(r'^·.*日$', text):
                    continue
                # 通用排除：时长（11:33 / 1:09:22）
                if re.match(r'^\d+:\d+(:\d+)?$', text):
                    continue
                # 通用排除：URL
                if re.match(r'^https?://', text):
                    continue
                # 排除 B站 评论（0 开头 + 冒号，如 0用户名：评论内容）
                if re.match(r'^0.*[：:]', text):
                    continue
                # 通用排除：技术布局节点
                # 1) 含下划线且无中文
                if "_" in text and not re.search(r'[\u4e00-\u9fa5]', text):
                    continue
                # 2) 全小写纯英文且长度 < 15（tabhost / layermanager 这类技术 ID）
                if re.match(r'^[a-z][a-z0-9_]{0,14}$', text):
                    continue
                dup = False
                for sy in seen_y:
                    if abs(sy - yy) < 60:
                        dup = True
                        break
                if dup:
                    continue
                seen_y.append(yy)
                valid_nodes.append({"text": text, "x": xx, "y": yy})


            # 纵向聚类：按 y 排序，按屏高比例分组
            # 阈值 = 0.11H（Tab 栏内节点间距小，商品卡间距大）
            cluster_gap = int(H * 0.11)
            valid_nodes.sort(key=lambda n: n["y"])
            groups = []
            current = []
            last_y = None
            for n in valid_nodes:
                if last_y is None or n["y"] - last_y <= cluster_gap:
                    current.append(n)
                else:
                    groups.append(current)
                    current = [n]
                last_y = n["y"]
            if current:
                groups.append(current)
            # 仅当最顶部组是 Tab 栏（多节点、y 跨度小、x 分布均匀）才丢弃
            if len(groups) > 1:
                g0 = groups[0]
                g0_span = g0[-1]["y"] - g0[0]["y"]
                _xs = sorted(n["x"] for n in g0)
                _gaps = [_xs[i+1] - _xs[i] for i in range(len(_xs)-1)]
                _uniform_x = bool(_gaps) and all(g < W * 0.08 for g in _gaps)
                if len(g0) >= 3 and g0_span < int(H * 0.15) and _uniform_x:
                    groups = groups[1:]
            valid_nodes = [n for g in groups for n in g]

            if not valid_nodes:
                print("      ❌ 没有可用候选节点，中止")
                return
            print(f"      📋 [候选] 共 {len(valid_nodes)} 个：")
            for _i, _n in enumerate(valid_nodes):
                print(f"         {_i+1}. [{_n['text'][:40]}] @ ({_n['x']},{_n['y']})")

            CN_NUM = {"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}
            # 忽略 args.index（AI 可能传“第 N 个候选”，不靠谱），全走 AI 选片
            # user_order 仅用于 prompt 提示“用户要第 N 个视频”，不直接点
            target = None
            # 用户原话直接传给 AI，让 AI 判断“第 N 个视频”

            _ai_nodes = valid_nodes[:6] if len(valid_nodes) > 6 else valid_nodes
            options_text = "".join(f"{i+1}: {n['text']}\n" for i, n in enumerate(_ai_nodes))
            stage("6.选片-AI选片")
            selector_system = "你是安卓屏幕元素选择器。只输出一个从 1 开始的数字序号，不输出任何文字、JSON、标点或解释。"
            if target is None:
                _profile_block = ""
                if _app_profile:
                    _profile_block = f"""当前 App 类型：{_app_profile.get('app_kind', 'other')}
内容卡类型：{_app_profile.get('content_kind', '内容')}
主页内容卡：{_app_profile.get('home_content_hint', '')}
点击内容卡：{_app_profile.get('click_target_hint', '')}

"""
                _ak = (_app_profile or {}).get("app_kind", "other")
                if _ak == "game_store":
                    _ignore_extra = "UP 主名、时间、"
                else:
                    _ignore_extra = "UP 主名、时间、播放量、预约数、"
                prompt = f"""{_profile_block}当前屏幕候选：
{options_text}
用户指令：{user_text}

请先在心里数出候选里有多少个"内容卡标题"。
以下不是内容卡标题，要忽略：{_ignore_extra}平台（移动/电脑）、按钮（进入论坛/预约/下载）、评论内容、点赞数、分集信息、广告、Tab。
如果用户要的"第 N 个"超出内容卡总数，只输出 0。
否则输出第 N 个内容卡标题在屏幕候选里的编号（一个数字）。"""
                selected_str, _ = ask_deepseek(prompt, system=selector_system)
                m = re.search(r'\d+', selected_str or "")
                if not m:
                    print(f"      ❌ AI 未输出序号: {selected_str!r}，中止")
                    return
                ai_num = int(m.group(0))
                if ai_num == 0:
                    print(f"      ❌ AI 返回 0（视频数量不足），中止")
                    return
                if not (1 <= ai_num <= len(valid_nodes)):
                    print(f"      ❌ AI 序号 {ai_num} 越界，中止")
                    return
                target = valid_nodes[ai_num - 1]
                print(f"      🎯 AI 选中第 {ai_num} 个: [{target['text']}] @ ({target['x']},{target['y']})")

            W, H = hf.get_screen_size(mcp)
            if not (0 <= target["x"] < W and 0 <= target["y"] < H):
                print(f"      ❌ 坐标越界 ({target['x']},{target['y']})，中止")
                return
            stage("7.选片-点击前重定位")
            # 点击前重新定位：先用候选 raw（同一次抓屏，最快），找不到再重新抓
            found2 = False
            _target_norm = re.sub(r'\s+', '', target["text"])
            p_ai = r'(\d+)\]\s*(🖱️)?\s*"([^"]+)"\s*\((\d+),(\d+)\)'
            _raw_matches = re.findall(p_ai, _candidate_raw or "")
            print(f"      🔍 [候选raw] 解析 {len(_raw_matches)} 个节点，目标: [{_target_norm[:30]}]")
            print(f"      🔍 [候选raw] 前600字符: {(_candidate_raw or '')[:600]}")
            for _ln in (_candidate_raw or '').splitlines():
                if '神仙' in _ln:
                    print(f"      🔍 [候选raw] 含神仙行: {_ln.strip()[:120]}")
            _hit = False
            for _, ck, t2, x2, y2 in _raw_matches:
                if re.sub(r'\s+', '', t2) == _target_norm:
                    _hit = True
                    nx, ny = int(x2), int(y2)
                    if (nx, ny) != (target["x"], target["y"]):
                        print(f"      🔄 坐标已变化(候选raw): ({target['x']},{target['y']}) → ({nx},{ny})")
                    target = {"text": target["text"], "x": nx, "y": ny}
                    found2 = True
                    break
            if not _hit and _raw_matches:
                print(f"      🔍 [候选raw] 未命中，候选前3: {[m[2][:20] for m in _raw_matches[:3]]}")
            if not found2:
                res2 = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=1001)
                raw2b = res2.get("result", {}).get("content", [{}])[0].get("text", "")
                if raw2b:
                    p2 = r'(\d+)\]\s*(🖱️)?\s*"([^"]+)"\s*\((\d+),(\d+)\)'
                    for _, ck, t2, x2, y2 in re.findall(p2, raw2b):
                        if re.sub(r'\s+', '', t2) == re.sub(r'\s+', '', target["text"]):
                            nx, ny = int(x2), int(y2)
                            if (nx, ny) != (target["x"], target["y"]):
                                print(f"      🔄 坐标已变化(get_ui_state): ({target['x']},{target['y']}) → ({nx},{ny})")
                            target = {"text": target["text"], "x": nx, "y": ny}
                            found2 = True
                            break
            if not found2:
                # 降级用 dump XML 再试
                raw2 = hf.dump_ui_xml(mcp)
                hf.invalidate_quick_cache()
                if raw2:
                    for n in hf.parse_nodes(raw2):
                        txt2 = (n["text"] or n["desc"] or "").strip()
                        if txt2 == target["text"]:
                            nx, ny = int(n["cx"]), int(n["cy"])
                            if (nx, ny) != (target["x"], target["y"]):
                                print(f"      🔄 坐标已变化(dump): ({target['x']},{target['y']}) → ({nx},{ny})")
                            target = {"text": target["text"], "x": nx, "y": ny}
                            found2 = True
                            break
            if not found2:
                print(f"      ❌ 点击前未找到目标文本 [{target['text']}]，中止（不盲点）")
                return
            stage("8.选片-点击")
            mcp.run_shell(f"~/rish -c 'input tap {target['x']} {target['y']}'")
            hf.invalidate_quick_cache()
            _tsleep(0.5)
            stage("9.选片-点击后处理")
            print("      ✅ 坐标点击完成")
            # 点完直接假定 detail（点搜索结果 99% 进详情/播放页），不 dump 判
            new_pt = "detail"
            pt_data = hf.load_page_types()
            if not (pkg in pt_data and "detail" in pt_data[pkg]):
                print(f"      📸 点击后假定 detail，触发建卡")
                import subprocess as _sp
                _script = "/data/data/com.termux/files/home/termux-mcp/capture_worker.py"
                _log = open("/data/data/com.termux/files/home/termux-mcp/web.log", "a")
                _sp.Popen(["python3", _script, pkg, "detail"], stdout=_log, stderr=_log)
                _tsleep(0.3)
            else:
                print(f"      ✅ detail 已有建卡，跳过")
            continue

        elif tool == "open_app":
            pkg = args.get("package_or_action", "")
            
            pkg = _dehallucinate_global(pkg, user_text)
            pkg = auto_resolve_pkg(mcp, pkg)
            if pkg == "NOT_FOUND":
                print(f"   ❌ 找不到应用")
                return
            print(f"   -> 拉起: {pkg}")
            if safe_launch(mcp, pkg):
                print(f"   ✅ {pkg} 已在前台")
                # 打开 App 应回首页，如果停在历史页，用 press_back 退避
                # 先确认目标 App 真的在前台（Termux 过渡期 get_current_pkg 可能是 Termux）
                for _wait in range(5):
                    _cur = hf.get_current_pkg(mcp)
                    if _cur and pkg in _cur:
                        break
                    _tsleep(0.3)
                hf.invalidate_quick_cache()
                _back_n = 0
                _unknown_seen = 0
                for _ in range(5):
                    _pt_open = hf.classify_page_ai(pkg, mcp)
                    if _pt_open in ("home", "search"):
                        break
                    if _pt_open == "unknown":
                        _unknown_seen += 1
                        if _unknown_seen >= 2:
                            break
                    _back_n += 1
                    print(f"   🔙 当前 {_pt_open}，press_back 退避 ({_back_n}/5)")
                    mcp.run_shell("~/rish -c 'input keyevent 4'")
                    _tsleep(0.6)
                    hf.invalidate_quick_cache()
            else:
                print(f"   ❌ {pkg} 启动失败")

        elif tool == "read_and_report":
            app_name = args.get("pkg", "")
            target_page = args.get("target_page", "home")
            question = args.get("question", user_text)
            pkg = _dehallucinate_global(app_name, user_text)
            pkg = auto_resolve_pkg(mcp, pkg)
            if pkg == "NOT_FOUND":
                print(f"   ❌ 找不到应用: {app_name}")
                return
            print(f"   -> 📖 浏览并总结: {pkg} -> {target_page}")
            if not safe_launch(mcp, pkg):
                print(f"   ❌ 启动失败")
                return
            stage("10.浏览-导航")
            _PAGE_TYPES = {"home", "search", "result", "detail", "player"}
            if target_page in _PAGE_TYPES:
                # 页面类型目标：用 classify_page_ai 判当前页，退避到目标页
                _pt = hf.classify_page_ai(pkg, mcp)
                print(f"   🧭 AI 判页面: {_pt}")
                if _pt != target_page and _pt not in ("unknown",):
                    for _ in range(3):
                        print(f"   🔙 当前 {_pt}，退避到 {target_page}")
                        mcp.run_shell("~/rish -c 'input keyevent 4'")
                        hf.invalidate_quick_cache()
                        _tsleep(0.5)
                        _pt = hf.classify_page_ai(pkg, mcp)
                        if _pt == target_page:
                            break
                        if _pt == "unknown":
                            print(f"   ⚠️ 退避后 unknown，停止退避")
                            break
                elif _pt == "unknown":
                    print(f"   ⚠️ AI 也判 unknown，直接抓当前页")
            else:
                # Tab/文本目标（如“排行榜”“社区”）：get_ui_state 找文本坐标，点击
                print(f"   👆 目标是 Tab/文本 [{target_page}]，找坐标点击")
                _r_nav = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=901)
                _raw_nav = _r_nav.get("result", {}).get("content", [{}])[0].get("text", "")
                _hit_xy = None
                for _ln in (_raw_nav or "").splitlines():
                    if f'"{target_page}"' in _ln:
                        _m = re.search(r'\((\d+),(\d+)\)', _ln)
                        if _m:
                            _hit_xy = (int(_m.group(1)), int(_m.group(2)))
                            break
                if _hit_xy:
                    print(f"   👆 点击 [{target_page}] @ ({_hit_xy[0]},{_hit_xy[1]})")
                    mcp.run_shell(f"~/rish -c 'input tap {_hit_xy[0]} {_hit_xy[1]}'")
                    hf.invalidate_quick_cache()
                    _tsleep(1.5)
                else:
                    print(f"   ⚠️ 页面里没找到 [{target_page}]，按当前页继续")
            stage("11.浏览-抓取")
            _r = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=900)
            _raw = _r.get("result", {}).get("content", [{}])[0].get("text", "")
            _texts = re.findall(r'"([^"]+)"', _raw or "")
            _joined = " ".join(_texts)[:2000]
            if not _joined:
                print(f"   ❌ 抓取页面内容失败")
                return
            stage("12.浏览-总结")
            _profile = {}
            try:
                _profile = hf.load_anchors().get(pkg, {}).get("profile", {}) or {}
            except Exception:
                pass
            _profile_hint = ""
            if _profile:
                _profile_hint = f"（App类型：{_profile.get('app_kind')}，内容卡：{_profile.get('content_kind')}）"
            _prompt = f"""用户在 {app_name} 的当前页面问：{question}{_profile_hint}
页面内容：{_joined}

请根据页面内容的实际结构回答用户：
1. 如果页面是列表/榜单（如 1. xxx  2. xxx），请直接逐条列出，不要合并。
2. 如果页面是文章详情，请分段总结。
3. 如果页面是聚合卡片（上面一条大卡片，下面一个列表），请分别总结。
只基于页面内容，不要编造。"""
            try:
                _summary, _ = ask_deepseek(_prompt, system="你是信息总结助手，只基于给定页面内容回答，不编造。")
            except Exception as e:
                print(f"   ❌ AI 总结失败: {e}")
                return
            print(f"\n📝 [总结] {_summary}\n")
            # 总结写到文件，Web 前端读取显示（消息形式）
            try:
                _report_file = os.path.expanduser("~/.config/termux-mcp/last_report.json")
                _report = {
                    "ts": _t.strftime("%Y-%m-%d %H:%M:%S"),
                    "pkg": pkg,
                    "app": app_name,
                    "target_page": target_page,
                    "question": question,
                    "summary": _summary,
                }
                with open(_report_file, "w", encoding="utf-8") as _f:
                    json.dump(_report, _f, ensure_ascii=False, indent=2)
                print(f"   💬 总结已写入 {_report_file}")
            except Exception as e:
                print(f"   ⚠️ 写总结文件失败: {e}")
            # 手机端通知（长文本，可展开）
            try:
                import subprocess as _sp
                _sp.Popen(["termux-notification", "--title", f"📝 {app_name} 总结", "--content", _summary])
            except Exception as e:
                print(f"   ⚠️ termux-notification 失败: {e}")
            # 保留 toast 作为轻提示（可选）
            try:
                mcp.call("show_toast", {"text": _summary[:100]})
            except Exception:
                pass
            continue
            
        elif tool == "assemble_and_run":
            pkg = args.get("pkg", "")
            pkg = _dehallucinate_global(pkg, user_text)
            pkg = auto_resolve_pkg(mcp, pkg)
            if pkg == "NOT_FOUND":
                print(f"   ❌ 找不到应用: {args.get('pkg', '')}")
                return
            print(f"   🧩 积木组装: {pkg}")
            stage("13.组装-执行")
            try:
                assemble_and_run(mcp, user_text, pkg=pkg)
            except Exception as e:
                print(f"   ❌ 组装执行异常: {e}")
            continue

        elif tool in load_macros():
            macro = load_macros()[tool]
            pkg = macro["pkg"]
            print(f"   -> 执行宏 {tool}: pkg={pkg}")
            _run_macro(tool, macro, pkg, args, mcp)

        elif tool in MCP_TOOLS:
            result = mcp.call(tool, args, req_id=100+i)
            _tsleep(1.5)
            print(f"   ✅ 结果: {json.dumps(result, ensure_ascii=False)[:100]}")
        else:
            print(f"   ✗ 未知工具: {tool}")

    print_stages()
    print_stats()
    print(f"\n[{_t.strftime('%H:%M:%S')}] 🏁 任务完成！")

MCP_TOOLS = {"open_app","click_by_text","click_by_id","click_by_class","find_element",
    "wait_for_element","get_ui_state","get_current_app","clipboard","text_to_speech",
    "show_toast","send_notification","open_url","set_volume","toggle_torch","vibrate",
    "get_device_info","get_battery_status","get_wifi_info","get_storage_info","get_screen_size"}

if __name__ == "__main__":
    main()
