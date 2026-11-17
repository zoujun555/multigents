// 入口：登录、导航、页面切换、定时刷新。
import { h, t, api, state, toast, loadLanguage, savedLanguage } from "./core.js";
import { renderBoard } from "./board.js";
import { renderLane } from "./lane.js";
import { renderStats, renderConfig } from "./pages.js";

const REFRESH_MS = 4000;
const app = document.getElementById("app");
let visit = 0;          // 每次切换页面加一，旧页面的异步结果据此作废
let refreshTimer = null;

function nav() {
  const current = location.hash.split("/")[1] || "board";
  const link = (name) => h("a", { href: `#/${name}`, class: current === name || (name === "board" && current === "lane") ? "active" : "" }, t(`nav.${name}`));
  const lang = h("select", { class: "lang", "aria-label": "Language" },
    h("option", { value: "zh", selected: state.lang === "zh" }, "中文"),
    h("option", { value: "en", selected: state.lang === "en" }, "English"));
  lang.addEventListener("change", async () => { await loadLanguage(lang.value); route(); });
  return h("header", { class: "nav" },
    h("span", { class: "brand" }, t("app.name")),
    h("nav", {}, link("board"), link("stats"), link("config")),
    h("div", { class: "row" }, lang,
      h("button", { class: "btn small", onclick: async () => { await api("/api/logout", {}); showLogin(); } }, t("logout"))));
}

// 浏览器标签页标题带上"需要你处理"的数量，比如 (2) 多 Agent 工作台，不打开页面也能看到
let loggedIn = false;
async function updateTitle() {
  if (!loggedIn) return;   // 登录页上不要去请求，否则会反复触发登录页
  try {
    const board = await api("/api/board");
    const needs = board.lanes.filter((l) => l.status === "waiting" || l.status === "blocked").length;
    document.title = (needs ? `(${needs}) ` : "") + t("app.name");
  } catch { /* 暂时连不上：保持原来的标题 */ }
}
setInterval(updateTitle, 8000);

function showLogin() {
  loggedIn = false;
  clearInterval(refreshTimer);
  visit += 1;
  const user = h("input", { type: "text", autocomplete: "username", required: true });
  const pass = h("input", { type: "password", autocomplete: "current-password", required: true });
  const error = h("p", { class: "error-text" });
  const form = h("form", {
    class: "login",
    onsubmit: async (event) => {
      event.preventDefault();
      try {
        await api("/api/login", { username: user.value, password: pass.value });
        route();
      } catch (err) { error.textContent = err.message; pass.value = ""; }
    },
  },
    h("h1", {}, t("app.name")),
    h("label", {}, t("login.username"), user),
    h("label", {}, t("login.password"), pass),
    error,
    h("button", { type: "submit", class: "btn primary" }, t("login.submit")),
    h("p", { class: "muted small" }, t("login.hint")));
  app.replaceChildren(form);
  user.focus();
}

async function route() {
  loggedIn = true;
  updateTitle();
  clearInterval(refreshTimer);
  visit += 1;
  const mine = visit;
  const isCurrent = () => mine === visit;
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  const page = parts[0] || "board";
  const main = h("main", {});
  app.replaceChildren(nav(), main);
  let refresh = null;
  if (page === "lane" && parts[1]) refresh = await renderLane(main, decodeURIComponent(parts[1]), isCurrent);
  else if (page === "stats") refresh = await renderStats(main, isCurrent);
  else if (page === "config") refresh = await renderConfig(main, isCurrent);
  else refresh = await renderBoard(main, isCurrent);
  if (refresh && isCurrent()) {
    refreshTimer = setInterval(() => { if (!document.hidden && !document.querySelector("dialog[open]")) refresh(); }, REFRESH_MS);
  }
}

async function start() {
  await loadLanguage(savedLanguage());
  document.title = t("app.name");
  state.onUnauthorized = showLogin;
  window.addEventListener("hashchange", route);
  try {
    const session = await api("/api/session");
    if (session.logged_in) route(); else showLogin();
  } catch (err) { toast(err.message, true); showLogin(); }
}

start();
