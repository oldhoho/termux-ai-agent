#!/usr/bin/env python3
"""页面指纹 / 页面类型判定 / 冷启动校准 / 原子动作执行器的判定层。
数据源: uiautomator dump 的完整 XML。"""
import os, json, re, time

CONF_DIR = os.path.expanduser("~/.config/termux-mcp")
ANCHORS_FILE = os.path.join(CONF_DIR, "app_anchors.json")
STATS_FILE   = os.path.join(CONF_DIR, "anchor_stats.json")
STATE_FILE   = os.path.join(CONF_DIR, "app_home_state.json")
PAGE_TYPES_FILE = os.path.join(CONF_DIR, "page_types.json")

COLD_START_ROUNDS = 5
UPGRADE_K = 5
DOWNGRADE_M = 3
A_WARN_MISS = 5
PAGE_MATURE_ROUNDS = 5   # 页面类型样本成熟阈值

XML_REMOTE = "/sdcard/ui.xml"

def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass

def load_anchors(): return _load(ANCHORS_FILE, {})
def load_stats():   return _load(STATS_FILE, {})
def load_state():   return _load(STATE_FILE, {})
def load_page_types(): return _load(PAGE_TYPES_FILE, {})

def _norm(s):
    return re.sub(r"\s+", "", str(s or ""))

def _contains_anchor(texts, anchor):
    a = _norm(anchor)
    return any(a in _norm(t) for t in texts)

def _has_search_text(texts):
    for t in texts:
        n = _norm(t)
        if "搜索" in n or n.lower() == "search":
            return True
    return False

def dump_ui_xml(mcp):
    import time as _time
    start_t = _time.time()
    try:
        # 两段式超时：先 2s（快 App），失败再 5s（WebView App）
        for to in (2, 15):
            cmd = f"timeout {to} uiautomator dump {XML_REMOTE} > /dev/null 2>&1 && cat {XML_REMOTE}"
            res = mcp.run_shell(f"~/rish -c '{cmd}'")
            raw = res.get("result", {}).get("structuredContent", {}).get("result", "")
            if raw and "<hierarchy" in raw:
                return raw
        print(f"      ⏱️ [性能] 读取 UI 树超时或失败（2s+15s 均失败）")
        return ""
    except Exception:
        return ""

def parse_nodes(xml):
    import html as _html
    nodes = []
    if not xml or "<" not in xml:
        return nodes
    for m in re.finditer(r'<node\b[^>]*>', xml):
        tag = m.group(0)
        tm = re.search(r'\btext="([^"]*)"', tag)
        dm = re.search(r'\bcontent-desc="([^"]*)"', tag)
        bm = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
        cm = re.search(r'\bclickable="(true|false)"', tag)
        rim = re.search(r'\bresource-id="([^"]*)"', tag)
        clm = re.search(r'\bclass="([^"]*)"', tag)
        if not bm:
            continue
        l, t, r, b = map(int, bm.groups())
        _txt = _html.unescape(tm.group(1)) if tm else ""
        _desc = _html.unescape(dm.group(1)) if dm else ""
        nodes.append({
            "text": _txt.strip(),
            "desc": _desc.strip(),
            "clickable": (cm.group(1) == "true") if cm else False,
            "resource_id": (rim.group(1) if rim else ""),
            "class": (clm.group(1) if clm else ""),
            "bounds": [l, t, r, b],
            "cx": (l + r) // 2, "cy": (t + b) // 2,
        })
    return nodes

def _region_strings(nodes, screen_h, y_min_ratio, y_max_ratio):
    lo = screen_h * y_min_ratio
    hi = screen_h * y_max_ratio
    out = []
    for n in nodes:
        if lo <= n["cy"] <= hi:
            if n["text"]: out.append(n["text"])
            if n["desc"]: out.append(n["desc"])
    return out

_SCREEN_SIZE_CACHE = {}

def get_screen_size(mcp):
    _key = id(mcp)
    if _key in _SCREEN_SIZE_CACHE:
        return _SCREEN_SIZE_CACHE[_key]
    try:
        res = mcp.run_shell("wm size")
        raw = res.get("result", {}).get("structuredContent", {}).get("result", "")
        m = re.search(r"(\d+)x(\d+)", raw or "")
        if m:
            _r = (int(m.group(1)), int(m.group(2)))
            _SCREEN_SIZE_CACHE[_key] = _r
            return _r
    except Exception:
        pass
    return 1260, 2800

