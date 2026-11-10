#!/usr/bin/env python3
import sys, os, time, json, subprocess
sys.path.insert(0, os.path.expanduser("~/termux-mcp"))
import home_fingerprint as hf

if len(sys.argv) < 3: sys.exit(1)
pkg = sys.argv[1]
page_type = sys.argv[2]

try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("dispatch", "/data/data/com.termux/files/home/termux-mcp/dispatch.py")
    dispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch)
    mcp = dispatch.MCP()
    mcp.initialize()
    
    # 🚨 绝对防御：刚唤醒建卡脚本，立刻查是不是锁屏了
    if not hf.is_screen_on(mcp):
        print("FAIL: 建卡前检测到锁屏，立即退出！")
        sys.exit(0)
    
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
    for n in nodes:
        txt = n["text"] or n["desc"]
        if txt and len(txt) > 1:
            candidates.append({"text": txt, "x": round(n["cx"] / W, 3), "y": round(n["cy"] / H, 3)})
            
    prompt_templates = {
        "home": "当前界面是首页。请提取底部Tab或顶部搜索框等固定元素。务必提取至少3~6个固定文字特征。",
        "search": "当前界面是搜索页。请提取搜索输入框、清空按钮。**特别注意：如果你发现可以跳到结果页的按钮（比如输入框的回车/搜索按钮），请输出 edges 数组，格式为 [{\"target\": \"result\", \"action\": \"输入并回车\"}]**。",
        "result": "当前界面是搜索结果页。请提取顶部的分类Tab，过滤掉具体的视频标题和播放量。"
    }
    
    prompt = f"""你是一个安卓 UI 分析专家。{prompt_templates.get(page_type, prompt_templates['home'])}
请严格输出 JSON 格式：{{"features": ["文字1", "文字2"], "elements": {{"功能名": [x, y]}}, "edges": [{{"target": "页面类型", "action": "动作名"}}]}}
候选数据：\n{json.dumps(candidates, ensure_ascii=False)}
"""
    res_text, _ = dispatch.ask_deepseek(prompt)
    res_text = res_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    ai_data = json.loads(res_text)
    
    features = ai_data.get("features", [])
    elements = ai_data.get("elements", {})
    edges = ai_data.get("edges", [])
    
    # 🚨 核心修复：针对不同页面类型，做严格的废卡拦截
    if page_type == "result":
        # 结果页必须有列表特征，否则大概率是加载中，或者是误入的播放页
        # 通用校验：结果页必须包含一些固定词汇，比如 综合/视频/筛选/全部，或者列表里至少有 5 个以上的节点
        result_keywords = ["综合", "视频", "全部", "筛选", "排序", "番剧", "用户", "直播"]
        has_result_keyword = any(kw in " ".join(features) for kw in result_keywords)
        
        # 如果既没有固定标签词，AI 提取的特征又极少，直接拒绝
        if not has_result_keyword and len(features) < 3:
            print(f"FAIL: 当前页面未呈现出完整的结果列表特征，拒绝存入废卡 (特征数: {len(features)})")
            sys.exit(0)
            
    elif page_type == "player":
        # 播放页必须有播放器特征
        player_keywords = ["评论", "弹幕", "简介", "投币", "收藏", "播放"]
        has_player_keyword = any(kw in " ".join(features) for kw in player_keywords)
        if not has_player_keyword and len(features) < 3:
            print(f"FAIL: 当前页面未呈现出播放页特征，拒绝存入废卡 (特征数: {len(features)})")
            sys.exit(0)

    hf.add_manual_sample(pkg, page_type, features, {"elements": elements, "edges": edges})
    print(f"   💾 自动沉淀页面: {pkg} -> {page_type}")
    
except Exception as e:
    print("ERROR:", e)
