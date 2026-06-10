"""
理理喵 — Web 聊天 + Todo 面板
用法：python app.py
浏览器打开 http://localhost:5000
"""
import json
import re
import sys
from datetime import date
from pathlib import Path
from flask import Flask, render_template, request, jsonify
from openai import OpenAI

BASE = Path(__file__).parent
MEMORY_FILE = BASE / "lili_memory.json"
TODOS_FILE = BASE / "todos.json"
CHAT_FILE = BASE / "chat_history.json"
ENV_FILE = BASE / ".env"

app = Flask(__name__)


def strip_brackets(text):
    """移除 AI 回复中所有括号动作描写。"""
    text = re.sub(r'（[^）]*）', '', text)
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'【[^】]*】', '', text)
    text = re.sub(r'［[^］]*］', '', text)
    text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
    return text.strip()

# ── API Key ──
def get_api_key():
    import os
    # 云环境优先从环境变量读取
    env_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LILI_API_KEY")
    if env_key:
        return env_key
    if ENV_FILE.exists():
        return ENV_FILE.read_text(encoding="utf-8").strip()
    return None

# ── OpenAI 客户端 ──
_client = None

def get_client():
    global _client
    if _client is None:
        key = get_api_key()
        if not key:
            raise RuntimeError("未找到 API key，请先运行 python lili.py 输入 key")
        _client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
    return _client

# ── JSON 工具 ──
def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def safe_json_parse(text):
    """从 LLM 返回的文本中提取 JSON。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines)
    if not text.startswith("[") and not text.startswith("{"):
        for start_char, end_char in [("[", "]"), ("{", "}")]:
            si = text.find(start_char)
            ei = text.rfind(end_char)
            if si != -1 and ei != -1 and ei > si:
                text = text[si:ei + 1]
                break
    return json.loads(text)

# ── 记忆更新 ──
def update_memory(conversation_text, client=None):
    if client is None:
        try:
            client = get_client()
        except RuntimeError:
            return
    mem = load_json(MEMORY_FILE, {"config": {}, "learned": {"triggers": [], "soothers": [], "traits": [], "dislikes": []}, "chat_count": 0})
    learned = mem.get("learned", {})

    prompt = f"""从以下对话中提取关于用户的新信息。只提取新出现的、之前没记录过的。

已有记录：{json.dumps(learned, ensure_ascii=False)}

对话：{conversation_text[-1500:]}

返回 JSON，只包含 updates 字段，内容是新增信息的列表（每条一个简短描述）。
如果没有任何新信息，返回 {{"updates": []}}。不返回其他文字。"""

    try:
        r = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=300,
        )
        result = safe_json_parse(r.choices[0].message.content)
        if result.get("updates"):
            if "traits" not in learned:
                learned["traits"] = []
            for u in result["updates"]:
                if u not in learned["traits"]:
                    learned["traits"].append(u)
    except Exception as e:
        sys.stderr.write(f"  [update_memory 失败] {e}\n")

    mem["learned"] = learned
    mem["chat_count"] = mem.get("chat_count", 0) + 1
    save_json(MEMORY_FILE, mem)

# ── 待办提取 ──
def extract_todos(conversation_text, client=None):
    if client is None:
        try:
            client = get_client()
        except RuntimeError:
            return []
    existing = load_json(TODOS_FILE, {"todos": []})
    existing_str = json.dumps(existing.get("todos", []), ensure_ascii=False)
    mem = load_json(MEMORY_FILE, {})
    config = mem.get("config", {})

    prompt = f"""从对话中提取新的待办事项。只提取真的需要做的具体行动。

已有待办：{existing_str}
用户偏好：{json.dumps(config, ensure_ascii=False)}

对话：{conversation_text[-2000:]}

规则：
- 📋 任务型：需要做的具体事（投简历、刷题等）
- 🧘 舒缓型：如果用户表达了情绪困扰，且你了解ta的喜好（从对话中），可以安排一个小行动
- 纯情绪无行动点 → 不提取
- 和已有待办重复 → 跳过

返回 JSON 数组（不要其他文字）：
[{{"type":"task|soothe","content":"...","priority":"高|中|低","estimated_time":"Xh或Xmin"}}]