# ---------- 页面类型判定 ----------

def classify_page(pkg, mcp, xml=None):
    """返回 (page_type, detail)。page_type: home / search / unknown"""
    cfg = load_anchors().get(pkg)
    if not cfg:
        return "unknown", {"error": "no_config"}

    W, H = get_screen_size(mcp)
    if xml is None:
        xml = dump_ui_xml(mcp)
    nodes = parse_nodes(xml)
    if not nodes:
        return "unknown", {"node_count": 0}

    bottom = _region_strings(nodes, H, cfg.get("bottom_region_ratio", 0.85), 1.01)
    top    = _region_strings(nodes, H, -0.01, cfg.get("top_region_ratio", 0.15))

    # home 判定：A 锚点全中 + 顶部特征
    a_anchors = cfg["anchors"].get("A", [])
    # 🌟 核心修复：将精确相等改为包含匹配，防止 "首页" 匹配不上 "首页,标签"
    a_hits = [a for a in a_anchors if any(a in b for b in bottom)]
    a_miss = [a for a in a_anchors if a not in a_hits]
    top_a = cfg["top_feature"].get("A", {}) or {}
    top_ok = True
    if "has_search" in top_a:
        top_ok = (_has_search_text(top) == bool(top_a["has_search"]))

    is_home = (len(a_anchors) > 0) and (len(a_miss) == 0) and top_ok

    # 已成熟的页面类型锚点优先用于判定（比保守规则更准）
    pt = load_page_types().get(pkg, {})

    if is_home:
        return "home", {"node_count": len(nodes), "bottom": bottom, "top": top,
                        "A_hit": len(a_hits), "A_total": len(a_anchors), "A_miss": a_miss}

    # search 判定（成熟时用固化锚点，未成熟用保守规则）
    st = pt.get("search", {})
    if st.get("mature") and st.get("anchors"):
        s_anchors = st["anchors"]
        s_hits = [a for a in s_anchors if _contains_anchor(top + bottom, a)]
        if len(s_hits) == len(s_anchors):
            return "search", {"node_count": len(nodes), "bottom": bottom, "top": top, "rule": "mature_anchor"}
    # 保守规则：底部没有首页/我的 + 顶部有搜索相关
    bottom_has_home = _contains_anchor(bottom, "首页")
    top_has_search = _has_search_text(top) or any("搜索" in _norm(x) for x in top)
    if (not bottom_has_home) and top_has_search:
        return "search", {"node_count": len(nodes), "bottom": bottom, "top": top, "rule": "conservative"}

    return "unknown", {"node_count": len(nodes), "bottom": bottom, "top": top,
                       "A_miss": a_miss}

def judge_home(pkg, mcp, xml=None):
    pt, detail = classify_page(pkg, mcp, xml)
    return pt == "home", detail

# ---------- 自动抓样本 / 成熟机制 ----------

def auto_capture_page(pkg, page_type, mcp, xml=None):
    """执行到某页时自动抓一帧，若该页类型未成熟则存样本并结算。"""
    if page_type not in ("home", "search"):
        return
    pt_all = load_page_types()
    pkg_pt = pt_all.setdefault(pkg, {})
    entry = pkg_pt.setdefault(page_type, {"samples": [], "mature": False, "anchors": []})
    if entry.get("mature"):
        return

    if xml is None:
        xml = dump_ui_xml(mcp)
    nodes = parse_nodes(xml)
    if not nodes:
        return

    # 提取该类型的关键字符串作为样本特征（取顶部或底部区域）
    cfg = load_anchors().get(pkg, {})
    W, H = get_screen_size(mcp)
    feats = _region_strings(nodes, H, -0.01, 0.25) if page_type == "search" \
            else _region_strings(nodes, H, cfg.get("bottom_region_ratio", 0.85), 1.01)

    entry["samples"].append(feats)
    if len(entry["samples"]) > 20:
        entry["samples"] = entry["samples"][-20:]

    # 成熟结算：样本数够 + 样本间相似度稳定
    if len(entry["samples"]) >= PAGE_MATURE_ROUNDS:
        # 找出现频次最高的 anchor 词（简单词频，取 top3）
        from collections import Counter
        cnt = Counter()
        for s in entry["samples"]:
            for w in s:
                n = _norm(w)
                if 1 <= len(n) <= 10:
                    cnt[n] += 1
        common = [w for w, c in cnt.most_common(5) if c >= PAGE_MATURE_ROUNDS]
        if common:
            entry["mature"] = True
            entry["anchors"] = common[:3]
            print(f"   🎓 页面类型成熟: {pkg}/{page_type}，锚点={entry['anchors']}")

    _save(PAGE_TYPES_FILE, pt_all)

