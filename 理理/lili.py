"""
理理喵 — 主程序（终端版）
用法：python lili.py
退出：输入「理理再见」或 Ctrl+C
"""

import json
import re
import sys
from datetime import date
from pathlib import Path
from openai import OpenAI

# ---- 路径 ----
BASE = Path(__file__).parent
MEMORY_FILE = BASE / "lili_memory.json"
TODOS_FILE = BASE / "todos.json"
ENV_FILE = BASE / ".env"

# ---- API Key ----
def get_api_key():
    if ENV_FILE.exists():
        return ENV_FILE.read_text(encoding="utf-8").strip()
    key = input("理理喵：请输入你的 DeepSeek API key：").strip()
    ENV_FILE.write_text(key, encoding="utf-8")
    print("理理喵：记住了，以后不用再输。\n")
    return key

# ---- 客户端（在 main() 中初始化）----
client = None

# ---- 加载/保存 ----
def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def strip_brackets(text):
    """移除 AI 回复中的所有括号动作描写。"""
    text = re.sub(r'（[^）]*）', '', text)
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'【[^】]*】', '', text)
    text = re.sub(r'［[^］]*］', '', text)
    text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
    return text.strip()

# ---- 安全的 JSON 提取 ----
def safe_json_parse(text):
    """从 LLM 返回的文本中提取 JSON，处理 markdown 包裹和多余文字。"""
    text = text.strip()
    # 去掉 ```json ... ``` 包裹
    if text.startswith("```"):
        lines = text.split("\n")
        # 去掉第一行 ```json 和最后一行 ```
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines)
    # 如果还不是 JSON，尝试提取 [...] 或 {...}
    if not text.startswith("[") and not text.startswith("{"):
        for start_char, end_char in [("[", "]"), ("{", "}")]:
            si = text.find(start_char)
            ei = text.rfind(end_char)
            if si != -1 and ei != -1 and ei > si:
                text = text[si:ei + 1]
                break
    return json.loads(text)

# ---- 记忆更新 ----
def update_memory(conversation_text):
    """用轻量 LLM 调用从对话中提取关于用户的新信息"""
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
        sys.stderr.flush()

    mem["learned"] = learned
    mem["chat_count"] = mem.get("chat_count", 0) + 1
    save_json(MEMORY_FILE, mem)

# ---- 待办提取 ----
def extract_todos(conversation_text):
    """从对话中提取新待办"""
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
        sys.stderr.flush()
        return []

# ---- 显示待办 ----
def show_todos():
    data = load_json(TODOS_FILE, {"todos": []})
    todos = data.get("todos", [])
    if not todos:
        print("\n理理喵：还没有待办。你想列什么的话跟我说。\n")
        return

    pending = [t for t in todos if t.get("status") != "done"]
    if not pending:
        print("\n理理喵：全都搞定了。\n")
        return

    # 按优先级分组
    high = [t for t in pending if t.get("priority") == "高"]
    mid = [t for t in pending if t.get("priority") == "中"]
    low = [t for t in pending if t.get("priority") == "低"]

    def print_group(title, items):
        if not items:
            return
        print(f"\n  {title}")
        for t in items:
            icon = "🧘" if t.get("type") == "soothe" else "📋"
            print(f"  ├── {icon} {t['content']:<30} {t.get('estimated_time',''):>8}")

    print_group("📋 先做", high)
    print_group("📋 其次", mid)
    print_group("📋 有空做", low)
    print()

# ---- 划掉待办 ----
def mark_done(user_input):
    """检查用户输入是否包含「完成某待办」的意图。"""
    data = load_json(TODOS_FILE, {"todos": []})
    done_count = 0
    for t in data.get("todos", []):
        if t.get("status") != "done" and t["content"] in user_input:
            t["status"] = "done"
            done_count += 1
            print(f"理理喵：划掉了 ✅ {t['content']}")
    if done_count:
        save_json(TODOS_FILE, data)
    return done_count > 0

# ---- 重置 ----
def reset_all():
    for f in [MEMORY_FILE, TODOS_FILE]:
        if f.exists():
            f.unlink()
    print("理理喵：全部清空了。下次见面，重新认识。喵。\n")