无新待办返回 []"""

    try:
        r = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        new_todos = safe_json_parse(r.choices[0].message.content)
        if not isinstance(new_todos, list):
            new_todos = []
        if new_todos:
            existing["todos"].extend(new_todos)
            existing["updated"] = date.today().isoformat()
            save_json(TODOS_FILE, existing)
        return new_todos
    except Exception as e:
        sys.stderr.write(f"  [extract_todos 失败] {e}\n")
        return []

# ── System Prompt 构建 ──
from prompt import LILI_SYSTEM_PROMPT, FIRST_RUN_PROMPT

def build_system_prompt():
    mem = load_json(MEMORY_FILE, {"config": {}, "learned": {"triggers": [], "soothers": [], "traits": [], "dislikes": []}, "chat_count": 0})
    config = mem.get("config", {})
    is_first_run = not config or not config.get("peak_time")

    if is_first_run:
        return FIRST_RUN_PROMPT

    name = config.get("name", "你")
    memory_context = (
        f"用户称呼：{name}\n"
        f"偏好：{json.dumps(config, ensure_ascii=False)}\n"
        f"你对ta的了解：{json.dumps(mem.get('learned', {}), ensure_ascii=False)}\n"
        f"聊天次数：{mem.get('chat_count', 0)}"
    )
    return LILI_SYSTEM_PROMPT + "\n\n---\n关于正在和你聊天的人：\n" + memory_context

def is_first_run():
    mem = load_json(MEMORY_FILE, {"config": {}, "learned": {"triggers": [], "soothers": [], "traits": [], "dislikes": []}, "chat_count": 0})
    config = mem.get("config", {})
    return not config or not config.get("peak_time")

# ── 页面 ──
@app.route("/")
def index():
    return render_template("index.html", first_run=is_first_run())

# ── Todo API ──
@app.route("/api/todos")
def api_todos():
    data = load_json(TODOS_FILE, {"todos": []})
    return jsonify({"todos": data.get("todos", []), "updated": data.get("updated", "")})

@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    body = request.get_json()
    idx = body.get("index")
    data = load_json(TODOS_FILE, {"todos": []})
    todos = data.get("todos", [])
    if 0 <= idx < len(todos):
        current = todos[idx].get("status")
        todos[idx]["status"] = "todo" if current == "done" else "done"
        data["updated"] = date.today().isoformat()
        save_json(TODOS_FILE, data)
        return jsonify({"ok": True, "status": todos[idx]["status"]})
    return jsonify({"ok": False, "error": "index out of range"}), 400

@app.route("/api/delete", methods=["POST"])
def api_delete():
    body = request.get_json()
    idx = body.get("index")
    data = load_json(TODOS_FILE, {"todos": []})
    todos = data.get("todos", [])
    if 0 <= idx < len(todos):
        removed = todos.pop(idx)
        data["updated"] = date.today().isoformat()
        save_json(TODOS_FILE, data)
        return jsonify({"ok": True, "removed": removed})
    return jsonify({"ok": False, "error": "index out of range"}), 400

@app.route("/api/add", methods=["POST"])
def api_add():
    body = request.get_json()
    content = body.get("content", "").strip()
    priority = body.get("priority", "中")
    todo_type = body.get("type", "task")
    if not content:
        return jsonify({"ok": False, "error": "content is required"}), 400
    data = load_json(TODOS_FILE, {"todos": []})
    data.get("todos", []).append({
        "type": todo_type, "content": content,
        "priority": priority, "status": "todo", "estimated_time": ""
    })
    data["updated"] = date.today().isoformat()
    save_json(TODOS_FILE, data)
    return jsonify({"ok": True})

# ── 聊天 API ──
@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.get_json()
    user_message = body.get("message", "").strip()
    if not user_message:
        return jsonify({"ok": False, "error": "empty message"}), 400

    # 优先使用前端传的 API key（多用户支持），回退到服务端 key
    user_key = body.get("api_key", "").strip()
    try:
        if user_key:
            client = OpenAI(api_key=user_key, base_url="https://api.deepseek.com")
        else:
            client = get_client()
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    # 加载或初始化聊天记录
    chat_data = load_json(CHAT_FILE, {"messages": [], "is_first_run": True})

    if not chat_data.get("messages"):
        system_content = build_system_prompt()
        chat_data["messages"] = [{"role": "system", "content": system_content}]
        if not is_first_run():
            chat_data["messages"].append({
                "role": "assistant",
                "content": "喵。今天怎么样？"
            })

    # 检查是否触发了「帮我理理」
    triggered_todo = any(kw in user_message for kw in ["帮我理理", "帮我整理", "理理喵帮我", "帮我理理喵"])

    chat_data["messages"].append({"role": "user", "content": user_message})

    try:
        r = client.chat.completions.create(
            model="deepseek-chat",
            messages=chat_data["messages"],
            temperature=0.7,
            max_tokens=800,
        )
        reply = strip_brackets(r.choices[0].message.content)
    except Exception as e:
        reply = f"好像卡了一下……再说一次？"

    chat_data["messages"].append({"role": "assistant", "content": reply})
    chat_data["is_first_run"] = is_first_run()
    save_json(CHAT_FILE, chat_data)

    # 后台提取记忆
    recent = "\n".join([
        f"{'理理喵' if m['role'] == 'assistant' else '你'}: {m['content'][:200]}"
        for m in chat_data["messages"][-8:]
    ])
    update_memory(recent, client=client if user_key else None)

    # 后台提取待办
    new_todos = []
    if triggered_todo:
        recent_full = "\n".join([
            f"{'理理喵' if m['role'] == 'assistant' else '你'}: {m['content'][:300]}"
            for m in chat_data["messages"][-12:]
        ])
        new_todos = extract_todos(recent_full, client=client if user_key else None)

    return jsonify({
        "ok": True,
        "reply": reply,
        "todosUpdated": len(new_todos) > 0,
        "newTodos": new_todos,
    })

@app.route("/api/chat/history")
def api_chat_history():
    chat_data = load_json(CHAT_FILE, {"messages": [], "is_first_run": True})
    # 只返回可见消息（去掉 system prompt）
    visible = [m for m in chat_data.get("messages", []) if m["role"] != "system"]
    return jsonify({"messages": visible, "is_first_run": chat_data.get("is_first_run", True)})

@app.route("/api/chat/clear", methods=["POST"])
def api_chat_clear():
    if CHAT_FILE.exists():
        CHAT_FILE.unlink()
    return jsonify({"ok": True})

@app.route("/api/reset", methods=["POST"])
def api_reset():
    """一键重置：清空聊天、记忆、待办"""
    for f in [CHAT_FILE, MEMORY_FILE, TODOS_FILE]:
        if f.exists():
            f.unlink()
    return jsonify({"ok": True})

# ── 状态检查 ──
@app.route("/api/status")
def api_status():
    try:
        get_client()
        has_api_key = True
    except RuntimeError:
        has_api_key = False
    return jsonify({
        "has_api_key": has_api_key,
        "is_first_run": is_first_run()
    })

if __name__ == "__main__":
    import os
    import socket

    port = int(os.environ.get("PORT", 5000))
    # 本地运行时显示本机 IP，云环境跳过
    if not os.environ.get("RENDER"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except:
            local_ip = "127.0.0.1"
        print(f"  本机访问 → http://localhost:{port}")
        print(f"  WiFi 内 → http://{local_ip}:{port}")

    # ── ngrok 公网隧道（可选）──
    public_url = None
    ngrok_token = os.environ.get("NGROK_AUTH_TOKEN") or (
        open(ENV_FILE, encoding="utf-8").readlines()[1].strip()
        if ENV_FILE.exists() and len(open(ENV_FILE, encoding="utf-8").readlines()) >= 2
        else None
    )
    if ngrok_token:
        try:
            from pyngrok import ngrok
            ngrok.set_auth_token(ngrok_token)
            tunnel = ngrok.connect(port, "http")
            public_url = tunnel.public_url
            print(f"  🌐 公网地址 → {public_url}")
            print(f"     手机用流量也能打开！")
        except Exception as e:
            print(f"  [ngrok 启动失败: {e}]")

    print(f"\n理理喵 启动中... 端口 {port}")
    if not public_url:
        print("  （电脑和手机要在同一个 WiFi 下）")
    app.run(host="0.0.0.0", port=port, debug=False)
