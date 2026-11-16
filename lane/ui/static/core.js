// 共用的辅助函数：建 DOM、调接口、多语言、提示条、把 Markdown 渲染成 DOM。
// 页面内容全部用 textContent 写入，不用 innerHTML，所以 Agent 写的文件内容不会被当成 HTML 执行。

export const state = { lang: "zh", strings: {}, onUnauthorized: null };

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "value") el.value = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// 替换容器的内容。会跳过 null/false，所以可以直接写条件表达式（replaceChildren 本身会把 null 显示成文字）。
export function put(root, ...children) {
  root.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

export function t(key, vars) {
  let text = state.strings[key] ?? key;
  for (const [name, value] of Object.entries(vars || {})) text = text.replace(`{${name}}`, value);
  return text;
}

export async function loadLanguage(lang) {
  const res = await fetch(`i18n/${lang}.json`, { cache: "no-cache" });
  state.strings = await res.json();
  state.lang = lang;
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  try { localStorage.setItem("lane.lang", lang); } catch { /* 无痕窗口等：不记也能用 */ }
}

export function savedLanguage() {
  try { return localStorage.getItem("lane.lang") || "zh"; } catch { return "zh"; }
}

export async function api(path, body) {
  const options = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "lane" }, body: JSON.stringify(body) };
  const res = await fetch(path, options);
  let data = {};
  try { data = await res.json(); } catch { /* 非 JSON 的错误页 */ }
  if (res.status === 401 && path !== "/api/login") {
    if (state.onUnauthorized) state.onUnauthorized();
    throw new Error(t("error.login_required"));
  }
  if (!res.ok) throw new Error(data.error || `${res.status}`);
  return data;
}

let toastTimer = null;
export function toast(text, isError) {
  const box = document.getElementById("toast");
  box.textContent = text;
  box.className = isError ? "toast show error" : "toast show";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.className = "toast"; }, isError ? 9000 : 5000);
}

// 执行一个操作：成功显示后端的结果文字，失败显示原因。返回是否成功。
export async function act(path, body) {
  try {
    const data = await api(path, body);
    toast(data.message || t("done"));
    return true;
  } catch (err) {
    toast(err.message, true);
    return false;
  }
}

export function age(stamp) {
  if (!stamp) return "";
  const minutes = Math.max(0, (Date.now() - new Date(stamp).getTime()) / 60000);
  if (minutes < 60) return t("age.minutes", { n: Math.floor(minutes) });
  if (minutes < 1440) return t("age.hours", { n: (minutes / 60).toFixed(1) });
  return t("age.days", { n: (minutes / 1440).toFixed(1) });
}

export function badge(status) {
  return h("span", { class: `badge ${status}` }, t(`status.${status}`));
}

// ---- 极简 Markdown：标题、列表、代码块、表格、粗体、行内代码 ----

function inline(text) {
  const out = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  for (const m of text.matchAll(pattern)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const token = m[0];
    out.push(token.startsWith("**") ? h("strong", {}, token.slice(2, -2)) : h("code", {}, token.slice(1, -1)));
    last = m.index + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function tableFrom(rows) {
  const cells = (line) => line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
  const body = rows.filter((r, i) => i !== 1);
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, cells(body[0]).map((c) => h("th", {}, inline(c))))),
    h("tbody", {}, body.slice(1).map((r) => h("tr", {}, cells(r).map((c) => h("td", {}, inline(c))))))));
}

export function markdown(text) {
  const root = h("div", { class: "md" });
  const lines = text.split(/\r?\n/);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const code = [];
      for (i += 1; i < lines.length && !lines[i].startsWith("```"); i += 1) code.push(lines[i]);
      root.append(h("pre", {}, code.join("\n")));
      i += 1;
    } else if (/^#{1,6} /.test(line)) {
      const level = Math.min(line.match(/^#+/)[0].length + 1, 5);
      root.append(h(`h${level}`, {}, inline(line.replace(/^#+ /, ""))));
      i += 1;
    } else if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
      const rows = [];
      for (; i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i]); i += 1) rows.push(lines[i]);
      root.append(tableFrom(rows));
    } else if (/^\s*([-*]|\d+\.) /.test(line)) {
      const ordered = /^\s*\d+\. /.test(line);
      const list = h(ordered ? "ol" : "ul", {});
      for (; i < lines.length && /^\s*([-*]|\d+\.) /.test(lines[i]); i += 1) {
        list.append(h("li", {}, inline(lines[i].replace(/^\s*([-*]|\d+\.) /, ""))));
      }
      root.append(list);
    } else if (line.trim() === "") {
      i += 1;
    } else {
      const para = [];
      for (; i < lines.length && lines[i].trim() !== "" && !/^(#{1,6} |```|\s*([-*]|\d+\.) |\s*\|)/.test(lines[i]); i += 1) para.push(lines[i]);
      root.append(h("p", {}, inline(para.join(" "))));
    }
  }
  return root;
}
