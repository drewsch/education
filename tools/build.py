"""Сборка сайта: content/*.md + tools/template.html + tools/kernel-data.js -> public/index.html
Запуск из корня репозитория:  pip install markdown && python3 tools/build.py
"""
import re, json, os, sys
try:
    import markdown
except ImportError:
    sys.exit("Нужен пакет markdown: pip install markdown")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Источник заметок — хранилище Obsidian (единственный источник правды).
# Переопределяется переменной окружения NT_KB_DIR; если хранилища нет — берётся content/ в репозитории.
VAULT_KB = os.path.join(os.path.expanduser("~"), "Documents", "Obsidian Vault",
                        "01 База знаний НТ")
KB_DIR = os.environ.get("NT_KB_DIR") or VAULT_KB
if not os.path.isdir(KB_DIR):
    KB_DIR = os.path.join(ROOT, "content")
if not os.path.isdir(KB_DIR):
    sys.exit("Не найдена папка с заметками: " + KB_DIR)
KB = KB_DIR + os.sep
TOOLS = os.path.join(ROOT, "tools")
OUT = os.path.join(ROOT, "public", "index.html")

PAGES = [
    # file, id, short title, icon, color, group
    ("00 Карта обучения", "map", "Карта обучения", "🗺️", "#8A7B6A", "Старт"),
    ("01 Linux — ядро, ресурсы, метрики", "linux", "Linux: ресурсы и метрики", "🐧", "#C2552B", "Фундамент"),
    (None, "kernel", "Ядро Linux: подсистемы", "⚙️", "#9A4A66", "Фундамент"),
    ("02 Сеть — пропускная способность, фаерволы, узкие места", "net", "Сеть и фаерволы", "🌐", "#2F7D6E", "Фундамент"),
    ("03 Протоколы взаимодействия", "proto", "Протоколы", "🔌", "#4F7A3A", "Фундамент"),
    ("04 Аутентификация и токены", "auth", "Аутентификация и токены", "🔐", "#B07A18", "Фундамент"),
    ("05 PostgreSQL", "pg", "PostgreSQL", "🐘", "#A6482F", "Хранилища"),
    ("06 ClickHouse", "ch", "ClickHouse", "🟨", "#8F6B16", "Хранилища"),
    ("07 Redis", "redis", "Redis", "🟥", "#B33A2C", "Хранилища"),
    ("08 Kafka", "kafka", "Kafka", "📨", "#7E4A85", "Хранилища"),
    ("09 Camunda Zeebe", "camunda", "Camunda / Zeebe", "🔀", "#C2662B", "Хранилища"),
    ("10 JVM", "jvm", "JVM", "☕", "#A33B55", "Рантаймы"),
    ("11 Go", "go", "Go", "🐹", "#2E8074", "Рантаймы"),
    ("12 Python", "py", "Python и Locust", "🐍", "#6E7F26", "Рантаймы"),
    ("13 Kubernetes, OpenShift, Docker", "k8s", "Kubernetes / OpenShift", "☸️", "#8A5636", "Платформа и архитектура"),
    ("15 Микросервисы и системный дизайн", "sd", "Микросервисы и System Design", "🏗️", "#3D7A57", "Платформа и архитектура"),
    ("14 Теория тестирования и методология НТ", "qa", "Теория тестирования и НТ", "🧪", "#A8427A", "Процессы"),
    ("16 Дополнительная литература", "lit", "Дополнительная литература", "📚", "#9C6B2F", "Ресурсы"),
]
NAME2ID = {p[0]: p[1] for p in PAGES if p[0]}

def wikilinks(s):
    def rep(m):
        name = m.group(1)
        pid = NAME2ID.get(name)
        label = re.sub(r"^\d+\s+", "", name)
        if not pid:
            return label
        return f'<a class="wl" href="#{pid}">{label}</a>'
    return re.sub(r"\[\[([^\]]+)\]\]", rep, s)

LIST_RE = re.compile(r"^\s*([-*+]|\d+\.)\s+")
def fix_blank_lines(text):
    out, fence, prev = [], False, ""
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            if not fence and prev.strip() != "":
                out.append("")
            fence = not fence
            out.append(line); prev = line; continue
        if not fence:
            is_list = bool(LIST_RE.match(line))
            is_tbl = line.lstrip().startswith("|")
            prev_list = bool(LIST_RE.match(prev)) or prev.startswith("  ") or prev.startswith("\t")
            prev_tbl = prev.lstrip().startswith("|")
            if prev.strip() and ((is_list and not prev_list) or (is_tbl and not prev_tbl)):
                out.append("")
            if prev_tbl and not is_tbl and line.strip():
                out.append("")
        out.append(line); prev = line
    return "\n".join(out)