# ---- 显示记忆 ----
def show_memory():
    mem = load_json(MEMORY_FILE, {})
    config = mem.get("config", {})
    learned = mem.get("learned", {})
    print(f"\n  称呼：{config.get('name', '未知')}")
    print(f"  高效时段：{config.get('peak_time', '未知')}")
    print(f"  专注时长：{config.get('focus_duration', '未知')}")
    print(f"  夸夸：{'需要' if config.get('needs_praise') else '不用'}")
    print(f"  催促：{config.get('nudge_mode', '未知')}")
    print(f"  聊天次数：{mem.get('chat_count', 0)}")
    if learned.get("traits"):
        print(f"  了解：{'、'.join(learned['traits'])}")
    print()

# ============ 首次见面流程 ============

def parse_choice(answer, options, default):
    """从用户自由回答中解析 A/B/C/D 选项。"""
    upper = answer.upper().strip()
    # 精确匹配字母
    for i, opt in enumerate(options):
        if chr(ord('A') + i) in upper:
            return opt
    # 关键词匹配
    for opt in options:
        if opt in answer:
            return opt
    return default

def run_first_time_setup():
    """代码驱动的首次见面——理理喵版。问名字 → 4个偏好题 → 返回 config。"""
    config = {}

    # ── 自我介绍 ──
    print("\n理理喵：喵。你开门了。")
    print("理理——住这的猫。不是宠物啊，室友。")
    print("你忙你的猫躺猫的。想说话猫在。\n")

    # ── 问名字 ──
    name = input("理理喵：怎么叫你？\n你：").strip()
    config["name"] = name if name else "人类"
    print(f"\n理理喵：行，{config['name']}。猫问几个问题。不用太认真随便答。\n")

    # ── 问题1：高效时段 ──
    print("理理喵：你一天里什么时候最清醒？")
    print("  A 上午  B 下午  C 晚上  D 深夜")
    q1 = input("你：").strip()
    config["peak_time"] = parse_choice(q1, ["上午", "下午", "晚上", "深夜"], "晚上")
    print(f"理理喵：记下了。\n")

    # ── 问题2：专注时长 ──
    print("理理喵：一次能专心多久？")
    print("  A 25分钟就困了  B 能撑1-2小时  C 看状态")
    q2 = input("你：").strip()
    config["focus_duration"] = parse_choice(q2, ["25分钟", "1-2小时", "看状态"], "看状态")
    print(f"理理喵：喵。\n")

    # ── 问题3：夸夸 ──
    print("理理喵：做了事需要猫夸你吗？")
    print("  A 需要  B 不用")
    q3 = input("你：").strip()
    config["needs_praise"] = ("a" in q3.lower() or "需要" in q3 or "要" in q3)
    if config["needs_praise"]:
        print("理理喵：行，猫偶尔可以夸你两句。\n")
    else:
        print("理理喵：好，猫省口水。\n")

    # ── 问题4：催促模式 ──
    print("理理喵：最后——如果你有些事情拖着没做，希望猫：")
    print("  A 挠你一下提醒")
    print("  B 别管，你自己会说")
    print("  C 先看看你心情再说")
    q4 = input("你：").strip()
    q4_upper = q4.upper()
    if "A" in q4_upper or "挠" in q4 or "提醒" in q4:
        config["nudge_mode"] = "gentle"
    elif "B" in q4_upper or "别" in q4 or "自己" in q4:
        config["nudge_mode"] = "silent"
    elif "C" in q4_upper or "心情" in q4:
        config["nudge_mode"] = "mood_first"
    else:
        config["nudge_mode"] = "mood_first"

    mode_names = {"gentle": "猫挠你", "silent": "猫不说话", "mood_first": "猫看心情"}
    print(f"理理喵：明白了——{mode_names.get(config['nudge_mode'], '看心情')}。\n")

    return config


# ============ 主程序 ============

from prompt import LILI_SYSTEM_PROMPT

