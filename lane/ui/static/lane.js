// 需求页：阶段经过、各阶段产出的文件、最近的日志，以及对这条需求的操作。
import { h, t, put, api, act, age, badge, toast, markdown } from "./core.js";

function minutesBetween(start, end) {
  const minutes = (new Date(end) - new Date(start)) / 60000;
  return minutes < 1 ? t("age.seconds", { n: Math.round(minutes * 60) }) : t("age.minutes", { n: minutes.toFixed(1) });
}

function timeline(data) {
  const rows = (data.history || []).map((e) => h("tr", {},
    h("td", { class: "nowrap muted" }, e.at.replace("T", " ").slice(5, 16)),
    h("td", { class: "nowrap" }, t(`stage.${e.stage}`)),
    h("td", { class: "nowrap" }, h("span", { class: `result ${e.result}` }, t(`result.${e.result}`))),
    h("td", { class: "nowrap muted" }, e.started ? minutesBetween(e.started, e.at) : ""),
    h("td", {}, e.note || "", (e.reverted || []).length ? ` ${t("lane.reverted", { n: e.reverted.length })}` : "")));
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["lane.col_time", "lane.col_stage", "lane.col_result", "lane.col_duration", "lane.col_note"]
      .map((k) => h("th", {}, t(k))))),
    h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { colspan: "5", class: "muted" }, t("lane.no_history"))))));
}

function actions(data, refresh) {
  if (data.archived) return h("p", { class: "muted" }, t("lane.archived"));
  const id = data.id;
  const items = [];
  if (data.status === "waiting") {
    const note = h("textarea", { rows: "3", placeholder: t("lane.reject_hint") });
    // 批准规格时可以附带意见（编码、修复、评审阶段都会读到）；确认合并时没有这个框
    const approveNotes = data.stage === "spec_gate" ? h("textarea", { rows: "3", placeholder: t("lane.approve_notes_hint") }) : null;
    items.push(
      h("p", {}, t(data.stage === "spec_gate" ? "lane.gate_spec" : "lane.gate_merge")),
      approveNotes ? h("label", {}, t("lane.approve_notes_label"), approveNotes) : null,
      h("div", { class: "row" },
        h("button", {
          class: "btn primary",
          onclick: async () => { if (await act("/api/lane/approve", { id, notes: approveNotes ? approveNotes.value : "" })) refresh(); },
        }, t("lane.approve"))),
      h("label", {}, t("lane.reject_label"), note),
      h("div", { class: "row" },
        h("button", { class: "btn", onclick: async () => { if (await act("/api/lane/reject", { id, message: note.value })) refresh(); } }, t("lane.reject"))));
  }
  if (data.status === "blocked" || data.status === "queued" || data.status === "waiting") {
    // 停在闸门时默认选闸门前的那个阶段，免得误选成从头规划
    const current = { spec_gate: "plan", merge_gate: "report" }[data.stage] || data.stage;
    const stage = h("select", {}, data.auto_stages.map((s) => h("option", { value: s, selected: s === current }, t(`stage.${s}`))));
    const reset = h("input", { type: "checkbox" });
    items.push(h("div", { class: "row wrap" },
      h("label", { class: "inline" }, t("lane.resume_from"), stage),
      h("label", { class: "inline" }, reset, t("lane.reset_rounds")),
      h("button", {
        class: data.status === "blocked" ? "btn primary" : "btn",
        onclick: async () => { if (await act("/api/lane/resume", { id, stage: stage.value, reset_rounds: reset.checked })) refresh(); },
      }, t("lane.resume"))));
  }
  if (data.status !== "running") {
    const done = data.status === "done";
    items.push(h("div", { class: "row" }, h("button", {
      class: "btn danger",
      onclick: async () => {
        if (!confirm(t(done ? "lane.clean_confirm" : "lane.discard_confirm", { id }))) return;
        if (await act("/api/lane/clean", { id, force: !done })) location.hash = "#/board";
      },
    }, t(done ? "lane.clean" : "lane.discard"))));
  }
  if (data.status === "running") items.push(h("p", { class: "muted" }, t("lane.running_hint")));
  return items;
}

