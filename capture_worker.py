#!/usr/bin/env python3
import sys, os, time, json, subprocess, signal, re

# 30 秒硬超时，防建卡进程卡死残留
def _timeout_handler(signum, frame):
    print("TIMEOUT: 建卡超过 30 秒，强制退出")
    sys.exit(3)
signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(60)

sys.path.insert(0, os.path.expanduser("~/termux-mcp"))
import home_fingerprint as hf

if len(sys.argv) < 3: sys.exit(1)
pkg = sys.argv[1]
page_type = sys.argv[2]
from_page = sys.argv[3] if len(sys.argv) > 3 else None

try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("dispatch", "/data/data/com.termux/files/home/termux-mcp/dispatch.py")
    dispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch)
    mcp = dispatch.MCP()
    mcp.initialize()

    # 通用查重：该页面已有建卡数据则跳过抓取，但如果给了 from_page，补 parent 后退出
    pt_data = hf.load_page_types()
    if pkg in pt_data and page_type in pt_data[pkg]:
        if from_page:
            try:
                pt_data[pkg][page_type]["parent"] = from_page
                import json as _j
                _j.dump(pt_data, open(hf.PAGE_TYPES_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
                print(f"   🔗 补记录退避路径: {page_type} -> {from_page}")
            except Exception as _e:
                print(f"   ⚠️ 补 parent 失败: {_e}")
        print(f"SKIP: {pkg} -> {page_type} 已有建卡数据，跳过")
        sys.exit(0)
    
    # 🚨 绝对防御：刚唤醒建卡脚本，立刻查是不是锁屏了
    if not hf.is_screen_on(mcp):
        print("FAIL: 建卡前检测到锁屏，立即退出！")
        sys.exit(0)

    # 只在前台不是目标 App 时冷启动；dispatch 调用时 App 已在前台，不重启
    _cur = hf.get_current_pkg(mcp)
    if not _cur or pkg not in _cur:
        print(f"    建卡冷启动 {pkg}（force-stop + monkey）")
        mcp.run_shell(f"~/rish -c 'pm enable {pkg}'")
        time.sleep(0.5)
        mcp.run_shell(f"~/rish -c 'am force-stop {pkg}'")
        time.sleep(1.0)
        mcp.run_shell(f"~/rish -c 'monkey -p {pkg} -c android.intent.category.LAUNCHER 1'")
        time.sleep(4.0)
    else:
        print(f"    {pkg} 已在前台，跳过冷启动")
    if True:
        # 扫广告/弹窗
        for _ in range(2):
            _r_ad = mcp.call("get_ui_state", {"include_screenshot": False}, req_id=800)
            _raw_ad = _r_ad.get("result", {}).get("content", [{}])[0].get("text", "")
            _hit_ad = None
            for _ln in (_raw_ad or "").splitlines():
                if any(t in _ln for t in ("跳过", "关闭", "×", "广告")):
                    _m = re.search(r'\((\d+),(\d+)\)', _ln)
                    if _m:
                        _hit_ad = (int(_m.group(1)), int(_m.group(2)))
                        break
            if _hit_ad:
                mcp.run_shell(f"~/rish -c 'input tap {_hit_ad[0]} {_hit_ad[1]}'")
                time.sleep(1.0)
        _cur2 = hf.get_current_pkg(mcp)
        if not _cur2 or pkg not in _cur2:
            print(f"FAIL: 拉起后仍不在前台（当前 {_cur2}）")
            sys.exit(0)
        hf.invalidate_quick_cache()
        print(f"   ✅ {pkg} 已在前台")
    
    # 🌟 核心修复：轮询等待屏幕真正渲染出来（至少包含一些节点，且不是开屏广告）
    xml = ""
    for _ in range(10):
        xml = hf.dump_ui_xml(mcp)
        if xml and "<hierarchy" in xml and len(xml) > 2000:
            break
        time.sleep(1.0)
        
    if not xml:
        print("FAIL: 等待超时，无法获取界面特征")
        sys.exit(2)

    W, H = hf.get_screen_size(mcp)
    nodes = hf.parse_nodes(xml)
    candidates = []
    # 记录抓取失败的次数
    failed_reads = 0

    # 无文本的 search/input 节点（搜索入口），只保留 bounds 最大的（父容器）
    _search_entries = [
        n for n in nodes
        if not (n["text"] or n["desc"])
        and ("search" in (n.get("resource_id") or "").lower() or "input" in (n.get("resource_id") or "").lower())
    ]
    _biggest_search = None
    if _search_entries:
        _biggest_search = max(
            _search_entries,
            key=lambda n: (n["bounds"][2]-n["bounds"][0]) * (n["bounds"][3]-n["bounds"][1])
        )

    for n in nodes:
        txt = n["text"] or n["desc"]
        _is_search_entry = (n is _biggest_search)
        if (txt and len(txt) > 1) or _is_search_entry:
            candidates.append({
                "text": txt if txt else "（搜索入口）",
                "x": round(n["cx"] / W, 3),
                "y": round(n["cy"] / H, 3),
            })
            
    prompt_templates = {
        "home": "当前界面是首页。请提取底部Tab或顶部搜索框等固定元素。务必提取至少3~6个固定文字特征。",
        "search": "当前界面是搜索页。请提取搜索输入框、清空按钮。**特别注意：如果你发现可以跳到结果页的按钮（比如输入框的回车/搜索按钮），请输出 edges 数组，格式为 [{\"target\": \"result\", \"action\": \"输入并回车\"}]**。",
        "result": "当前界面是搜索结果页。请提取顶部的分类Tab（综合/全部/筛选/排序等），**绝对不要提取搜索词、视频标题、播放量、UP主名**。",
        "player": "当前界面是播放页/视频详情页。请提取播放控制按钮（播放/暂停/下一集/倍速）、评论入口、返回按钮、分享/收藏/点赞/投币等固定功能按钮。不要提取视频标题、UP主名、评论内容。",
        "detail": "当前界面是详情页（商品/文章/帖子详情）。请提取返回按钮、购买/加入购物车、分享、收藏、评论入口等固定功能按钮。不要提取商品标题、价格、正文内容。",
    }
    
    # result 页特征只取顶部 Tab 区（y < 0.25），内容区标题不作为特征
    if page_type == "result":
        top_candidates = [c for c in candidates if c["y"] < 0.25]
        candidates_for_prompt = top_candidates if top_candidates else candidates
        print(f"   📐 [建卡] result 特征仅取顶部区: {len(candidates_for_prompt)}/{len(candidates)}")
    else:
        candidates_for_prompt = candidates

    _profile_hint = ""
    if page_type == "home":
        _profile_hint = '另外，请输出该 App 的 profile 字段（只基于当前屏幕内容判断，不要编造）："profile": {"app_kind": "video_platform|game_store|social|shopping|reading|tool|other 之一", "content_kind": "主页内容卡类型，如：视频卡/游戏卡/商品卡/帖子/聊天/其他", "home_content_hint": "一句话描述主页内容卡长什么样", "click_target_hint": "一句话描述点内容卡会进什么页面"}'
    _profile_schema = ', "profile": {...}' if page_type == 'home' else ''
    prompt = f"""你是一个安卓 UI 分析专家。{prompt_templates.get(page_type, prompt_templates['home'])}
重要：features 只填页面固定元素（Tab/按钮/输入框），不要填用户搜索词、视频标题、商品名等内容。{_profile_hint}
请严格输出 JSON 格式：{{"features": ["文字1", "文字2"], "elements": {{"功能名": [x, y]}}, "edges": [{{"target": "页面类型", "action": "动作名"}}]{_profile_schema}}}
候选数据：\n{json.dumps(candidates_for_prompt, ensure_ascii=False)}
"""
    res_text, _ = dispatch.ask_deepseek(prompt)
    res_text = res_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    ai_data = json.loads(res_text)
    
    features = ai_data.get("features", [])
    elements = ai_data.get("elements", {})
    edges = ai_data.get("edges", [])
    # 通用过滤：features 必须是 candidates 里真实出现过的屏幕文字（排除 AI 编的抽象名）
    cand_texts = set()
    for c in candidates:
        cand_texts.add(c["text"])
    filtered = [f for f in features if any(f == c or f in c for c in cand_texts)]
    if filtered:
        print(f"   🧹 [建卡] features 过滤: {len(features)} → {len(filtered)}（保留屏幕真实文字）")
        features = filtered
    else:
        print(f"   ⚠️ [建卡] features 无一项在屏幕文字里，保留全部 {len(features)} 项")
    
    # 🚨 核心修复：针对不同页面类型，做严格的废卡拦截
    # search 卡 A 级：从 candidates 里取搜索页独有元素（（搜索框）/清空/返回），不硬编码
    if page_type == "search":
        _ed_features = []
        for _c in candidates:
            _t = _c["text"]
            if _t == "（搜索框）" or "输入" in _t or "清空" in _t or "返回" in _t:
                _ed_features.append(_t)
        if _ed_features:
            features = _ed_features

    # 通用废卡拦截：结构性校验，不依赖任何 App 特定文本
    if len(features) < 3 or len(elements) < 2:
        print(f"FAIL: 页面特征不足 (features={len(features)}, elements={len(elements)})，拒绝存入废卡")
        sys.exit(0)
    # search 页不单独建卡（和 result 页区分不可靠，通用）
    if page_type == "search":
        print(f"   ℹ [建卡] search 页不单独建卡")
        sys.exit(0)

    # 记录来源页（退避路径）
    if from_page:
        try:
            _pt = hf.load_page_types()
            if pkg not in _pt: _pt[pkg] = {}
            if page_type not in _pt[pkg]: _pt[pkg][page_type] = {}
            _pt[pkg][page_type]['parent'] = from_page
            import json as _j
            _j.dump(_pt, open(hf.PAGE_TYPES_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
            print(f'   🔗 记录退避路径: {page_type} -> {from_page}')
        except Exception as _e:
            print(f'   ⚠️ 记录 parent 失败: {_e}')
    # 算 element_meta：每个候选 y → 区域 + 邻居（按 y 排序前后 2 个）
    _region_of = lambda y: "top_bar" if y < 0.12 else ("tab_bar" if y < 0.2 else ("bottom_nav" if y > 0.88 else "content"))
    _sorted = sorted(candidates, key=lambda c: c["y"])
    _meta = {}
    for _i, _c in enumerate(_sorted):
        _t = _c["text"]
        if _t in _meta:
            continue
        _nb = []
        for _j in (_i-2, _i-1, _i+1, _i+2):
            if 0 <= _j < len(_sorted) and _sorted[_j]["text"] != _t:
                _nb.append(_sorted[_j]["text"])
        _meta[_t] = {"region": _region_of(_c["y"]), "neighbors": _nb[:4]}
    print(f"   🧱 [建卡] element_meta: {len(_meta)} 个元素，区域分布: " + str({r: sum(1 for v in _meta.values() if v['region']==r) for r in ('top_bar','tab_bar','content','bottom_nav')}))

    # 任何页面卡的 features 都排除 home 的 A+B 级（避免主页特征被当成其他页特征）
    if page_type != "home":
        _home_cfg = hf.load_anchors().get(pkg, {})
        _home_exclude = set(_home_cfg.get("anchors", {}).get("A", []) + _home_cfg.get("anchors", {}).get("B", []))
        if _home_exclude:
            _before = len(features)
            features = [f for f in features if f not in _home_exclude]
            print(f"    [建卡] {page_type} features 排除 home A+B 级: {_before}  {len(features)}")

    # result/detail 卡 A 级：取同一 y 上 3 个、x 间距 < 0.3 的 Tab 栏（通用结构性判断）
    if page_type in ("result", "detail"):
        _by_y = {}
        for _c in candidates:
            _yk = round(_c["y"] * 20) / 20
            _by_y.setdefault(_yk, []).append(_c)
        for _yk, _grp in sorted(_by_y.items()):
            if len(_grp) < 3:
                continue
            _xs = sorted(g["x"] for g in _grp)
            _gaps = [_xs[i+1] - _xs[i] for i in range(len(_xs)-1)]
            if _gaps and all(g < 0.3 for g in _gaps):
                features = [g["text"] for g in _grp][:5]
                print(f"    [建卡] {page_type} A 级取 Tab 栏: {features}")
                break

    # 生成 layout：先按 y 粗聚类（0.03），再识别 Tab 栏（同 y 且 >=3 元素）
    _sorted_cands = sorted(candidates, key=lambda c: c["y"])
    _coarse_bands = []
    _cur_band = []
    for _c in _sorted_cands:
        if not _cur_band or _c["y"] - _cur_band[-1]["y"] < 0.03:
            _cur_band.append(_c)
        else:
            _coarse_bands.append(_cur_band)
            _cur_band = [_c]
    if _cur_band:
        _coarse_bands.append(_cur_band)

    _layout_regions = []
    for _band in _coarse_bands:
        _y_vals = [c["y"] for c in _band]
        _y0, _y1 = min(_y_vals), max(_y_vals)
        # 检查是不是 Tab 栏：同 y（差 < 0.01）且元素数 >= 3
        _is_same_y = (len(_band) >= 3 and _y1 - _y0 < 0.01)
        if _is_same_y and _y1 < 0.20:
            _rname = "tab_bar"
        elif _is_same_y and _y0 > 0.85:
            _rname = "bottom_nav"
        elif _y1 < 0.20:
            _rname = "top_bar"
        else:
            _rname = "content"
        # 重名加后缀
        _existing = [r["name"] for r in _layout_regions]
        if _rname in _existing:
            _rname = f"{_rname}_{len(_existing)}"
        _elems = []
        for _c in _band:
            _node = next((n for n in nodes if (n["text"] or n["desc"]) == _c["text"]), None)
            _desc = (_node.get("desc", "") if _node else "")[:60]
            if not _desc and _node:
                _rid = _node.get("resource_id", "") or ""
                _desc = _rid.split("/")[-1] if "/" in _rid else _rid
            _elems.append({
                "text": _c["text"],
                "x": _c["x"],
                "y": _c["y"],
                "desc": _desc[:60],
            })
        _elems.sort(key=lambda e: e["x"])
        _layout_regions.append({"name": _rname, "y_range": [max(0.0, _y0 - 0.02), min(1.0, _y1 + 0.02)], "elements": _elems})
    _layout = {"screen": {"w": W, "h": H}, "regions": _layout_regions}
    print(f"   🗺️ [建卡] layout: {len(_layout_regions)} 区, {sum(len(r['elements']) for r in _layout_regions)} 元素")

    hf.add_manual_sample(pkg, page_type, features, {"elements": elements, "edges": edges, "element_meta": _meta, "layout": _layout}, a_full=(page_type in ("result", "detail")))

    # home 页建卡时，存 profile 到 app_anchors[pkg]["profile"]
    if page_type == "home":
        profile = ai_data.get("profile")
        if profile and isinstance(profile, dict):
            try:
                _a = hf.load_anchors()
                if pkg in _a:
                    _a[pkg]["profile"] = profile
                    import json as _j
                    _j.dump(_a, open(hf.ANCHORS_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
                    print(f"   🧬 记录 App profile: {profile.get('app_kind')} / {profile.get('content_kind')}")
            except Exception as _e:
                print(f"   ⚠️ 记录 profile 失败: {_e}")
    print(f"   💾 自动沉淀页面: {pkg} -> {page_type}")
    
except Exception as e:
    print("ERROR:", e)