# ---------- 首页冷启动校准（沿用） ----------

def wait_for_home_ready(pkg, mcp, timeout=12, interval=0.8):
    cfg = load_anchors().get(pkg) or {}
    a_anchors = cfg.get("anchors", {}).get("A", [])
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        xml = dump_ui_xml(mcp)
        if xml:
            last = xml
            nodes = parse_nodes(xml)
            W, H = get_screen_size(mcp)
            bottom = _region_strings(nodes, H, cfg.get("bottom_region_ratio", 0.85), 1.01)
            if any(_contains_anchor(bottom, a) for a in a_anchors):
                return xml
        time.sleep(interval)
    return last

def record_cold_start_sample(pkg, mcp, xml=None):
    if xml is None:
        xml = dump_ui_xml(mcp)
    is_home, detail = judge_home(pkg, mcp, xml)
    if not is_home:
        return False, detail, False
    stats = load_stats()
    st = stats.setdefault(pkg, {"rounds": 0, "A_miss_streak": {}, "B_streak": {}, "C_streak": {}, "mature": False})
    st["rounds"] += 1
    cfg = load_anchors().get(pkg, {})
    bottom = detail.get("bottom", [])
    for a in cfg["anchors"].get("A", []):
        hit = _contains_anchor(bottom, a)
        st["A_miss_streak"][a] = 0 if hit else st["A_miss_streak"].get(a, 0) + 1
        if not hit and st["A_miss_streak"][a] >= A_WARN_MISS:
            print(f"   ⚠️ 锚点告警: {pkg} A级「{a}」连续 {A_WARN_MISS} 次未现")
    for c in list(cfg["anchors"].get("C", [])):
        hit = _contains_anchor(bottom, c)
        st["C_streak"][c] = 0 if not hit else st["C_streak"].get(c, 0) + 1
        if st["C_streak"][c] >= UPGRADE_K:
            cfg["anchors"]["C"].remove(c); cfg["anchors"].setdefault("B", []).append(c)
            st["B_streak"][c] = 0
            print(f"   🔼 锚点升级 C→B: {pkg} 「{c}」")
    for b in list(cfg["anchors"].get("B", [])):
        hit = _contains_anchor(bottom, b)
        st["B_streak"][b] = 0 if hit else st["B_streak"].get(b, 0) + 1
        if st["B_streak"][b] >= DOWNGRADE_M:
            cfg["anchors"]["B"].remove(b); cfg["anchors"].setdefault("C", []).append(b)
            st["B_streak"][b] = 0
            print(f"   🔽 锚点降级 B→C: {pkg} 「{b}」")
    all_anchors = load_anchors(); all_anchors[pkg] = cfg; _save(ANCHORS_FILE, all_anchors)
    mature = st["rounds"] >= COLD_START_ROUNDS
    if mature and not st.get("mature"):
        st["mature"] = True
        state = load_state(); state[pkg] = {"cold_start_required": False, "matured_at_round": st["rounds"]}; _save(STATE_FILE, state)
        print(f"   🎓 首页指纹成熟: {pkg} 完成 {st['rounds']} 轮校准")
    _save(STATS_FILE, stats)
    return True, detail, st.get("mature", False)

def cold_start_required(pkg):
    state = load_state().get(pkg)
    return True if state is None else bool(state.get("cold_start_required", True))


# ---------- 锚点管理辅助（供 web_server API 调用） ----------

