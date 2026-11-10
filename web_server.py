#!/usr/bin/env python3
import os, sys, json, time, threading, logging, importlib.util, subprocess, traceback
from flask import Flask, request, jsonify, render_template_string, make_response
import home_fingerprint as hf

app = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

TOKEN_FILE = os.path.expanduser("~/.config/termux-mcp/token_usage.json")
MACROS_FILE = os.path.expanduser("~/.config/termux-mcp/macros.json")
ANCHORS_FILE = os.path.expanduser("~/.config/termux-mcp/app_anchors.json")

def _empty_tokens(): return {"prompt": 0, "completion": 0, "total": 0, "cache_hit": 0, "cache_miss": 0}
def _load_total_tokens():
    try:
        with open(TOKEN_FILE) as f: d = json.load(f)
        t = _empty_tokens()
        for k in t: t[k] = int(d.get(k, 0))
        return t
    except Exception: return _empty_tokens()
def _save_total_tokens(t):
    try:
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        tmp = TOKEN_FILE + ".tmp"
        with open(tmp, "w") as f: json.dump(t, f)
        os.replace(tmp, TOKEN_FILE)
    except Exception: pass

GLOBAL_STATE = {"logs": [], "is_running": False, "need_human": None, "session_tokens": _empty_tokens(), "total_tokens": _load_total_tokens()}
TASK_LOCK = threading.Lock()

def add_log(msg, level="info"):
    GLOBAL_STATE["logs"].append({"time": time.strftime("%H:%M:%S"), "msg": msg, "level": level})

def add_token_usage(usage):
    sess = GLOBAL_STATE["session_tokens"]; tot = GLOBAL_STATE["total_tokens"]
    for k in ("prompt", "completion", "total", "cache_hit", "cache_miss"):
        v = int(usage.get(k, 0)); sess[k] = sess.get(k, 0) + v; tot[k] = tot.get(k, 0) + v
    _save_total_tokens(tot)
    add_log(f"🧮 Token 本轮: ↑{usage.get('prompt',0)} (命中{usage.get('cache_hit',0)}/未命中{usage.get('cache_miss',0)}) ↓{usage.get('completion',0)} = {usage.get('total',0)}", "info")

HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI 控制台</title>
  <style>
    body { font-family: -apple-system, sans-serif; background: #121212; color: #fff; padding: 16px; margin: 0; }
    h2 { text-align: center; color: #00bcd4; }
    .card { background: #1e1e1e; padding: 16px; border-radius: 12px; margin-bottom: 14px; }
    input[type="text"] { width: 100%; padding: 12px; border-radius: 8px; border: none; margin-bottom: 10px; font-size: 15px; background: #333; color: #fff; }
    .btn { background: #2196F3; color: #fff; border: none; padding: 12px; border-radius: 8px; width: 100%; cursor: pointer; font-size: 15px; font-weight: bold; }
    .btn:disabled { background: #555; }
    .status-bar { font-size: 15px; font-weight: bold; text-align: center; margin: 10px 0; padding: 12px; background: #333; border-radius: 8px; border-left: 5px solid #2196F3; }
    .token-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .token-box { background: #263238; border-radius: 10px; padding: 12px; }
    .token-box h4 { margin: 0 0 8px 0; font-size: 13px; color: #90a4ae; }
    .token-row { display: flex; justify-content: space-between; font-size: 13px; padding: 2px 0; font-family: monospace; }
    .token-row .k { color: #b0bec5; } .token-row .v { color: #fff; font-weight: bold; }
    .token-total { border-top: 1px solid #37474f; margin-top: 6px; padding-top: 6px; }
    .up { color: #4fc3f7; } .down { color: #ffb74d; } .sum { color: #81c784; }
    .log-box { background: #000; padding: 10px; border-radius: 8px; height: 40vh; overflow-y: auto; font-family: monospace; font-size: 12px; border: 1px solid #333; }
    .log-info { color: #00bcd4; } .log-success { color: #4caf50; } .log-error { color: #f44336; } .log-warn { color: #ff9800; } .log-ai { color: #9c27b0; }
    .copy-bar { margin-top: 10px; }
    .toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); background: #323232; color: #fff; padding: 10px 18px; border-radius: 20px; font-size: 13px; opacity: 0; transition: opacity 0.25s; pointer-events: none; z-index: 99; }
    .toast.show { opacity: 1; }
    .warning-border { position: fixed; top: 0; left: 0; right: 0; bottom: 0; pointer-events: none; border: 4px solid #f44336; z-index: 9999; display: none; animation: pulse 1.5s infinite; }
    @keyframes pulse { 0% { border-color: rgba(244, 67, 54, 0.3); } 50% { border-color: rgba(244, 67, 54, 1); } 100% { border-color: rgba(244, 67, 54, 0.3); } }
  </style>
</head>
<body>
  <div id="warning-overlay" class="warning-border"></div>
  <h2>⚡ AI 控制台</h2>
  <div style="text-align:center;margin-bottom:10px"><a href="/anchors" style="color:#4fc3f7;text-decoration:none;">⚙️ 系统配置与资产</a></div>
  <div class="card">
    <div id="status-bar" class="status-bar">准备就绪</div>
    <input type="text" id="instruction" placeholder="输入指令，如：去B站搜Python">
    <button class="btn" id="run-btn" onclick="runTask()">启动智能体</button>
  </div>
  <div class="card">
    <div class="token-grid">
      <div class="token-box">
        <h4>本轮消耗</h4>
        <div class="token-row"><span class="k">上行 ↑</span><span class="v up" id="tk-sess-prompt">0</span></div>
        <div class="token-row"><span class="k">· 命中</span><span class="v" style="color:#66bb6a;" id="tk-sess-hit">0</span></div>
        <div class="token-row"><span class="k">· 未命中</span><span class="v" style="color:#ef5350;" id="tk-sess-miss">0</span></div>
        <div class="token-row"><span class="k">下行 ↓</span><span class="v down" id="tk-sess-completion">0</span></div>
        <div class="token-row token-total"><span class="k">合计</span><span class="v sum" id="tk-sess-total">0</span></div>
      </div>
      <div class="token-box">
        <h4>累计消耗</h4>
        <div class="token-row"><span class="k">上行 ↑</span><span class="v up" id="tk-all-prompt">0</span></div>
        <div class="token-row"><span class="k">· 命中</span><span class="v" style="color:#66bb6a;" id="tk-all-hit">0</span></div>
        <div class="token-row"><span class="k">· 未命中</span><span class="v" style="color:#ef5350;" id="tk-all-miss">0</span></div>
        <div class="token-row"><span class="k">下行 ↓</span><span class="v down" id="tk-all-completion">0</span></div>
        <div class="token-row token-total"><span class="k">合计</span><span class="v sum" id="tk-all-total">0</span></div>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="log-box" id="log-panel"></div>
    <div class="copy-bar"><button class="btn btn-secondary" style="padding:10px;font-size:14px;" onclick="copySessionLogs()">📋 复制本轮日志</button></div>
  </div>
  <div class="toast" id="toast"></div>
  <script>
    var last = 0;
    function setWarning(on) { document.getElementById('warning-overlay').style.display = on ? 'block' : 'none'; }
    function updateTokens(tok) {
      if (!tok) return;
      var s = tok.session || {}, a = tok.total || {};
      document.getElementById('tk-sess-prompt').textContent = s.prompt;
      document.getElementById('tk-sess-hit').textContent = s.cache_hit;
      document.getElementById('tk-sess-miss').textContent = s.cache_miss;
      document.getElementById('tk-sess-completion').textContent = s.completion;
      document.getElementById('tk-sess-total').textContent = s.total;
      document.getElementById('tk-all-prompt').textContent = a.prompt;
      document.getElementById('tk-all-hit').textContent = a.cache_hit;
      document.getElementById('tk-all-miss').textContent = a.cache_miss;
      document.getElementById('tk-all-completion').textContent = a.completion;
      document.getElementById('tk-all-total').textContent = a.total;
    }
    function pollLogs() {
      fetch('/api/get_logs?start=' + last).then(r => r.json()).then(function(d) {
        if (d.tokens) updateTokens(d.tokens);
        if (d.need_human && d.need_human.indexOf("正在收集") >= 0) setWarning(true);
        else if (d.need_human === null || d.need_human === "") setWarning(false);
        if (d.need_human && d.need_human.indexOf("正在收集") < 0) {
            if(confirm("🙋 AI 卡住了: " + d.need_human)) fetch('/api/resume', {method:'POST'}).then(function() { location.reload(); });
        }
        if (d.logs && d.logs.length > 0) {
          var p = document.getElementById('log-panel');
          d.logs.forEach(function(x) {
            var div = document.createElement('div');
            div.className = 'log-' + x.level;
            div.textContent = '[' + x.time + '] ' + x.msg;
            p.appendChild(div);
          });
          last = d.next_index; p.scrollTop = p.scrollHeight;
          var lastMsg = d.logs[d.logs.length-1].msg;
          var bar = document.getElementById('status-bar');
          bar.innerText = lastMsg; 
          if(lastMsg.indexOf("完成") >= 0) bar.style.borderLeftColor = "#4caf50";
          else if(lastMsg.indexOf("失败") >= 0) bar.style.borderLeftColor = "#f44336";
          else bar.style.borderLeftColor = "#2196F3";
        }
      }).finally(function() { setTimeout(pollLogs, 1000); });
    }
    pollLogs();

    function runTask() {
      var v = document.getElementById('instruction').value.trim();
      if (!v) return alert("请先输入指令");
      var btn = document.getElementById('run-btn');
      btn.disabled = true; btn.textContent = '任务执行中...';
      fetch('/api/run', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({instruction: v}) })
      .then(function(r) { return r.json(); }).then(function(d) {
        if (d.error) { alert(d.error); btn.disabled = false; btn.textContent = '启动智能体'; }
        else { setTimeout(function() { btn.disabled = false; btn.textContent = '启动智能体'; }, 3000); }
      }).catch(function(e) { alert('请求失败: ' + e); btn.disabled = false; btn.textContent = '启动智能体'; });
    }

    function copySessionLogs() {
      var panel = document.getElementById('log-panel'); 
      var text = panel.innerText || panel.textContent || '';
      if (!text.trim()) return alert("暂无日志");
      if (navigator.clipboard && window.isSecureContext) { 
          navigator.clipboard.writeText(text).then(function(){ alert("✅ 复制成功"); }); 
      } else { 
          var ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); alert("✅ 复制成功");
      }
    }
  </script>
</body>
</html>
"""

@app.route('/')
def index(): return make_response(render_template_string(HTML))

@app.route('/api/get_logs')
def get_logs():
    start = int(request.args.get('start', 0))
    return jsonify({
        "logs": GLOBAL_STATE["logs"][start:],
        "next_index": len(GLOBAL_STATE["logs"]),
        "need_human": GLOBAL_STATE.get("need_human"),
        "tokens": {"session": GLOBAL_STATE["session_tokens"], "total": GLOBAL_STATE["total_tokens"]},
    })

@app.route('/api/resume', methods=['POST'])
def api_resume():
    GLOBAL_STATE["need_human"] = None
    return jsonify({"ok": True})

def execute_agent(instruction):
    add_log(f"🧠 开始执行: {instruction}", "ai")
    spec = importlib.util.spec_from_file_location("dispatch", "/data/data/com.termux/files/home/termux-mcp/dispatch.py")
    dispatch = importlib.util.module_from_spec(spec)
    class LoggerWriter:
        def write(self, message):
            if not message.strip(): return
            line = message.rstrip("\n")
            if line.startswith("__TOKEN_USAGE__"):
                try: add_token_usage(json.loads(line[len("__TOKEN_USAGE__"):]))
                except: pass
                return
            if line.startswith("__NEED_HUMAN__"):
                add_log(line.replace("__NEED_HUMAN__", "🙋 "), "warn")
                GLOBAL_STATE["need_human"] = line.replace("__NEED_HUMAN__", "")
                return
            lvl = "info"
            if "✅" in line or "🏁" in line: lvl = "success"
            elif "❌" in line: lvl = "error"
            elif "🧠" in line or "计划" in line: lvl = "ai"
            add_log(line.strip(), lvl)
        def flush(self): pass
    old_stdout = sys.stdout; sys.stdout = LoggerWriter()
    try:
        spec.loader.exec_module(dispatch); sys.argv = ["dispatch.py", instruction]; dispatch.main()
        add_log("🏁 任务执行完毕", "success")
    except Exception as e: add_log(f"❌ 执行异常: {e}", "error")
    finally: sys.stdout = old_stdout

@app.route('/api/run', methods=['POST'])
def run_task():
    if GLOBAL_STATE["is_running"]: return jsonify({"error": "任务执行中"}), 409
    instruction = (request.json or {}).get('instruction', '').strip()
    if not instruction: return jsonify({"error": "指令不能为空"}), 400
    GLOBAL_STATE["logs"] = []; GLOBAL_STATE["session_tokens"] = _empty_tokens(); GLOBAL_STATE["is_running"] = True; GLOBAL_STATE["need_human"] = None
    def _wrap():
        try:
            with TASK_LOCK: execute_agent(instruction)
        except Exception as e: add_log(f"未知异常: {e}", "error")
        finally: GLOBAL_STATE["is_running"] = False
    threading.Thread(target=_wrap, daemon=True).start()
    return jsonify({"msg": "started"})

# ==================== 锚点与资产管理页面 ====================
@app.route('/anchors')
def anchors_page():
    try:
        data = hf.anchors_snapshot()
        # 🌟 核心修复：遍历清洗所有旧数据，如果 anchors 是 list，强转成 A/B/C 字典
        for pkg, cfg in data.items():
            pts = cfg.get("page_types", {})
            for pt, p_data in pts.items():
                if isinstance(p_data, dict):
                    anchors = p_data.get("anchors")
                    if not isinstance(anchors, dict):
                        # 如果是旧列表，转为新字典
                        old_list = anchors if isinstance(anchors, list) else []
                        p_data["anchors"] = {"A": old_list[:2], "B": old_list[2:4], "C": old_list[4:]}
                    # 确保元素图纸存在
                    if "elements" not in p_data:
                        p_data["elements"] = {}
    except Exception as e:
        return "<h3>数据读取失败</h3><pre>" + traceback.format_exc() + "</pre>", 500

    try: macros = json.load(open(MACROS_FILE, encoding='utf-8'))
    except: macros = {}
    
    macros_html = ""
    for m_name, m_data in macros.items():
        steps_cnt = len(m_data.get("atoms", []))
        macros_html += '<div style="margin-top:8px;background:#2a2a2a;padding:8px;border-radius:6px;">'
        macros_html += '<b>' + m_name + '</b> <span class="muted">(' + m_data.get("pkg","") + ')</span>'
        macros_html += '<button class="btn danger" style="float:right;padding:2px 6px;" onclick="delMacro(\'' + m_name + '\')">× 删除</button>'
        macros_html += '<div class="muted" style="margin-top:4px;">包含 ' + str(steps_cnt) + ' 个原子动作</div></div>'
    if not macros_html: macros_html = '<div class="muted">暂无宏</div>'

    cards_html = ""
    if not data:
        cards_html = '<div class="card">暂无 App 配置</div>'
    else:
        for pkg, d in data.items():
            name = d.get("display_name", pkg)
            cards_html += '<div class="card">'
            cards_html += '<div style="display:flex; justify-content:space-between; align-items:center;">'
            cards_html += '<h3 style="margin:0;">' + name + ' <span class="muted">(' + pkg + ')</span></h3>'
            cards_html += '<button class="btn" style="padding:4px 8px;font-size:12px;" onclick="renameApp(\'' + pkg + '\', \'' + name + '\')">改名</button>'
            cards_html += '</div>'
            status = '<span class="mature">已固化 (AI提取)</span>' if d.get("mature") else '<span class="pending">校准中</span>'
            cards_html += '<div class="muted" style="margin-top:6px;">校准轮次: ' + str(d.get("rounds",0)) + ' | 状态: ' + status + '</div>'
            
            for lv in ['A', 'B', 'C']:
                cards_html += '<div style="margin-top:4px"><b>' + lv + ' 级:</b> '
                arr = d.get("anchors", {}).get(lv, [])
                if not isinstance(arr, list): arr = []
                for a in arr:
                    cards_html += '<span class="tag ' + lv + '">' + a
                    if lv != 'A':
                        cards_html += '<span class="x" style="color:#4fc3f7;margin-left:6px;" title="升级" onclick="promoteAnchor(\'' + pkg + '\',\'' + lv + '\',\'' + a + '\')">↑</span>'
                    if lv != 'C':
                        cards_html += '<span class="x" style="color:#ffb74d;margin-left:6px;" title="降级" onclick="demoteAnchor(\'' + pkg + '\',\'' + lv + '\',\'' + a + '\')">↓</span>'
                    cards_html += '<span class="x" style="margin-left:6px;" title="删除" onclick="delAnchor(\'' + pkg + '\',\'' + lv + '\',\'' + a + '\')">×</span></span>'
                cards_html += '</div>'
                
            elements = d.get("elements", {})
            if elements and isinstance(elements, dict):
                cards_html += '<div style="background:#000;padding:10px;border-radius:8px;font-family:monospace;font-size:12px;margin-top:10px;border:1px solid #444;color:#81c784;">元素图纸：<br>'
                for el_name, coords in elements.items():
                    cards_html += el_name + ': ' + str(coords) + '<br>'
                cards_html += '</div>'

            # 渲染自定义页面
            pts = d.get("page_types", {})
            if isinstance(pts, dict):
                for pt, p in pts.items():
                    if not isinstance(p, dict): continue
                    pt_elements = p.get("elements", {})
                    pt_anchors = p.get("anchors", {"A": [], "B": [], "C": []})
                    
                    cards_html += '<div style="margin-top:15px; padding-top:10px; border-top:1px dashed #444;">'
                    cards_html += '<div style="display:flex; justify-content:space-between; margin-bottom:6px;">'
                    display_names = {"search": "搜索页", "result": "结果页", "profile": "个人主页"}
                    pt_name = display_names.get(pt, pt)
                    cards_html += '<b>页面 [' + pt_name + ']:</b>'
                    cards_html += '<button class="btn danger" style="padding:2px 8px;font-size:12px;" onclick="delPage(\'' + pkg + '\', \'' + pt + '\')">× 删除该页</button>'
                    cards_html += '</div>'
                    
                    for lv in ['A', 'B', 'C']:
                        cards_html += '<div style="margin-top:4px"><b style="font-size:12px;color:#90a4ae;">' + lv + ' 级:</b> '
                        arr = pt_anchors.get(lv, [])
                        if not isinstance(arr, list): arr = []
                        for a in arr:
                            cards_html += '<span class="tag ' + lv + '">' + a
                            if lv != 'A':
                                cards_html += '<span class="x" style="color:#4fc3f7;margin-left:6px;" title="升级" onclick="promoteAnchor(\'' + pkg + '\',\'' + lv + '\',\'' + a + '\',\'' + pt + '\')">↑</span>'
                            if lv != 'C':
                                cards_html += '<span class="x" style="color:#ffb74d;margin-left:6px;" title="降级" onclick="demoteAnchor(\'' + pkg + '\',\'' + lv + '\',\'' + a + '\',\'' + pt + '\')">↓</span>'
                            cards_html += '<span class="x" style="margin-left:6px;" title="删除" onclick="delAnchor(\'' + pkg + '\',\'' + lv + '\',\'' + a + '\',\'' + pt + '\')">×</span></span>'
                        cards_html += '</div>'

                    if pt_elements and isinstance(pt_elements, dict):
                        cards_html += '<div style="background:#000;padding:10px;border-radius:8px;font-family:monospace;font-size:12px;margin-top:10px;border:1px solid #444;color:#81c784;">元素图纸：<br>'
                        for el_name, coords in pt_elements.items():
                            cards_html += el_name + ': ' + str(coords) + '<br>'
                        cards_html += '</div>'
                    cards_html += '</div>'

            cards_html += '<div class="row" style="margin-top:10px;">'
            cards_html += '<input id="in_' + pkg + '" placeholder="添加新锚点文字">'
            cards_html += '<select id="lv_' + pkg + '" style="background:#333;color:#fff;border:none;border-radius:6px;padding:8px"><option value="A">A级</option><option value="B">B级</option><option value="C">C级</option></select>'
            cards_html += '<button class="btn" onclick="addAnchor(\'' + pkg + '\')">添加</button>'
            cards_html += '</div>'
            
            cards_html += '<div class="row" style="margin-top:10px;border-top:1px solid #333;padding-top:10px;">'
            cards_html += '<button class="btn" style="flex:1" onclick="appendPage(\'' + pkg + '\')">➕ 追加新页面</button>'
            cards_html += '<button class="btn warn" style="flex:1" onclick="resetApp(\'' + pkg + '\')">重置校准</button>'
            cards_html += '<button class="btn danger" onclick="delApp(\'' + pkg + '\')">删除</button>'
            cards_html += '</div></div>'

    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>系统配置与资产</title>
<style>
body{font-family:-apple-system,sans-serif;background:#121212;color:#fff;padding:16px;margin:0}
h2{text-align:center;color:#00bcd4}
.card{background:#1e1e1e;padding:14px;border-radius:12px;margin-bottom:14px;border:1px solid #333}
.card h3{margin:0 0 8px;font-size:16px;color:#4fc3f7}
.tag{display:inline-block;padding:4px 8px;margin:3px;border-radius:6px;font-size:12px;font-family:monospace}
.tag.A{background:#1b5e20;color:#c8e6c9} .tag.B{background:#e65100;color:#ffe0b2} .tag.C{background:#37474f;color:#cfd8dc} .tag.cand{background:#5d4037;color:#ffccbc}
.x{cursor:pointer;color:#f44336;font-weight:bold;margin-left:4px}
.muted{color:#90a4ae;font-size:12px}
.row{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
input{background:#333;border:none;color:#fff;padding:8px;border-radius:6px;font-size:13px;flex:1;min-width:80px}
.btn{background:#2196F3;color:#fff;border:none;padding:8px 12px;border-radius:6px;cursor:pointer;font-size:12px}
.btn.warn{background:#e65100} .btn.danger{background:#c62828} .btn.success{background:#2e7d32}
.mature{color:#4caf50} .pending{color:#ff9800}
</style></head><body>
<a href="/" style="color:#4fc3f7;font-size:13px;text-decoration:none;">&lt; 返回控制台</a>
<h2>⚙️ 系统配置与资产</h2>

<div class="card" style="border: 1px solid #ff9800;">
  <h3>💾 资产备份与恢复</h3>
  <div class="row">
    <button class="btn success" style="flex:1" onclick="exportData()">📤 导出配置</button>
    <button class="btn" style="flex:1" onclick="importData()">📥 导入恢复</button>
  </div>
</div>

<div class="card" style="border: 1px solid #4fc3f7; text-align:center;">
  <h3>➕ 一键添加新 App</h3>
  <button class="btn success" style="width:100%;padding:12px;" onclick="startAutoCapture()">🚀 开始抓取首页</button>
</div>

<div class="card" style="border: 1px solid #9c27b0;">
  <h3>📦 已沉淀的宏</h3>
  __MACROS_HTML__
</div>

<h3 style="color:#00bcd4;text-align:center;">已配置的 App 锚点</h3>
__CARDS_HTML__

<script>
function api(path, data) { return fetch(path, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data||{})}).then(r=>r.json()); }
function startAutoCapture() { doCapture("AUTO", "home"); }
function appendPage(pkg) {
  var pt = prompt("请输入页面标识 (如 search, profile, cart):", "search");
  if (!pt) return;
  doCapture(pkg, pt); 
}
function delPage(pkg, pt) {
  if (!confirm('确定删除页面 [' + pt + '] 的配置吗？')) return;
  api('/api/anchors/delete_page', {pkg: pkg, page_type: pt}).then(function(){ location.reload(); });
}

function doCapture(pkg, page_type) {
  var msg = "点击确定后，请立刻切到目标 App 的首页或搜索页并停留 5 秒！";
  if (page_type !== "home") msg = "点击确定后，请立刻切到该 App 的 " + page_type + " 页面并停留 5 秒！";
  if (!confirm(msg)) return;
  
  fetch('/api/anchors/capture_auto', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pkg: pkg, page_type: page_type})
  }).then(r=>r.json()).then(d=>{ if (!d.ok) alert('启动失败: ' + d.msg); });
}

function renameApp(pkg, oldName) {
  const newName = prompt("修改应用名称：", oldName);
  if (newName && newName !== oldName) api('/api/anchors/rename', {pkg: pkg, name: newName}).then(()=>location.reload());
}
function addAnchor(pkg){
  const lv=document.getElementById('lv_'+pkg).value;
  const val=document.getElementById('in_'+pkg).value.trim();
  if(!val) return;
  fetch('/api/anchors').then(r=>r.json()).then(all=>{
    const cur=(all[pkg] && all[pkg].anchors[lv]) || [];
    if(cur.indexOf(val) === -1) cur.push(val);
    api('/api/anchors/update',{pkg:pkg,level:lv,anchors:cur}).then(()=>location.reload());
  });
}
function delAnchor(pkg,lv,anchor,page_type){
    const url = '/api/anchors/delete_anchor';
    api(url,{pkg:pkg,level:lv,anchor:anchor,page_type:page_type||""}).then(()=>location.reload());
}
function promoteAnchor(pkg,lv,anchor,page_type){
    if(!confirm('确定将 [' + anchor + '] 晋级吗？')) return;
    fetch('/api/anchors').then(r=>r.json()).then(all=>{
        let cfg = page_type ? all[pkg].page_types[page_type].anchors : all[pkg].anchors;
        if(cfg[lv] && cfg[lv].includes(anchor)){
            cfg[lv] = cfg[lv].filter(x => x !== anchor);
            const nextLv = lv === 'C' ? 'B' : 'A';
            if(!cfg[nextLv]) cfg[nextLv] = [];
            if(!cfg[nextLv].includes(anchor)) cfg[nextLv].push(anchor);
            api('/api/anchors/update',{pkg:pkg, level:nextLv, anchors:cfg[nextLv], page_type:page_type||""}).then(()=>{
                api('/api/anchors/update',{pkg:pkg, level:lv, anchors:cfg[lv], page_type:page_type||""}).then(()=>location.reload());
            });
        }
    });
}
function demoteAnchor(pkg,lv,anchor,page_type){
    if(!confirm('确定将 [' + anchor + '] 降级吗？')) return;
    fetch('/api/anchors').then(r=>r.json()).then(all=>{
        let cfg = page_type ? all[pkg].page_types[page_type].anchors : all[pkg].anchors;
        if(cfg[lv] && cfg[lv].includes(anchor)){
            cfg[lv] = cfg[lv].filter(x => x !== anchor);
            const nextLv = lv === 'A' ? 'B' : 'C';
            if(!cfg[nextLv]) cfg[nextLv] = [];
            if(!cfg[nextLv].includes(anchor)) cfg[nextLv].push(anchor);
            api('/api/anchors/update',{pkg:pkg, level:nextLv, anchors:cfg[nextLv], page_type:page_type||""}).then(()=>{
                api('/api/anchors/update',{pkg:pkg, level:lv, anchors:cfg[lv], page_type:page_type||""}).then(()=>location.reload());
            });
        }
    });
}
function resetApp(pkg){ if(confirm('重置校准状态？')) api('/api/anchors/reset',{pkg:pkg}).then(()=>location.reload()); }
function delApp(pkg){ if(confirm('确定删除该 App 所有配置？')) api('/api/anchors/delete',{pkg:pkg}).then(()=>location.reload()); }
function confirmPT(pkg,pt,csv){ const arr=csv?csv.split(','):[]; api('/api/anchors/confirm_pt',{pkg:pkg,page_type:pt,anchors:arr}).then(()=>location.reload()); }
function delMacro(name){
    if(!confirm('确定删除宏 ' + name + ' 吗？')) return;
    api('/api/macros/delete', {name: name}).then(d => {
        if(d.ok) location.reload();
        else alert('删除失败: ' + (d.msg || '后端未响应'));
    });
}
function exportData() {
    fetch('/api/backup/export').then(r=>r.json()).then(d=>{
        const blob = new Blob([JSON.stringify(d, null, 2)], {type: "application/json"});
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = url; a.download = "ai_agent_backup.json"; a.click();
    });
}
function importData() {
    const input = document.createElement('input'); input.type = 'file'; input.accept = '.json';
    input.onchange = e => {
        const file = e.target.files[0]; const reader = new FileReader();
        reader.onload = event => {
            try {
                const data = JSON.parse(event.target.result);
                if(confirm('确定要导入并覆盖现有配置吗？')) api('/api/backup/import', data).then(()=>{ alert('导入成功！'); location.reload(); });
            } catch(err) { alert('文件格式错误'); }
        }; reader.readAsText(file);
    }; input.click();
}
</script></body></html>"""
    
    html = html.replace("__MACROS_HTML__", macros_html)
    html = html.replace("__CARDS_HTML__", cards_html)
    
    return make_response(html)

@app.route('/api/anchors')
def api_anchors(): return jsonify(hf.anchors_snapshot())

@app.route('/api/backup/export')
def api_export():
    d = {}
    for p, name in [(ANCHORS_FILE, "anchors"), (MACROS_FILE, "macros")]:
        try: d[name] = json.load(open(p, encoding="utf-8"))
        except: d[name] = {}
    return jsonify(d)

@app.route('/api/backup/import', methods=['POST'])
def api_import():
    d = request.json or {}
    if "anchors" in d: hf._save(ANCHORS_FILE, d["anchors"])
    if "macros" in d:
        with open(MACROS_FILE, "w", encoding="utf-8") as f:
            json.dump(d["macros"], f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True})

@app.route('/api/macros/delete', methods=['POST'])
def api_macros_delete():
    d = request.json or {}
    name = d.get("name", "")
    if name:
        try:
            with open(MACROS_FILE, encoding='utf-8') as f: m = json.load(f)
            if name in m:
                del m[name]
                with open(MACROS_FILE, 'w', encoding='utf-8') as f: json.dump(m, f, ensure_ascii=False, indent=2)
                return jsonify({"ok": True})
            return jsonify({"ok": False, "msg": "未找到该宏"})
        except Exception as e: return jsonify({"ok": False, "msg": str(e)})
    return jsonify({"ok": False, "msg": "缺少宏名字"})

@app.route('/api/anchors/capture_auto', methods=['POST'])
def api_capture_auto():
    d = request.json or {}
    pkg = d.get("pkg", "AUTO")
    page_type = d.get("page_type", "home")
    print(f"🎬 收到抓取任务: pkg={pkg}, page_type={page_type}")
    subprocess.Popen(["python3", "-c", "import subprocess; subprocess.run(['/data/data/com.termux/files/home/rish', '-c', 'settings put system screen_brightness 0'])"])
    script = "/data/data/com.termux/files/home/termux-mcp/capture_worker.py"
    log_file = open("/data/data/com.termux/files/home/termux-mcp/web.log", "a")
    subprocess.Popen(["python3", script, pkg, page_type], stdout=log_file, stderr=log_file)
    return jsonify({"ok": True})

@app.route('/api/anchors/delete_page', methods=['POST'])
def api_anchors_delete_page():
    d = request.json or {}
    pkg = d.get("pkg", "")
    page_type = d.get("page_type", "")
    if pkg and page_type:
        pt = hf.load_page_types()
        if pkg in pt and page_type in pt[pkg]:
            del pt[pkg][page_type]
            if not pt[pkg]: del pt[pkg]
            hf._save(hf.PAGE_TYPES_FILE, pt)
            return jsonify({"ok": True})
    return jsonify({"ok": False})

@app.route('/api/anchors/rename', methods=['POST'])
def api_anchors_rename():
    d = request.json or {}
    pkg = d.get("pkg", ""); name = d.get("name", "")
    if pkg and name:
        anchors = hf.load_anchors()
        if pkg in anchors:
            anchors[pkg]["display_name"] = name
            hf._save(hf.ANCHORS_FILE, anchors)
            return jsonify({"ok": True})
    return jsonify({"ok": False})

@app.route('/api/anchors/update', methods=['POST'])
def api_anchors_update():
    d = request.json or {}
    pkg = d.get("pkg",""); level = d.get("level",""); anchors = d.get("anchors",[]); page_type = d.get("page_type","")
    if page_type:
        pt = hf.load_page_types()
        if pkg in pt and page_type in pt[pkg]:
            if "anchors" not in pt[pkg][page_type]: pt[pkg][page_type]["anchors"] = {"A":[], "B":[], "C":[]}
            pt[pkg][page_type]["anchors"][level] = anchors
            hf._save(hf.PAGE_TYPES_FILE, pt)
            return jsonify({"ok": True})
        return jsonify({"ok": False})
    else:
        return jsonify({"ok": hf.update_anchor_level(pkg, level, anchors)[0]})

@app.route('/api/anchors/delete_anchor', methods=['POST'])
def api_anchors_delete_anchor():
    d = request.json or {}
    pkg = d.get("pkg",""); level = d.get("level",""); anchor = d.get("anchor",""); page_type = d.get("page_type","")
    if page_type:
        pt = hf.load_page_types()
        if pkg in pt and page_type in pt[pkg]:
            arr = pt[pkg][page_type]["anchors"].get(level, [])
            if anchor in arr:
                arr.remove(anchor)
                hf._save(hf.PAGE_TYPES_FILE, pt)
                return jsonify({"ok": True})
        return jsonify({"ok": False})
    else:
        return jsonify({"ok": hf.delete_anchor(pkg, level, anchor)[0]})

@app.route('/api/anchors/reset', methods=['POST'])
def api_anchors_reset():
    d = request.json or {}
    return jsonify({"ok": hf.reset_app_calibration(d.get("pkg",""))[0]})

@app.route('/api/anchors/delete', methods=['POST'])
def api_anchors_delete():
    d = request.json or {}
    return jsonify({"ok": hf.delete_app(d.get("pkg",""))[0]})

@app.route('/api/anchors/confirm_pt', methods=['POST'])
def api_anchors_confirm_pt():
    d = request.json or {}
    return jsonify({"ok": hf.confirm_page_type_anchor(d.get("pkg",""), d.get("page_type",""), d.get("anchors", []))[0]})

if __name__ == '__main__':
    print("🚀 Web 服务已启动: http://127.0.0.1:8080")
    app.run(host='0.0.0.0', port=8080, threaded=True)