def main():
    global client
    api_key = get_api_key()
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    mem = load_json(MEMORY_FILE, {
        "config": {},
        "learned": {"triggers": [], "soothers": [], "traits": [], "dislikes": []},
        "chat_count": 0
    })
    config = mem.get("config", {})
    is_first_run = not config or not config.get("peak_time")

    # ── 首次运行：代码驱动的见面流程 ──
    if is_first_run:
        config = run_first_time_setup()
        mem["config"] = config
        mem["chat_count"] = 0
        save_json(MEMORY_FILE, mem)

        name = config.get("name", "你")
        memory_context = (
            f"用户称呼：{name}\n"
            f"偏好：{json.dumps(config, ensure_ascii=False)}\n"
            f"你对ta的了解：{json.dumps(mem.get('learned', {}), ensure_ascii=False)}\n"
            f"聊天次数：0"
        )
        messages = [
            {"role": "system", "content": LILI_SYSTEM_PROMPT + "\n\n---\n关于正在和你聊天的人：\n" + memory_context},
            {"role": "assistant", "content": f"好了，{name}，想说什么随时说，不想说话也可以不说。"}
        ]
        print(f"理理喵：好了，{name}。想说就说，不想说就躺着。喵。\n")

    else:
        name = config.get("name", "你")
        memory_context = (
            f"用户称呼：{name}\n"
            f"偏好：{json.dumps(config, ensure_ascii=False)}\n"
            f"你对ta的了解：{json.dumps(mem.get('learned', {}), ensure_ascii=False)}\n"
            f"聊天次数：{mem.get('chat_count', 0)}"
        )
        messages = [
            {"role": "system", "content": LILI_SYSTEM_PROMPT + "\n\n---\n关于正在和你聊天的人：\n" + memory_context},
            {"role": "assistant", "content": f"喵。{name}，今天怎么样？"}
        ]

    # ── 对话循环 ──
    while True:
        try:
            user_input = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n理理喵：好，下次聊。\n")
            break

        if not user_input:
            continue

        # ── 特殊指令（精确匹配，不发给 AI）──

        if user_input == "理理再见":
            print("理理喵：喵。下次聊。\n")
            break

        if "理理 重置" in user_input:
            reset_all()
            print("（请重新运行 python lili.py）")
            return

        if "理理 记忆" in user_input:
            show_memory()
            continue

        if "理理 设置" in user_input:
            reset_all()
            print("理理喵：下次见面，我们重新认识。\n（请重新运行 python lili.py）")
            return

        # ── 理理 划掉 xxx ──
        if "理理 划掉" in user_input:
            mark_done(user_input)
            continue

        # ── 帮我理理 / 帮我整理：AI 回复 + 生成待办 ──
        if any(kw in user_input for kw in ["帮我理理", "帮我整理"]):
            messages.append({"role": "user", "content": user_input})

            # 先让理理正常回复
            try:
                r = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=messages,
                    temperature=0.7,
                    max_tokens=800,
                )
                reply = strip_brackets(r.choices[0].message.content)
            except Exception as e:
                reply = f"（好像卡了一下...再说一次？）"
            print(f"\n理理喵：{reply}\n")
            messages.append({"role": "assistant", "content": reply})

            # 再从完整对话提取结构化待办
            print("（理理在整理待办...）")
            recent = "\n".join([
                f"{'理理喵' if m['role'] == 'assistant' else '你'}: {m['content'][:300]}"
                for m in messages[-12:]
            ])
            new_todos = extract_todos(recent)
            if new_todos:
                show_todos()
            else:
                print("（待办提取未生成新项——可能是对话中没有具体的行动点，或者 API 出错了）\n")
            continue

        # ── 看下待办 / 清单：只展示 ──
        if any(kw in user_input for kw in ["看下待办", "今天要干嘛", "待办", "清单"]):
            show_todos()
            continue

        # ── 自然语言划掉（做完了/搞定了/完成了）──
        if any(kw in user_input for kw in ["做完了", "搞定了", "完成了"]):
            if mark_done(user_input):
                continue
            # 如果没匹配到任何待办，走正常聊天（用户只是在说话中用到了这些词）

        # ── 正常聊天 ──
        messages.append({"role": "user", "content": user_input})

        try:
            r = client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                temperature=0.7,
                max_tokens=800,
            )
            reply = strip_brackets(r.choices[0].message.content)
        except Exception as e:
            reply = f"（好像卡了一下...再说一次？）"

        print(f"\n理理喵：{reply}\n")
        messages.append({"role": "assistant", "content": reply})

        # 后台更新记忆和待办
        recent = "\n".join([
            f"{'理理喵' if m['role'] == 'assistant' else '你'}: {m['content'][:200]}"
            for m in messages[-8:]
        ])
        update_memory(recent)
        extract_todos(recent)


if __name__ == "__main__":
    main()