def anchors_snapshot():
    """返回所有 App 的锚点配置 + 校准状态，供前端展示。"""
    anchors = load_anchors()
    stats = load_stats()
    state = load_state()
    pages = load_page_types()
    out = {}
    for pkg, cfg in anchors.items():
        st = stats.get(pkg, {})
        sd = state.get(pkg, {})
        out[pkg] = {
            "display_name": cfg.get("display_name", pkg),
            "anchors": cfg.get("anchors", {"A": [], "B": [], "C": []}),
            "top_feature": cfg.get("top_feature", {}),
            "rounds": st.get("rounds", 0),
            "mature": bool(sd.get("cold_start_required", True) is False),
            "layout": cfg.get("layout", {}),
            "element_meta": cfg.get("element_meta", {}),
            "profile": cfg.get("profile", {}),
            "cold_start_required": sd.get("cold_start_required", True),
            "page_types": pages.get(pkg, {}),
        }
    return out

def update_anchor_level(pkg, level, anchor_list):
    """覆盖某个 pkg 的某一级锚点（A/B/C）。"""
    anchors = load_anchors()
    if pkg not in anchors:
        return False, "pkg not found"
    level = level.upper()
    if level not in ("A", "B", "C"):
        return False, "bad level"
    anchors[pkg].setdefault("anchors", {})[level] = list(anchor_list)
    _save(ANCHORS_FILE, anchors)
    return True, "ok"

def delete_anchor(pkg, level, anchor):
    anchors = load_anchors()
    if pkg not in anchors:
        return False, "pkg not found"
    level = level.upper()
    arr = anchors[pkg].get("anchors", {}).get(level, [])
    if anchor in arr:
        arr.remove(anchor)
        _save(ANCHORS_FILE, anchors)
        return True, "ok"
    return False, "anchor not found"

def reset_app_calibration(pkg):
    anchors = load_anchors()
    if pkg not in anchors:
        return False, "pkg not found"
    state = load_state()
    state[pkg] = {"cold_start_required": True}
    _save(STATE_FILE, state)
    stats = load_stats()
    stats[pkg] = {"rounds": 0, "A_miss_streak": {}, "B_streak": {}, "C_streak": {}, "mature": False}
    _save(STATS_FILE, stats)
    return True, "reset"

def delete_app(pkg):
    anchors = load_anchors()
    if pkg in anchors:
        del anchors[pkg]
        _save(ANCHORS_FILE, anchors)
    for path, key in ((STATE_FILE, "state"), (STATS_FILE, "stats"), (PAGE_TYPES_FILE, "pages")):
        d = _load(path, {})
        if pkg in d:
            del d[pkg]
            _save(path, d)
    return True, "deleted"

def confirm_page_type_anchor(pkg, page_type, anchors_list):
    """把候选锚点确认到某页面类型的固化锚点，并标成熟。"""
    pt = load_page_types()
    pkg_pt = pt.setdefault(pkg, {})
    entry = pkg_pt.setdefault(page_type, {"samples": [], "mature": False, "anchors": []})
    entry["anchors"] = list(anchors_list)
    entry["mature"] = True
    _save(PAGE_TYPES_FILE, pt)
    return True, "confirmed"


# ---------- 引导式采集辅助 ----------

def capture_and_extract(pkg, page_type, mcp):
    xml = dump_ui_xml(mcp)
    if not xml or len(xml) < 100:
        return False, "未能抓取到 UI 树"
    
    W, H = get_screen_size(mcp)
    nodes = parse_nodes(xml)
    if not nodes:
        return False, "UI 树为空"

    clean_feats = []
    element_map = {}

    for n in nodes:
        txt = n["text"] or n["desc"]
        if not txt or len(txt) < 1: continue
        
        cy = n["cy"]
        w = n["bounds"][2] - n["bounds"][0]
        h = n["bounds"][3] - n["bounds"][1]
        
        # 1. 提取稳定的 A 级锚点（只取顶部和底部的固定 UI，绝不取中间动态流）
        if cy < H * 0.15 or cy > H * 0.85:
            # 过滤掉纯数字（如播放量）和过长文本（如视频标题）
            if not txt.replace('.', '').isdigit() and len(txt) < 12:
                clean_feats.append(txt)
        
        # 2. 提取“元素图纸”（只提取有实际点击意义的控件）
        # 条件：宽高足够大，或者带有明确的按钮特征
        is_button = any(k in txt for k in ["搜索", "发送", "首页", "我的", "新建", "对话", "发布", "侧边栏"])
        if (w > 200 and h > 80 and cy > H * 0.15 and cy < H * 0.85) or is_button:
            element_map[txt] = [round(n["cx"] / W, 3), round(n["cy"] / H, 3)]

    return True, {
        "features": list(dict.fromkeys(clean_feats)), 
        "node_count": len(nodes),
        "elements": element_map
    }

