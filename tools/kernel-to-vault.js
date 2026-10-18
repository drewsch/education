/* Генерирует заметки раздела «Ядро Linux» в хранилище Obsidian из tools/kernel-data.js.
   Источник правды для этого раздела — kernel-data.js (из него же собирается сайт);
   заметки в хранилище нужны, чтобы раздел читался офлайн в Obsidian.
   Запуск:  node tools/kernel-to-vault.js   [путь к папке базы знаний] */
const fs = require("fs");
const path = require("path");
const os = require("os");

const ROOT = path.dirname(__dirname);
const OUT = process.argv[2] || path.join(os.homedir(), "Documents", "Obsidian Vault",
  "01 База знаний НТ", "Ядро Linux");

const src = fs.readFileSync(path.join(ROOT, "tools", "kernel-data.js"), "utf8");
const KERNEL = new Function(src + ";return KERNEL;")();

const CAPS = /^[А-ЯЁA-Z0-9 ,()\/\-—:.«»"']{6,}$/;

// Тело темы — простой текст. Разбиваем на абзацы по пустым строкам: абзац, в котором
// есть хоть одна строка с отступом, — выровненный листинг, его целиком заворачиваем
// в код, иначе выравнивание развалится. Строки капсом становятся подзаголовками.
const NL = String.fromCharCode(10);
function body2md(text) {
  const out = [];
  const paras = text.split(new RegExp(NL + "[ \t]*" + NL));
  for (const para of paras) {
    const lines = para.split(NL).filter(l => l.trim() !== "");
    if (!lines.length) continue;
    if (lines.some(l => /^ {2,}\S/.test(l))) {
      // только пустые строки по краям: обычный trim() съел бы отступ первой строки
      const block = para.split(NL);
      while (block.length && !block[0].trim()) block.shift();
      while (block.length && !block[block.length - 1].trim()) block.pop();
      out.push("```text", block.join(NL), "```", "");
      continue;
    }
    for (const l of lines) {
      const t = l.trim();
      if (CAPS.test(t) && t === t.toUpperCase()) out.push("### " + t, "");
      else out.push(l);
    }
    out.push("");
  }
  return out.join(NL).replace(new RegExp("(" + NL + "){3,}", "g"), NL + NL).trim();
}

const callout = (kind, head, text) =>
  `> [!${kind}]- ${head}\n` + text.split("\n").map(l => l.trim() ? "> " + l : ">").join("\n");

const esc = s => s.replace(/\|/g, "\|").replace(/\n+/g, " ");
const safe = s => s.replace(/[\/:*?"<>|]/g, "-");

fs.mkdirSync(OUT, { recursive: true });
const made = [];

KERNEL.forEach((c, i) => {
  const name = `${String(i + 1).padStart(2, "0")} ${c.title}`;
  const L = [];
  L.push("---");
  L.push("domain: подготовка к собеседованиям");
  L.push("tags:");
  L.push("  - собеседование/ядро-linux");
  L.push("status: to-review");
  L.push(`order: ${i + 1}`);
  L.push("generated: tools/kernel-to-vault.js");
  L.push("---");
  L.push(`# ${c.icon} ${c.title}`);
  L.push("");
  L.push("> [!info] Сгенерированная заметка");
  L.push("> Собрана из `tools/kernel-data.js` репозитория сайта. Правки здесь **не попадут**");
  L.push("> на сайт и перезатрутся при следующей генерации — править нужно `kernel-data.js`.");
  L.push(`> Назад: [[Ядро Linux]] · Смежное: [[01 Linux — ядро, ресурсы, метрики]]`);
  L.push("");
  c.theory.forEach(t => { L.push(`## ${t.title}`, "", body2md(t.body), ""); });
  if (c.qa.length) {
    L.push("## Вопросы на собеседовании", "");
    c.qa.forEach((x, n) => L.push(`${n + 1}. ${x.q}`));
    L.push("", "### Эталонные ответы", "");
    c.qa.forEach(x => { L.push(callout("question", x.q, x.a), ""); });
  }
  if (c.terms.length) {
    L.push("## Глоссарий", "", "| Термин | Определение |", "|---|---|");
    c.terms.forEach(t => L.push(`| \`${esc(t.t)}\` | ${esc(t.d)} |`));
    L.push("");
  }
  fs.writeFileSync(path.join(OUT, safe(name) + ".md"), L.join("\n"), "utf8");
  made.push({ name: safe(name), icon: c.icon, title: c.title, th: c.theory.length, qa: c.qa.length, tm: c.terms.length });
});

const idx = [
  "---", "domain: подготовка к собеседованиям", "tags:", "  - собеседование/ядро-linux",
  "status: to-review", "order: 0", "generated: tools/kernel-to-vault.js", "---",
  "# ⚙️ Ядро Linux — подсистемы", "",
  "> [!info] Сгенерированный раздел",
  "> Источник — `tools/kernel-data.js` в репозитории сайта. Пересборка: `node tools/kernel-to-vault.js`.",
  "> Смежные темы: [[01 Linux — ядро, ресурсы, метрики]], [[00 Карта обучения]]", "",
  "| Подсистема | Тем | Вопросов | Терминов |", "|---|---|---|---|",
  ...made.map(m => `| ${m.icon} [[${m.name}\|${m.title}]] | ${m.th} | ${m.qa} | ${m.tm} |`),
  "",
  `Всего: ${made.reduce((a, m) => a + m.th, 0)} тем, ${made.reduce((a, m) => a + m.qa, 0)} вопросов, ${made.reduce((a, m) => a + m.tm, 0)} терминов.`,
  "",
];
fs.writeFileSync(path.join(OUT, "Ядро Linux.md"), idx.join("\n"), "utf8");

console.log("готово:", OUT);
made.forEach(m => console.log(` ${m.name} — теория ${m.th}, вопросы ${m.qa}, термины ${m.tm}`));
