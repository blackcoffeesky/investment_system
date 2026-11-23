/* 状态图的前端布局：纯函数 + DOM 绑定。
 * 纯函数（clampWindow / xAt / visible / segSpan）被 `chart_layout.spec.js` 用 node 直接测。
 * 浏览器里由 `bind(document)` 接管所有 `.rngbar`。
 */
(function (global) {
  "use strict";

  function clampWindow(lo, hi, n, minGap) {
    lo = Math.round(lo); hi = Math.round(hi);
    lo = Math.max(0, Math.min(lo, n - 1));
    hi = Math.max(0, Math.min(hi, n - 1));
    if (hi - lo < minGap) {
      if (lo + minGap <= n - 1) { hi = lo + minGap; }
      else { hi = n - 1; lo = Math.max(0, hi - minGap); }
    }
    return [lo, hi];
  }

  function xAt(i, lo, hi, padL, plotW) {
    return padL + plotW * (i - lo) / Math.max(1, hi - lo);
  }

  function visible(i, lo, hi) { return i >= lo && i <= hi; }

  // 一段 [a,b] 在窗口内可见的端点；不可见返回 null
  function segSpan(a, b, lo, hi) {
    var a2 = Math.max(a, lo), b2 = Math.min(b, hi);
    return b2 <= a2 ? null : [a2, b2];
  }

  function paint(svg, d, lo, hi) {
    var g = d.geo;
    lo = Math.max(0, Math.min(lo, hi));
    hi = Math.min(d.n - 1, Math.max(hi, lo));

    svg.querySelectorAll(".seg").forEach(function (el) {
      var a = +el.dataset.a, b = +el.dataset.b;
      var s = segSpan(a, b, lo, hi);
      if (!s) { el.style.display = "none"; return; }
      el.style.display = "";
      el.setAttribute("x1", xAt(s[0], lo, hi, g.pad_l, g.plot_w));
      el.setAttribute("x2", xAt(s[1], lo, hi, g.pad_l, g.plot_w));
    });

    svg.querySelectorAll(".amt").forEach(function (el) {
      var i = +el.dataset.t;
      if (!visible(i, lo, hi)) { el.style.display = "none"; return; }
      el.style.display = "";
      var h = (d.amounts[i] / (g.amax || 1)) * g.h2;
      var w = Math.max(0.8, g.plot_w / (hi - lo) * 0.6);
      el.setAttribute("x", xAt(i, lo, hi, g.pad_l, g.plot_w) - w / 2);
      el.setAttribute("width", w);
      el.setAttribute("y", g.pad_t + g.h1 + g.gap + g.h2 - h);
      el.setAttribute("height", h);
    });

    svg.querySelectorAll(".rf").forEach(function (el) {
      var i = +el.dataset.t;
      if (!visible(i, lo, hi)) { el.style.display = "none"; return; }
      el.style.display = "";
      var x = xAt(i, lo, hi, g.pad_l, g.plot_w);
      el.setAttribute("x1", x); el.setAttribute("x2", x);
    });

    return { lo: lo, hi: hi };
  }

  function bind(root) {
    root.querySelectorAll(".rngbar").forEach(function (bar) {
      var cid = bar.dataset.cid;
      var box = root.getElementById ? root.getElementById("d-" + cid)
                                   : root.querySelector("#d-" + cid);
      if (!box) return;
      var d = JSON.parse(box.textContent);
      var svg = root.querySelector('svg.chart[data-cid="' + cid + '"]');
      if (!svg) return;
      var lo = bar.querySelector(".lo"), hi = bar.querySelector(".hi");
      var minGap = 20;
      [lo, hi].forEach(function (el) { el.min = 0; el.max = d.n - 1; });
      lo.value = Math.max(0, d.n - 251);
      hi.value = d.n - 1;

      function upd(src) {
        var w = clampWindow(+lo.value, +hi.value, d.n, minGap);
        lo.value = w[0]; hi.value = w[1];
        paint(svg, d, w[0], w[1]);
        var lab = bar.querySelector(".rlabel");
        if (lab) lab.textContent = d.dates[w[0]] + " ~ " + d.dates[w[1]] +
          "（" + (w[1] - w[0] + 1) + " 天）";
      }
      lo.addEventListener("input", function () { upd("lo"); });
      hi.addEventListener("input", function () { upd("hi"); });
      var all = bar.querySelector(".all");
      if (all) all.addEventListener("click", function () {
        lo.value = 0; hi.value = d.n - 1; upd("all");
      });
      var yr = bar.querySelector(".yr");
      if (yr) yr.addEventListener("click", function () {
        lo.value = Math.max(0, d.n - 251); hi.value = d.n - 1; upd("yr");
      });
      upd("init");
    });
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () { bind(document); });
    } else { bind(document); }
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { clampWindow: clampWindow, xAt: xAt, visible: visible, segSpan: segSpan };
  }
  global.ChartLayout = { clampWindow: clampWindow, xAt: xAt, visible: visible, segSpan: segSpan };
})(typeof window !== "undefined" ? window : globalThis);