def add_manual_sample(pkg, page_type, features, res_data=None, force=False, a_full=False):
    # 🌟 核心修复：如果用户已经手动调整过（或者卡片已存在），默认不覆盖！
    if not force:
        anchors = load_anchors()
        if pkg in anchors and anchors[pkg].get("mature"):
            if page_type == "home":
                return True
            pt = load_page_types()
            if pkg in pt and page_type in pt[pkg]:
                return True
    # 1. 确保 app_anchors.json 里有这个 App 的主卡片
    anchors = load_anchors()
    if pkg not in anchors:
        anchors[pkg] = {
            "display_name": pkg, "cold_start_cmd": f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1",
            "bottom_region_ratio": 0.85, "top_region_ratio": 0.15,
            "anchors": {"A": [], "B": [], "C": []}, 
            "top_feature": {"A": {}, "B": [], "C": []},
            "page_types": {} # 🌟 初始化页面类型归档
        }
    if "page_types" not in anchors[pkg]:
        anchors[pkg]["page_types"] = {}

    # 2. 如果存的是首页，直接更新主卡片的 A/B/C 锚点
    if page_type == "home":
        if a_full:
            anchors[pkg]["anchors"]["A"] = features
            anchors[pkg]["anchors"]["B"] = []
        elif len(features) >= 3:
            anchors[pkg]["anchors"]["A"] = features[:2]
            anchors[pkg]["anchors"]["B"] = features[2:]
        else:
            anchors[pkg]["anchors"]["A"] = features
            anchors[pkg]["anchors"]["B"] = []
        if res_data and "elements" in res_data:
            anchors[pkg]["elements"] = res_data["elements"]
        if res_data and "element_meta" in res_data:
            anchors[pkg]["element_meta"] = res_data["element_meta"]
        if res_data and "layout" in res_data:
            anchors[pkg]["layout"] = res_data["layout"]
        anchors[pkg]["mature"] = True
        
    # 3. 如果是其他页面，统一塞进 page_types 里面
    else:
        # 🌟 核心修复：如果这个页面没建过，初始化字典，绝不覆盖已有数据
        pt = load_page_types()
        pkg_pt = pt.setdefault(pkg, {})
        entry = pkg_pt.setdefault(page_type, {"anchors": {"A": [], "B": [], "C": []}, "elements": {}})
        if "elements" not in entry: entry["elements"] = {}
        
        if a_full:
            entry["anchors"]["A"] = features
            entry["anchors"]["B"] = []
        elif len(features) >= 3:
            entry["anchors"]["A"] = features[:2]
            entry["anchors"]["B"] = features[2:]
        else:
            entry["anchors"]["A"] = features
            entry["anchors"]["B"] = []
            
        if res_data and "elements" in res_data:
            entry["elements"] = res_data["elements"]
        if res_data and "element_meta" in res_data:
            entry["element_meta"] = res_data["element_meta"]
        if res_data and "layout" in res_data:
            entry["layout"] = res_data["layout"]
        if res_data and "edges" in res_data:
            entry["edges"] = res_data["edges"]
        entry["mature"] = True
        _save(PAGE_TYPES_FILE, pt)

    _save(ANCHORS_FILE, anchors)
    
    state = load_state()
    state[pkg] = {"cold_start_required": False}
    _save(STATE_FILE, state)
    return True



def get_current_pkg(mcp):
    """通过 dumpsys 获取当前前台包名（只读，绕过锁屏检查）"""
    try:
        _fn = getattr(mcp, "_raw_call", None)
        if _fn:
            res = _fn("execute_command", {"command": "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'"}, req_id=99)
        else:
            res = mcp.run_shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
        raw = res.get("result", {}).get("structuredContent", {}).get("result", "")
        m = re.search(r"([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)+)/[a-zA-Z0-9_.]+", raw)
        if m: return m.group(1)
    except Exception:
        pass
    return ""