function files(data, reload) {
  const view = h("div", { class: "file-view" }, h("p", { class: "muted" }, t("lane.pick_file")));
  const tabs = h("div", { class: "tabs" });
  // 等待批准规格时，规格可以直接改；其他时候文件只读
  const specEditable = data.status === "waiting" && data.stage === "spec_gate" && !data.archived;
  const edit = (text) => {
    const box = h("textarea", { class: "editor spec-editor", spellcheck: "false" });
    box.value = text;
    view.replaceChildren(
      h("p", { class: "hint" }, t("lane.edit_hint")),
      box,
      h("div", { class: "row" },
        h("button", { class: "btn primary", onclick: async () => { if (await act("/api/lane/spec", { id: data.id, text: box.value })) reload(); } }, t("lane.save_spec")),
        h("button", { class: "btn", onclick: () => reload() }, t("cancel"))));
    box.focus();
  };
  const show = async (name, button) => {
    for (const b of tabs.children) b.classList.remove("active");
    button.classList.add("active");
    try {
      const res = await api(`/api/file?id=${encodeURIComponent(data.id)}&name=${encodeURIComponent(name)}`);
      put(view,
        specEditable && name === "spec.md"
          ? h("div", { class: "row end" }, h("button", { class: "btn small", onclick: () => edit(res.text) }, t("lane.edit_spec")))
          : null,
        markdown(res.text));
    } catch (err) { toast(err.message, true); }
  };
  for (const name of data.files) {
    const button = h("button", { class: "tab" }, name.replace(/\.md$/, ""));
    button.addEventListener("click", () => show(name, button));
    tabs.append(button);
  }
  if (!data.files.length) return h("p", { class: "muted" }, t("lane.no_files"));
  // 默认打开最需要看的那个：汇总 > 规格
  const preferred = ["summary.md", "spec.md", "request.md"].find((n) => data.files.includes(n));
  if (preferred) show(preferred, tabs.children[data.files.indexOf(preferred)]);
  return [tabs, view];
}

export async function renderLane(root, id, isCurrent) {
  let lastKey = "";
  const refresh = async () => {
    let data;
    try { data = await api(`/api/lane?id=${encodeURIComponent(id)}`); } catch (err) { toast(err.message, true); return; }
    if (!isCurrent()) return;
    // 状态没变就不重画，免得打断正在阅读或输入的人
    const key = `${data.stage}|${data.status}|${(data.history || []).length}|${data.files.join()}`;
    if (key === lastKey) {
      const log = root.querySelector("pre.log");
      if (log) log.textContent = data.log_tail || t("lane.no_log");
      return;
    }
    lastKey = key;
    put(root, 
      h("p", {}, h("a", { href: "#/board" }, `← ${t("nav.board")}`)),
      h("div", { class: "page-head" }, h("h1", {}, `${data.id} ${data.title}`), badge(data.status)),
      h("p", { class: "muted" }, `${data.project} · ${data.branch} · ${t(`stage.${data.stage}`)} · ${age(data.since)}`,
        data.round ? ` · ${t("board.round", { n: data.round })}` : ""),
      data.note ? h("p", { class: data.status === "blocked" ? "note blocked" : "note" }, data.note) : null,
      h("section", { class: "panel" }, h("h2", {}, t("lane.actions")), actions(data, () => { lastKey = ""; refresh(); })),
      h("section", { class: "panel" }, h("h2", {}, t("lane.files")), files(data, () => { lastKey = ""; refresh(); })),
      h("section", { class: "panel" }, h("h2", {}, t("lane.history")), timeline(data)),
      h("section", { class: "panel" }, h("h2", {}, t("lane.log"), " ", h("span", { class: "muted small" }, data.log_name)),
        h("pre", { class: "log" }, data.log_tail || t("lane.no_log"))));
  };
  await refresh();
  return refresh;
}
