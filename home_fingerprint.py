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
    try:
        mcp.run_shell(f"~/rish -c 'uiautomator dump {XML_REMOTE}'")
        res = mcp.run_shell(f"~/rish -c 'cat {XML_REMOTE}'")
        raw = res.get("result", {}).get("structuredContent", {}).get("result", "")
        if isinstance(raw, str) and "<hierarchy" in raw:
            return raw
        if isinstance(raw, dict):
            raw = raw.get("result") or raw.get("output") or ""
        return raw if isinstance(raw, str) else ""
    except Exception:
        return ""

def parse_nodes(xml):
    nodes = []
    if not xml or "<" not in xml:
        return nodes
    for m in re.finditer(r'<node\b[^>]*>', xml):
        tag = m.group(0)
        tm = re.search(r'\btext="([^"]*)"', tag)
        dm = re.search(r'\bcontent-desc="([^"]*)"', tag)
        bm = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
        if not bm:
            continue
        l, t, r, b = map(int, bm.groups())
        nodes.append({
            "text": (tm.group(1) if tm else "").strip(),
            "desc": (dm.group(1) if dm else "").strip(),
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

def get_screen_size(mcp):
    try:
        res = mcp.run_shell("wm size")
        raw = res.get("result", {}).get("structuredContent", {}).get("result", "")
        m = re.search(r"(\d+)x(\d+)", raw or "")
        if m:
            return int(m.group(1)), int(m.group(2))
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

def add_manual_sample(pkg, page_type, features, res_data=None):
    anchors = load_anchors()
    if pkg not in anchors:
        anchors[pkg] = {
            "display_name": pkg, "cold_start_cmd": f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1",
            "bottom_region_ratio": 0.85, "top_region_ratio": 0.15,
            "anchors": {"A": [], "B": [], "C": []}, "top_feature": {"A": {}, "B": [], "C": []}
        }

    # AI 提取的特征分级：最核心的放 A，次核心的放 B，其余的放 C（为了有的升降）
    def split_features(feats):
        if len(feats) >= 4:
            return feats[:1], feats[1:3], feats[3:]
        elif len(feats) >= 2:
            return feats[:1], feats[1:], []
        else:
            return feats, [], []

    a_feats, b_feats, c_feats = split_features(features)

    if page_type == "home":
        anchors[pkg]["anchors"]["A"] = a_feats
        anchors[pkg]["anchors"]["B"] = b_feats
        anchors[pkg]["anchors"]["C"] = c_feats
        if res_data and "elements" in res_data:
            anchors[pkg]["elements"] = res_data["elements"]
        _save(ANCHORS_FILE, anchors)
    
    else:
        pt = load_page_types()
        pkg_pt = pt.setdefault(pkg, {})
        entry = pkg_pt.setdefault(page_type, {"elements": {}, "anchors": {"A": [], "B": [], "C": []}})
        
        entry["anchors"]["A"] = a_feats
        entry["anchors"]["B"] = b_feats
        entry["anchors"]["C"] = c_feats
        if res_data and "elements" in res_data:
            entry["elements"] = res_data["elements"]
        entry["mature"] = True
        _save(PAGE_TYPES_FILE, pt)
    
    state = load_state()
    state[pkg] = {"cold_start_required": False}
    _save(STATE_FILE, state)
    return True



def get_current_pkg(mcp):
    """通过 dumpsys 获取当前前台包名"""
    try:
        res = mcp.run_shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
        raw = res.get("result", {}).get("structuredContent", {}).get("result", "")
        m = re.search(r"([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)+)/[a-zA-Z0-9_.]+", raw)
        if m: return m.group(1)
    except Exception:
        pass
    return ""