_SCREEN_ON_CACHE = {"ts": 0.0, "val": True}
_SCREEN_ON_TTL = 2.0

def is_screen_on(mcp):
    """通用检测：当前屏幕是否点亮且未锁屏（0.5s TTL 缓存）"""
    import time as _t
    now = _t.time()
    if now - _SCREEN_ON_CACHE["ts"] < _SCREEN_ON_TTL:
        return _SCREEN_ON_CACHE["val"]
    def _raw(cmd):
        try:
            return mcp.call("execute_command", {"command": cmd}, req_id=99)
        except Exception:
            return {}
    try:
        # 只查 isKeyguardShowing（唯一可靠信号，省一次 dumpsys）
        res = _raw("~/rish -c 'dumpsys window | grep isKeyguardShowing'")
        raw = res.get("result", {}).get("structuredContent", {}).get("result", "") or ""
        if "isKeyguardShowing=true" in raw:
            _SCREEN_ON_CACHE["ts"] = now
            _SCREEN_ON_CACHE["val"] = False
            return False
        _SCREEN_ON_CACHE["ts"] = now
        _SCREEN_ON_CACHE["val"] = True
        return True
    except Exception:
        return True

_QUICK_TEXTS_CACHE = {"ts": 0.0, "text": ""}
_QUICK_TTL = 1.5

def invalidate_quick_cache():
    _QUICK_TEXTS_CACHE["ts"] = 0.0
    _QUICK_TEXTS_CACHE["text"] = ""

def get_quick_texts(mcp, pkg=None):
    """极轻量抓取，带降级和详细日志"""
    import time as _t
    now = _t.time()
    if _QUICK_TEXTS_CACHE["text"] and (now - _QUICK_TEXTS_CACHE["ts"]) < _QUICK_TTL:
        return _QUICK_TEXTS_CACHE["text"]
    try:
        # 第一步：尝试正常抓取全量 XML
        cmd = f"uiautomator dump {XML_REMOTE} > /dev/null 2>&1 && cat {XML_REMOTE}"
        res = mcp.run_shell(f"~/rish -c '{cmd}'")
        raw = res.get("result", {}).get("structuredContent", {}).get("result", "")
        
        # 如果抓到了，直接提取文字
        if raw and "<hierarchy" in raw:
            texts = re.findall(r'text="([^"]+)"', raw)
            descs = re.findall(r'content-desc="([^"]+)"', raw)
            all_strings = texts + descs
            # 文本少时补 get_ui_state 文本（WebView App 常丢 text 属性）
            if len(all_strings) < 20:
                try:
                    _r = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=556)
                    _raw = _r.get("result", {}).get("content", [{}])[0].get("text", "")
                    _extra = re.findall(r'"([^"]+)"', _raw or "")
                    for _t in _extra:
                        if _t not in all_strings and len(_t) > 0:
                            all_strings.append(_t)
                except Exception:
                    pass
            joined = " ".join(all_strings)
            print(f"      🐞 [正常抓取] 提取到 {len(all_strings)} 个文本节点")
            print(f"      📝 [文本列表] {all_strings[:60]}")
            _QUICK_TEXTS_CACHE["ts"] = now
            _QUICK_TEXTS_CACHE["text"] = joined
            return joined
            
        # 第二步：如果抓取失败或者超时，走降级方案，直接用 dumpsys 拿窗口里的文字！
        # dumpsys window windows 会包含很多文本，虽然乱，但不走 uiautomator，绝对不会卡死
        print("      🐞 [降级抓取] uiautomator 超时，尝试通过 dumpsys 强行获取文字...")
        res = mcp.run_shell("~/rish -c 'dumpsys window windows | grep -E \"mCurrentFocus|mFocusedApp\"'")
        raw2 = res.get("result", {}).get("structuredContent", {}).get("result", "")
        
        # 提取所有可能是文字的片段
        if raw2:
            print(f"      🐞 [降级抓取] dumpsys 拿到部分信息，长度: {len(raw2)}")
            print(f"      📝 [文本-降级] {raw2[:500]}")
            return raw2
            
        print("      ❌ 致命错误：底层完全无法获取屏幕任何内容")
        return ""
    except Exception as e:
        print(f"      ⚠️ 抓取异常: {e}")
        return ""
