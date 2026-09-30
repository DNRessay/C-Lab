import { tokens } from "./api.js";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmt = new Intl.NumberFormat("en-ZA", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const pct = (v, dp = 1) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(dp)}%`);
export const money = (v) => (v === null || v === undefined || v === "" ? "" : `${Number(v) < 0 ? "-" : ""}R ${fmt.format(Math.abs(Number(v)))}`);
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

export function page() {
  if (!tokens.isLoggedIn()) {
    location.href = "/login.html";
    throw new Error("redirecting");
  }
  const nav = $("#nav");
  if (nav) {
    nav.innerHTML = `<a class="brand" href="/">C-Lab</a><span class="muted" style="font-size:0.85rem">Charlie's Lab</span>
      <div class="links"><button id="logout" class="secondary small">Log out</button></div>`;
    $("#logout").addEventListener("click", () => { tokens.clear(); location.href = "/login.html"; });
  }
}

export function guestPage() {
  if (tokens.isLoggedIn()) location.href = "/";
}
