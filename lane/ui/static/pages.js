// 统计页和配置页。
import { h, t, put, api, act, toast } from "./core.js";

function tile(label, value, sub) {
  return h("div", { class: "tile" }, h("div", { class: "tile-value" }, value), h("div", { class: "tile-label" }, label),
    sub ? h("div", { class: "muted small" }, sub) : null);
}

function stageTable(data) {
  const longest = Math.max(1, ...data.stages.map((s) => s.avg_min));
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["stats.col_stage", "stats.col_runs", "stats.col_avg", "stats.col_total", "stats.col_failed", "stats.col_blocked"]
      .map((k) => h("th", {}, t(k))))),
    h("tbody", {}, data.stages.map((s) => {
      const fill = h("div", { class: "bar-fill" });
      fill.style.width = `${Math.round((s.avg_min / longest) * 100)}%`;
      return h("tr", {},
        h("td", { class: "nowrap" }, t(`stage.${s.stage}`)),
        h("td", { class: "num" }, s.runs),
        h("td", {}, h("div", { class: "bar-cell" }, h("span", { class: "num" }, s.avg_min), h("div", { class: "bar" }, fill))),
        h("td", { class: "num" }, s.total_min),
        h("td", { class: "num" }, s.failed || ""),
        h("td", { class: "num" }, s.blocked || ""));
    }))));
}

function usageTable(data) {
  if (!data.usage_days.length) return h("p", { class: "muted" }, t("stats.no_usage"));
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, t("stats.col_date")),
      data.agents.map((a) => h("th", { class: "num" }, `${a}${data.budget[a] ? ` (${t("stats.budget")} ${data.budget[a]})` : ""}`)))),
    h("tbody", {}, data.usage_days.slice().reverse().map((d) => h("tr", {},
      h("td", { class: "nowrap" }, d.date), data.agents.map((a) => h("td", { class: "num" }, d[a] || 0)))))));
}

export async function renderStats(root, isCurrent) {
  const project = h("select", {}, h("option", { value: "" }, t("stats.all_projects")));
  const from = h("input", { type: "date" });
  const to = h("input", { type: "date" });
  const body = h("div", {});
  const load = async () => {
    let data;
    try {
      data = await api(`/api/stats?project=${encodeURIComponent(project.value)}&from=${from.value}&to=${to.value}`);
    } catch (err) { toast(err.message, true); return; }
    if (!isCurrent()) return;
    if (project.options.length === 1) data.projects.forEach((p) => project.append(h("option", { value: p }, p)));
    const by = data.by_status;
    const rate = data.reviewed ? `${Math.round((data.review_first_pass / data.reviewed) * 100)}%` : "—";
    put(body, 
      h("div", { class: "tiles" },
        tile(t("stats.total"), data.total, t("stats.total_sub", { done: by.done || 0, blocked: by.blocked || 0, waiting: by.waiting || 0 })),
        tile(t("stats.first_pass"), rate, t("stats.first_pass_sub", { pass: data.review_first_pass, all: data.reviewed })),
        tile(t("stats.avg_rounds"), data.avg_rounds),
        tile(t("stats.cycle"), data.done ? data.avg_cycle_min : "—", t("stats.cycle_sub", { n: data.done }))),
      h("section", { class: "panel" }, h("h2", {}, t("stats.stages")), stageTable(data)),
      h("section", { class: "panel" }, h("h2", {}, t("stats.blocked")),
        data.blocked_notes.length
          ? h("ul", {}, data.blocked_notes.map((b) => h("li", {}, h("span", { class: "count" }, b.count), " ", b.note || t("stats.no_note"))))
          : h("p", { class: "muted" }, t("stats.no_blocked"))),
      h("section", { class: "panel" }, h("h2", {}, t("stats.usage")), usageTable(data)),
      data.total < 5 ? h("p", { class: "hint" }, t("stats.thin")) : null);
  };
  [project, from, to].forEach((el) => el.addEventListener("change", load));
  put(root, 
    h("div", { class: "page-head" }, h("h1", {}, t("nav.stats"))),
    h("div", { class: "row wrap filters" },
      h("label", { class: "inline" }, t("stats.project"), project),
      h("label", { class: "inline" }, t("stats.from"), from),
      h("label", { class: "inline" }, t("stats.to"), to)),
    body);
  await load();
  return null;   // 统计页不自动刷新
}

export async function renderConfig(root, isCurrent) {
  let files;
  try { files = (await api("/api/config")).files; } catch (err) { toast(err.message, true); return null; }
  if (!isCurrent()) return null;
  const editor = h("textarea", { class: "editor", spellcheck: "false", wrap: "off" });
  const picker = h("select", {}, files.map((f) => h("option", { value: f }, f)));
  const status = h("span", { class: "muted small" });
  let loaded = "";
  const dirty = () => editor.value !== loaded;
  const open = async (key) => {
    try {
      loaded = (await api(`/api/config/file?key=${encodeURIComponent(key)}`)).text;
      editor.value = loaded;
      status.textContent = "";
    } catch (err) { toast(err.message, true); }
  };
  let current = files[0];
  picker.addEventListener("change", () => {
    if (dirty() && !confirm(t("config.discard_confirm"))) { picker.value = current; return; }
    current = picker.value;
    open(current);
  });
  editor.addEventListener("input", () => { status.textContent = dirty() ? t("config.unsaved") : ""; });
  const save = h("button", {
    class: "btn primary",
    onclick: async () => {
      if (await act("/api/config/file", { key: current, text: editor.value })) { loaded = editor.value; status.textContent = ""; }
    },
  }, t("config.save"));
  const newRules = h("button", {
    class: "btn",
    onclick: () => {
      const name = prompt(t("config.new_rules_prompt"));
      if (!name) return;
      if (!/^[A-Za-z0-9_-]+$/.test(name)) { toast(t("config.bad_name"), true); return; }
      const key = `rules/${name}.md`;
      if (!files.includes(key)) { files.push(key); picker.append(h("option", { value: key }, key)); }
      if (dirty() && !confirm(t("config.discard_confirm"))) return;
      picker.value = key; current = key;
      open("templates/AGENTS.md").then(() => { loaded = ""; status.textContent = t("config.from_template"); });
    },
  }, t("config.new_rules"));
  put(root, 
    h("div", { class: "page-head" }, h("h1", {}, t("nav.config"))),
    h("p", { class: "muted" }, t("config.intro")),
    h("div", { class: "row wrap" }, h("label", { class: "inline" }, t("config.file"), picker), newRules, save, status),
    editor,
    h("p", { class: "hint" }, t("config.server_note")));
  await open(current);
  return null;
}