import hashlib as _hashlib

import os as _os, json as _json
_SIG_CACHE_FILE = _os.path.expanduser("~/.config/termux-mcp/sig_cache.json")
try:
    _SIG_PAGE_MAP = _json.load(open(_SIG_CACHE_FILE, encoding="utf-8"))
except Exception:
    _SIG_PAGE_MAP = {}
_PAGE_SIG_MAP = {}

def _save_sig_cache():
    try:
        _json.dump(_SIG_PAGE_MAP, open(_SIG_CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass

def page_signature(mcp):
    """稳定页面指纹：只取短文本（Tab/按钮名），忽略动态内容"""
    try:
        fn = getattr(mcp, "_raw_call", None) or mcp.call
        r = fn("get_ui_state", {"include_screenshot": False}, req_id=777)
        raw = r.get("result", {}).get("content", [{}])[0].get("text", "")
        if not raw:
            return ""
        texts = re.findall(r'"([^"]+)"', raw)
        # 只取稳定短文本：长度 2~6，非纯数字，非含数字
        stable = sorted(set(
            t for t in texts
            if 2 <= len(t) <= 6 and not any(c.isdigit() for c in t)
        ))
        if not stable:
            return ""
        return _hashlib.md5("|".join(stable).encode()).hexdigest()[:12]
    except Exception:
        return ""

def classify_by_anchors_fast(pkg, mcp):
    """用锚点 A 级特征直接匹配 get_ui_state 文本，不做 hash，最稳定"""
    try:
        fn = getattr(mcp, "_raw_call", None) or mcp.call
        r = fn("get_ui_state", {"include_screenshot": False}, req_id=779)
        raw = r.get("result", {}).get("content", [{}])[0].get("text", "")
        if not raw:
            return None
        texts = re.findall(r'"([^"]+)"', raw)
        joined = " ".join(texts)
        pt = load_page_types().get(pkg, {})
        for ptype, pdata in pt.items():
            feats = pdata.get("anchors", {}).get("A", [])
            if feats and all(f in joined for f in feats):
                print(f"      ⚡ [锚点直匹] {ptype} 命中 A级={feats}")
                return ptype, joined
        cfg = load_anchors().get(pkg, {})
        home_feats = cfg.get("anchors", {}).get("A", [])
        if home_feats and all(f in joined for f in home_feats):
            print(f"      ⚡ [锚点直匹] home 命中 A级={home_feats}")
            return "home", joined
        return None, ""
    except Exception:
        return None

def classify_by_signature(mcp):
    sig = page_signature(mcp)
    return _SIG_PAGE_MAP.get(sig) if sig else None, sig

def remember_signature(page_type, sig):
    if sig and page_type and page_type != "unknown":
        _SIG_PAGE_MAP[sig] = page_type
        _PAGE_SIG_MAP[page_type] = sig
        _save_sig_cache()

def classify_page_quick(pkg, mcp, return_text=False, skip_fallback=False):
    """严格页面判定：先查指纹缓存，未命中再 dump XML"""
    # 1. 锚点 A 级直接匹配 get_ui_state（最快最稳，2 秒）
    fast, fast_txt = classify_by_anchors_fast(pkg, mcp)
    if fast:
        if return_text:
            return fast, fast_txt  # 复用 get_ui_state 文本，不再 dump
        return fast
    # 2. 指纹缓存命中
    cached, sig = classify_by_signature(mcp)
    if cached:
        print(f"      ⚡ [指纹] 命中 {cached} (sig={sig})，跳过 dump")
        if return_text:
            return cached, ""
        return cached
    # 3. 未命中，dump XML 判定
    txt = get_quick_texts(mcp, pkg=pkg)
    if not txt:
        result = "unknown"
    else:
        result = _classify_from_text(pkg, txt)
    # 判定失败时，用 get_ui_state 文本补判一次（WebView App 常见丢 text）
    if result == "unknown" and not skip_fallback:
        try:
            _r = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=557)
            _raw = _r.get("result", {}).get("content", [{}])[0].get("text", "")
            _extra = re.findall(r'"([^"]+)"', _raw or "")
            if _extra:
                txt2 = txt + " " + " ".join(_extra)
                print(f"      🧭 [补判] 用 get_ui_state 文本补判 ({len(_extra)} 条)")
                result = _classify_from_text(pkg, txt2)
        except Exception:
            pass
    remember_signature(result, sig)
    if return_text:
        return result, txt
    return result

def _classify_from_text(pkg, txt):
    cfg = load_anchors().get(pkg, {})
    if not cfg:
        print(f"       [判定] {pkg} 无锚点配置  unknown")
        return "unknown"

    pt = load_page_types().get(pkg, {})

    # 1. 首页判定优先（home 是基础页面，A 级和 result 重叠时 home 优先）
    home_feats = cfg.get("anchors", {}).get("A", [])
    if home_feats:
        miss = [f for f in home_feats if f not in txt]
        if not miss:
            print(f"       [判定] home 命中 A级={home_feats}")
            return "home"
        print(f"       [判定] home 缺{miss}")

    # 2. 遍历所有 page_type（result/player/detail/search...），A 级全命中即返回
    for ptype, pdata in pt.items():
        feats = pdata.get("anchors", {}).get("A", [])
        if not feats:
            continue
        miss = [f for f in feats if f not in txt]
        if not miss:
            print(f"       [判定] {ptype} 命中 A级={feats}")
            return ptype
        print(f"       [判定] {ptype} 缺{miss}")

    print(f"       [判定] 所有 page_type A级均未全命中  unknown")
    return "unknown"

def classify_page_ai(pkg, mcp):
    """用 AI 极速判断页面类型，通用且准确"""
    txt = get_quick_texts(mcp, pkg=pkg)
    if not txt: return "unknown"
    
    # 截取前 1000 个字符，覆盖更多元素
    txt_snippet = txt[:1000].replace('"', '')

    prompt = f"""这是一个安卓App的屏幕文本。请判断这是什么页面：
文本："{txt_snippet}"
请只输出一个单词：
home（首页）、search（有搜索框/历史搜索/搜索发现/热搜的搜索页）、result（搜索结果列表）、detail（视频/商品详情/播放页）、ad（广告/开屏/登录页）、unknown（其他）。
"""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("dispatch", "/data/data/com.termux/files/home/termux-mcp/dispatch.py")
        dispatch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dispatch)
        res, _ = dispatch.ask_deepseek(prompt, system="你是安卓页面类型判断器，只输出一个单词，不解释。")
        res = res.strip().lower().replace("'", "").replace('"', '')
        
        # 容错匹配
        if "home" in res: return "home"
        if "result" in res: return "result"
        if "search" in res: return "search"
        if "detail" in res: return "detail"
        if "ad" in res: return "ad"
        return "unknown"
    except Exception as e:
        print(f"      ⚠️ AI 裁判异常: {e}")
        return "unknown"