def md2html(s):
    s = fix_blank_lines(s)
    s = wikilinks(s)
    h = markdown.markdown(s, extensions=["tables", "fenced_code", "sane_lists"])
    h = re.sub(r'<a href="(https?://[^"]+)"', r'<a href="\1" target="_blank" rel="noopener"', h)
    h = h.replace("<table>", '<div class="tw"><table>').replace("</table>", "</table></div>")
    return h

def plain(s):
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"[`*_>#|]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def parse_page(fname):
    raw = open(KB + fname + ".md", encoding="utf-8").read()
    if raw.startswith("---"):
        raw = raw.split("---", 2)[2]
    lines = raw.strip("\n").split("\n")
    title = None
    secs = []           # (level, title, [lines])
    cur = (0, "__intro__", [])
    fence = False
    for line in lines:
        if line.strip().startswith("```"):
            fence = not fence
        if not fence:
            m = re.match(r"^(#{1,2}) (.+)$", line)
            if m:
                lvl = len(m.group(1)); t = m.group(2).strip()
                if lvl == 1 and title is None:
                    title = t; continue
                secs.append(cur)
                cur = (lvl, t, [])
                continue
        cur[2].append(line)
    secs.append(cur)
    intro, sections, questions = "", [], []
    for lvl, t, body in secs:
        text = "\n".join(body).strip()
        text = re.sub(r"^\*{3}\s*$", "", text, flags=re.M).strip()
        # collect questions from any heading inside
        qs = extract_questions(t, text)
        if qs:
            questions.append(qs)
        if t == "__intro__":
            intro = md2html(text) if text else ""
            continue
        if t.startswith("Содержание"):
            continue
        if re.match(r"^(\d+\.\s*)?Вопросы на собеседовании", t):
            continue
        if lvl == 1:
            sections.append({"t": t, "part": True, "h": md2html(text) if text else "", "x": plain(t + " " + text)[:20000]})
            continue
        sections.append({"t": t, "h": md2html(text), "x": plain(t + " " + text)[:20000]})
    return title, intro, sections, questions

def extract_questions(sec_title, text):
    # whole-section questions
    items = []
    if re.match(r"^(\d+\.\s*)?Вопросы на собеседовании", sec_title):
        grp = sec_title
        items = [m.group(1).strip() for m in re.finditer(r"^\s*\d+\.\s+(.+)$", text, flags=re.M)]
        return {"g": re.sub(r"^\d+\.\s*", "", grp), "items": [wikilinks(i) for i in items]} if items else None
    # H3/H4 "Вопросы на собеседовании" inside a section
    found = []
    for m in re.finditer(r"^#{3,4} [^\n]*Вопросы на собеседовании[^\n]*\n((?:\s*\n|\s*\d+\.\s+[^\n]+\n?)+)", text, flags=re.M):
        found += [x.group(1).strip() for x in re.finditer(r"^\s*\d+\.\s+(.+)$", m.group(1), flags=re.M)]
    if found:
        return {"g": re.sub(r"^\d+\.\s*", "", sec_title), "items": [wikilinks(i) for i in found]}
    return None

pages_out = []
for fname, pid, short, icon, color, group in PAGES:
    if fname is None:
        pages_out.append({"id": pid, "short": short, "icon": icon, "color": color, "group": group, "kind": "kernel"})
        continue
    title, intro, sections, questions = parse_page(fname)
    pages_out.append({"id": pid, "short": short, "icon": icon, "color": color, "group": group, "kind": "md",
                      "title": title, "intro": intro, "sections": sections, "questions": questions})

kernel_js = open(os.path.join(TOOLS, "kernel-data.js"), encoding="utf-8").read()
tpl = open(os.path.join(TOOLS, "template.html"), encoding="utf-8").read()
os.makedirs(os.path.dirname(OUT), exist_ok=True)
data_json = json.dumps(pages_out, ensure_ascii=False).replace("</", "<\\/")
html = tpl.replace("/*__KERNEL__*/", kernel_js).replace("__PAGES_JSON__", data_json)
open(OUT, "w", encoding="utf-8").write(html)
print("pages", len(pages_out), "size", len(html))
for p in pages_out:
    if p["kind"] == "md":
        print(p["id"], len(p["sections"]), "sections,", sum(len(q["items"]) for q in p["questions"]), "questions")
