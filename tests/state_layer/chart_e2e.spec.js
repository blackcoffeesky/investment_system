/* 端到端（最小 DOM shim）：真的加载 chart.js、真的派发 input 事件、断言属性被重写。
 * 跑法： node tests/state_layer/chart_e2e.spec.js
 *
 * 局限说清楚：这是自己写的最小 DOM，不是浏览器。它能证明
 * "事件绑定 → paint() 执行 → 元素坐标/显隐被改写"，不能证明 CSS 布局与视觉。
 */
const assert = require("assert");

// ---------------- 最小 DOM
class El {
  constructor(tag, attrs = {}) {
    this.tag = tag;
    this.attrs = {};
    this.dataset = {};
    this.style = {};
    this.listeners = {};
    this.textContent = "";
    this.children = [];
    for (const [k, v] of Object.entries(attrs)) this.attrs[k] = String(v);
    for (const [k, v] of Object.entries(attrs)) {
      if (k.startsWith("data-")) this.dataset[k.slice(5)] = String(v);
    }
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  fire(t) { (this.listeners[t] || []).forEach((f) => f({ type: t })); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const want = sel.split(",").map((s) => s.trim());
    return this.children.filter((c) => want.some((w) => {
      if (w.startsWith(".")) return (c.attrs.class || "").split(" ").includes(w.slice(1));
      return c.tag === w;
    }));
  }
  append(c) { this.children.push(c); return c; }
}

function build(rows) {
  const doc = new El("doc");
  const box = doc.append(new El("script", { id: "d-c" }));
  box.textContent = JSON.stringify({
    n: rows.length, dates: rows.map((r) => r.date),
    amounts: rows.map((r) => r.amount), refit: [10],
    geo: { h1: 130, h2: 40, gap: 14, pad_l: 44, pad_t: 10, plot_w: 846,
           cmin: 0, cmax: 100, amax: 1e9 },
  });
  const bar = doc.append(new El("div", { class: "rngbar", "data-cid": "c" }));
  const track = doc.append(new El("div", { class: "track" }));
  const lo = track.append(new El("input", { class: "lo", type: "range" }));
  const hi = track.append(new El("input", { class: "hi", type: "range" }));
  bar.querySelector = (sel) =>
    sel === ".lo" ? lo : sel === ".hi" ? hi
      : sel === ".rlabel" ? { textContent: "" } : null;
  const svg = doc.append(new El("svg", { class: "chart", "data-cid": "c" }));
  const segs = [];
  for (let i = 0; i < rows.length - 1; i++) {
    const el = svg.append(new El("line", { class: "seg", "data-a": i, "data-b": i + 1 }));
    el.setAttribute("x1", "0"); el.setAttribute("x2", "0");
    segs.push(el);
  }
  svg.querySelectorAll = (sel) =>
    sel.includes(".seg") ? segs : [];
  doc.getElementById = (id) => (id === "d-c" ? box : null);
  doc.querySelector = (sel) => (sel.startsWith("svg.chart") ? svg : null);
  return { doc, segs, lo, hi, bar, svg };
}

const rows = Array.from({ length: 300 }, (_, i) => ({
  date: `2026-01-${String((i % 28) + 1).padStart(2, "0")}`,
  amount: 1e8 + i, close: i, label: "range",
}));

const { doc, segs, lo, hi } = build(rows);
const chart = require("../../state_layer/web/static/chart.js");
chart.bind(doc);

// 初始：默认最近一年 → 49 条可见（300-251）
const vis = (arr) => arr.filter((s) => s.style.display !== "none").length;
const before = vis(segs);
const beforeX1 = segs[segs.length - 1].attrs.x1;

lo.value = 100;
lo.fire("input");
const after = vis(segs);
const afterX1 = segs[segs.length - 1].attrs.x1;

console.log("before visible=%d lastX1=%s | after visible=%d lastX1=%s",
            before, beforeX1, after, afterX1);
assert.notStrictEqual(before, after, "派发 input 后可见段数应变化");
assert.notStrictEqual(beforeX1, afterX1, "派发 input 后坐标应重算");
console.log("chart_e2e OK");
