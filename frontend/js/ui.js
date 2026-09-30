import { tokens } from "./api.js";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmt = new Intl.NumberFormat("en-ZA", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const pct = (v, dp = 1) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(dp)}%`);
const fine = new Intl.NumberFormat("en-ZA", { minimumFractionDigits: 2, maximumFractionDigits: 5 });
// Amounts under R1 (fractional-share dividends) keep up to 5 decimals so they don't show as R 0.00.
export const amount = (v) => (Math.abs(Number(v)) < 1 && Number(v) !== 0 ? fine : fmt).format(Math.abs(Number(v)));
export const money = (v) => (v === null || v === undefined || v === "" ? "" : `${Number(v) < 0 ? "-" : ""}R ${amount(v)}`);
export const day = (v) => (v ? String(v).slice(0, 10) : "");

export const badge = (text, kind = "") => `<span class="badge ${kind}">${esc(text)}</span>`;

export function flash(text, kind = "ok", el = $("#msg")) {
  if (!el) return alert(text);
  el.innerHTML = `<div class="msg ${kind}">${esc(text)}</div>`;
  el.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

export const formData = (form) => Object.fromEntries(new FormData(form).entries());
export const param = (k) => new URLSearchParams(location.search).get(k);

// Wraps a button click: disables it while the promise runs, flashes errors.
export function action(btn, fn) {
  btn.addEventListener("click", async (e) => {
    e.preventDefault();
    btn.disabled = true;
    try { await fn(e); } catch (err) { flash(err.message, "err"); } finally { btn.disabled = false; }
  });
}

export function onSubmit(form, fn) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = form.querySelector("button[type=submit], button:not([type])");
    if (btn) btn.disabled = true;
    try { await fn(formData(form), e); } catch (err) { flash(err.message, "err"); } finally { if (btn) btn.disabled = false; }
  });
}

export async function download(resPromise, filename) {
  const res = await resPromise;
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  a.click();
  URL.revokeObjectURL(url);
}

// Polls fn() every `ms` until done(result) is true.
export async function poll(fn, done, onTick, ms = 2000, maxTries = 450) {
  for (let i = 0; i < maxTries; i++) {
    const r = await fn();
    onTick?.(r);
    if (done(r)) return r;
    await new Promise((res) => setTimeout(res, ms));
  }
  throw new Error("Timed out waiting for the job.");
}

export const empty = (cols, text) => `<tr><td colspan="${cols}" class="muted">${esc(text)}</td></tr>`;

// Top bar. `links` = [[id, label], ...] become menu items (#id); an entry [id, label, [[id, label], ...]] is a
// dropdown group. On phones everything folds into a burger menu, with group items indented under their heading.
export function page(links = []) {
  if (!tokens.isLoggedIn()) {
    location.href = "/login.html";
    throw new Error("redirecting");
  }
  const nav = $("#nav");
  if (!nav) return;
  const item = ([id, label]) => `<a href="#${esc(id)}" data-tab="${esc(id)}">${esc(label)}</a>`;
  const entry = (l) => l[2]
    ? `<div class="group" data-group="${esc(l[0])}">
         <button type="button" class="group-btn" aria-haspopup="true" aria-expanded="false">${esc(l[1])}<span class="caret">▾</span></button>
         <div class="submenu" role="menu">${l[2].map(item).join("")}</div>
       </div>`
    : item(l);
  nav.innerHTML = `<span class="wm-clip" aria-hidden="true"><span class="watermark">Charlie'$ Lab</span></span><a class="brand" href="/">C-Lab</a>
    <button class="menu" aria-label="Open menu" aria-expanded="false"><span></span><span></span><span></span></button>
    <div class="links">${links.map(entry).join("")}
      <button id="logout" class="secondary small">Log out</button></div>`;
  const menu = $(".menu", nav);
  const setOpen = (open) => {
    nav.classList.toggle("open", open);
    menu.setAttribute("aria-expanded", open);
    menu.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  };
  const closeGroups = (except) => $$(".group", nav).forEach((g) => {
    if (g === except) return;
    g.classList.remove("open");
    $(".group-btn", g).setAttribute("aria-expanded", "false");
  });
  menu.addEventListener("click", () => setOpen(!nav.classList.contains("open")));
  $$(".group-btn", nav).forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    const g = b.closest(".group"), open = !g.classList.contains("open");
    closeGroups(g);
    g.classList.toggle("open", open);
    b.setAttribute("aria-expanded", open);
  }));
  document.addEventListener("click", (e) => { if (!e.target.closest(".group")) closeGroups(); });
  $$(".links a", nav).forEach((a) => a.addEventListener("click", () => { setOpen(false); closeGroups(); }));
  $("#logout").addEventListener("click", () => { tokens.clear(); location.href = "/login.html"; });
}

export function setActive(id) {
  $$("#nav .links a").forEach((a) => a.classList.toggle("active", a.dataset.tab === id));
  $$("#nav .group").forEach((g) => g.classList.toggle("active", !!g.querySelector(`a[data-tab="${id}"]`)));
}

export function guestPage() {
  if (tokens.isLoggedIn()) location.href = "/";
}
