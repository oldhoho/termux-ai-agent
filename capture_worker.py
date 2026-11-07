#!/usr/bin/env python3
import sys, os, time, json, subprocess
sys.path.insert(0, os.path.expanduser("~/termux-mcp"))
import home_fingerprint as hf

def main_worker():

    if len(sys.argv) < 3: sys.exit(1)
    pkg = sys.argv[1]
    page_type = sys.argv[2]

    time.sleep(5) # 等待切 App

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("dispatch", "/data/data/com.termux/files/home/termux-mcp/dispatch.py")
        dispatch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dispatch)
        mcp = dispatch.MCP()
        mcp.initialize()
    
        actual_pkg = pkg
        created_new = False
        if pkg == "AUTO":
            actual_pkg = hf.get_current_pkg(mcp)
            created_new = True if actual_pkg else False
        
        if not actual_pkg: 
            print("FAIL: 无法获取当前包名")
            sys.exit(2)

        if created_new:
            print(f"   ❄️ 发现新应用 {actual_pkg}，强制冷启动归位...")
            mcp.run_shell(f"~/rish -c 'am force-stop {actual_pkg}'")
            time.sleep(1)
            mcp.run_shell(f"~/rish -c 'monkey -p {actual_pkg} -c android.intent.category.LAUNCHER 1'")
            time.sleep(5) # 等开屏广告
    
        # 1. 先提取全屏所有带文字的元素
        xml = hf.dump_ui_xml(mcp)
        if not xml:
            print("FAIL: 无法获取UI树")
            sys.exit(2)
    
        W, H = hf.get_screen_size(mcp)
        nodes = hf.parse_nodes(xml)
    
        candidates = []
        for n in nodes:
            txt = n["text"] or n["desc"]
            if txt and len(txt) > 1:
                candidates.append({
                    "text": txt,
                    "x": round(n["cx"] / W, 3),
                    "y": round(n["cy"] / H, 3)
                })
            
        # 2. 拿给 AI 筛选
        print(f"   🧠 抓取到 {len(candidates)} 个候选节点，页面类型: {page_type}，请求 AI 筛选...")
    
        if page_type == "home":
            prompt = f"""你是一个安卓 UI 分析专家。当前界面是 App 的首页。
    我提取了屏幕上所有的文本和坐标（x和y是 0~1 的比例）。
    请过滤掉动态内容，只保留能代表这个 App “固定框架”的元素（如底部Tab、顶部搜索框）。
    请严格输出 JSON 格式：{{"features": ["首页", "我的", "推荐"], "elements": {{"搜索框": [0.5, 0.08], "首页Tab": [0.1, 0.95]}}}}
    注意：features 请给至少 4~6 个固定文字，我会把它们分成三级。
    候选数据：\n{json.dumps(candidates, ensure_ascii=False)}
    """
        else:
            prompt = f"""你是一个安卓 UI 分析专家。当前界面是 App 的 {page_type} 页。
    我提取了屏幕上所有的文本和坐标。请提取出可以作为该页面特征的固定元素（比如输入框、分类Tab、结果卡片特征）。
    请严格输出 JSON 格式：{{"features": ["搜索", "综合"], "elements": {{"输入框": [0.5, 0.1], "综合Tab": [0.2, 0.2]}}}}
    注意：features 请给至少 3~5 个稳定文字，我会把它们分成三级。
    候选数据：\n{json.dumps(candidates, ensure_ascii=False)}
    """
        res_text, _ = dispatch.ask_deepseek(prompt) # 🌟 接收返回值并丢弃 token 统计
        res_text = res_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        ai_data = json.loads(res_text)
    
        features = ai_data.get("features", [])
        elements = ai_data.get("elements", {})
    
        # 3. 调用保存逻辑
        hf.add_manual_sample(actual_pkg, page_type, features, {"elements": elements})
    
        if created_new:
            anchors = hf.load_anchors()
            app_name = actual_pkg.split(".")[-1].capitalize()
            anchors[actual_pkg]["display_name"] = app_name
            hf._save(hf.ANCHORS_FILE, anchors)

        # 4. 打印写入校验结果，看看到底有没有写进去
        pt_data = hf.load_page_types()
        if actual_pkg in pt_data and page_type in pt_data[actual_pkg]:
            print(f"   💾 成功写入 {page_type} 页面配置！")
        else:
            print(f"   ⚠️ 写入 {page_type} 页面失败，数据结构异常！")
        
        print("OK")
    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        subprocess.run(['/data/data/com.termux/files/home/rish', '-c', 'settings put system screen_brightness 150'])

try:
    main_worker()
except Exception as e:
    print(f'❌ 建卡脚本崩溃: {e}')