def dump_ui_debug(mcp):
    """安全抓取用于调试：保存前 2000 字符，并提取干净的文字列表"""
    cmd = f"uiautomator dump {XML_REMOTE} > /dev/null 2>&1 && cat {XML_REMOTE}"
    res = mcp.run_shell(f"~/rish -c '{cmd}'")
    raw = res.get("result", {}).get("structuredContent", {}).get("result", "")
    
    if not raw or "<hierarchy" not in raw:
        print("❌ 调试抓取失败，未获得 XML 树")
        return
        
    print(f"✅ 抓取成功，XML 总长度: {len(raw)} 字符")
    
    # 保存到文件供你查看
    with open("/data/data/com.termux/files/home/termux-mcp/ui_debug.xml", "w", encoding="utf-8") as f:
        f.write(raw)
    print("📁 XML 已保存至 ~/termux-mcp/ui_debug.xml")
    
    # 提取干净的文字
    texts = re.findall(r'text="([^"]+)"', raw)
    descs = re.findall(r'content-desc="([^"]+)"', raw)
    
    all_texts = [t for t in texts if t.strip()] + [d for d in descs if d.strip()]
    
    print(f"📊 共提取到 {len(all_texts)} 个有效文本节点")
    print("=" * 40)
    print("前 50 个节点内容如下：")
    for i, t in enumerate(all_texts[:50]):
        print(f"  [{i}] {t}")
    print("=" * 40)
