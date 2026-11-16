// 看板：每条需求放在它所处的阶段里；顶部是调度器、用量和需要你处理的事项。
import { h, t, put, api, act, age, badge, toast } from "./core.js";

function usageBars(data) {
  return Object.keys(data.budget).map((agent) => {
    const used = data.usage[agent] || 0;
    const limit = data.budget[agent];
    const ratio = Math.min(1, used / limit);
    const fill = h("div", { class: ratio >= 1 ? "bar-fill full" : ratio >= 0.8 ? "bar-fill warn" : "bar-fill" });
    fill.style.width = `${Math.round(ratio * 100)}%`;
    return h("div", { class: "usage" },
      h("div", { class: "usage-label" }, h("span", {}, agent), h("span", { class: "muted" }, `${used} / ${limit}`)),
      h("div", { class: "bar" }, fill));
  });
}

function schedulerPanel(data, refresh) {
  const running = data.scheduler.running;
  const active = data.lanes.filter((l) => l.status === "running").length;
  const button = h("button", {
    class: running ? "btn" : "btn primary",
    onclick: async () => {
      if (running && !confirm(t("scheduler.stop_confirm"))) return;
      await act(running ? "/api/scheduler/stop" : "/api/scheduler/start", {});
      setTimeout(refresh, 800);
    },
  }, running ? t("scheduler.stop") : t("scheduler.start"));
  return h("section", { class: "panel" },
    h("h2", {}, t("scheduler.title")),
    h("div", { class: "row" },
      h("span", { class: running ? "dot on" : "dot" }),
      h("span", {}, running ? t("scheduler.running") : t("scheduler.stopped")),
      h("span", { class: "muted" }, t("scheduler.active", { n: active, max: data.max_parallel })),
      button),
    !running && data.lanes.some((l) => l.status === "queued") ? h("p", { class: "hint" }, t("scheduler.queued_hint")) : null);
}

function needsYou(data) {
  const items = data.lanes.filter((l) => l.status === "waiting" || l.status === "blocked");
  if (!items.length) return null;
  return h("section", { class: "panel attention" },
    h("h2", {}, t("board.needs_you"), h("span", { class: "count" }, items.length)),
    h("ul", { class: "plain" }, items.map((l) => h("li", {},
      h("a", { href: `#/lane/${l.id}`, class: "strong" }, l.id), " ", l.title, " ", badge(l.status),
      h("div", { class: "muted small" }, t(`stage.${l.stage}`), l.note ? ` · ${l.note}` : "")))));
}

function newDialog(data, refresh) {
  const dialog = h("dialog", {});
  const project = h("select", { required: true }, data.projects.map((p) => h("option", { value: p }, p)));
  const title = h("input", { type: "text", required: true, maxlength: "120" });
  const text = h("textarea", { rows: "10", placeholder: t("new.text_hint") });
  const form = h("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      if (await act("/api/lane/new", { project: project.value, title: title.value, text: text.value })) {
        dialog.close();
        refresh();
      }
    },
  },
    h("h2", {}, t("new.title")),
    h("label", {}, t("new.project"), project),
    h("label", {}, t("new.name"), title),
    h("label", {}, t("new.text"), text),
    h("div", { class: "row end" },
      h("button", { type: "button", class: "btn", onclick: () => dialog.close() }, t("cancel")),
      h("button", { type: "submit", class: "btn primary" }, t("new.create"))));
  dialog.append(form);
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}

function card(lane) {
  return h("a", { class: `card ${lane.status}`, href: `#/lane/${lane.id}` },
    h("div", { class: "card-head" }, h("span", { class: "strong" }, lane.id), badge(lane.status)),
    h("div", { class: "card-title" }, lane.title),
    h("div", { class: "muted small" }, lane.project, " · ", age(lane.since),
      lane.round ? ` · ${t("board.round", { n: lane.round })}` : ""));
}

function columns(data) {
  return h("div", { class: "kanban" }, data.stages.map((stage) => {
    const lanes = data.lanes.filter((l) => l.stage === stage);
    const gate = data.gates.includes(stage);
    return h("div", { class: gate ? "column gate" : "column" },
      h("div", { class: "column-head" }, t(`stage.${stage}`), h("span", { class: "count" }, lanes.length)),
      lanes.length ? lanes.map(card) : h("div", { class: "empty" }, "—"));
  }));
}

// 告警历史的筛选和展开状态：看板每几秒整个重画，这两个要记在外面
let historyProject = "";
let historyOpen = false;

function projectTag(project) {
  return project ? h("span", { class: "tag" }, project) : null;
}

function alertHistory(data, entries, refresh) {
  const filter = h("select", { "aria-label": t("alerts.project") },
    h("option", { value: "" }, t("stats.all_projects")),
    data.projects.map((p) => h("option", { value: p, selected: p === historyProject }, p)));
  filter.addEventListener("change", () => { historyProject = filter.value; refresh(); });
  const body = entries.length
    ? h("ul", { class: "plain" }, entries.map((e) => h("li", {},
        h("span", { class: "muted small nowrap" }, e.at.replace("T", " ").slice(5, 16)), " ", projectTag(e.project), " ",
        e.lane ? h("a", { href: `#/lane/${e.lane}` }, e.lane) : null, e.lane ? " " : null, e.text)))
    : h("p", { class: "muted" }, t("alerts.none"));
  const box = h("details", { class: "panel", open: historyOpen },
    h("summary", {}, t("alerts.history")), h("div", { class: "row" }, h("label", { class: "inline" }, t("alerts.project"), filter)), body);
  box.addEventListener("toggle", () => { historyOpen = box.open; });
  return box;
}

export async function renderBoard(root, isCurrent) {
  const refresh = async () => {
    let data, history;
    try {
      data = await api("/api/board");
      history = (await api(`/api/alerts?project=${encodeURIComponent(historyProject)}`)).alerts;
    } catch (err) { toast(err.message, true); return; }
    if (!isCurrent()) return;
    put(root, 
      h("div", { class: "page-head" },
        h("h1", {}, t("nav.board")),
        h("button", {
          class: "btn primary",
          disabled: !data.projects.length,
          title: data.projects.length ? null : t("new.no_project"),
          onclick: () => newDialog(data, refresh),
        }, t("new.title"))),
      h("div", { class: "top-grid" },
        schedulerPanel(data, refresh),
        h("section", { class: "panel" }, h("h2", {}, t("board.usage")), usageBars(data))),
      needsYou(data),
      data.alerts.length ? h("section", { class: "panel" }, h("h2", {}, t("board.alerts")),
        h("ul", {}, data.alerts.map((a) => h("li", {}, projectTag(a.project), a.project ? " " : null, a.text)))) : null,
      alertHistory(data, history, refresh),
      data.lanes.length ? columns(data) : h("p", { class: "empty-page" }, t("board.empty")));
  };
  await refresh();
  return refresh;   // 调用方每隔几秒调用一次
}
