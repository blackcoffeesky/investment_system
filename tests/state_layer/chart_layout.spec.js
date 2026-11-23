/* 纯函数等价测试：页面用的是同一份 `state_layer/web/static/chart.js`。
 * 跑法： node tests/state_layer/chart_layout.spec.js
 */
const assert = require("assert");
const L = require("../../state_layer/web/static/chart.js");

// ---- ① clampWindow：反向夹紧 + minGap
{
  const [lo, hi] = L.clampWindow(900, 100, 2496, 20);
  assert.ok(hi - lo >= 20, `窗口太窄：${lo}-${hi}`);
  assert.ok(lo <= hi, `起点在终点右边：${lo}-${hi}`);
}
{
  const [lo, hi] = L.clampWindow(-5, 99999, 2496, 20);
  assert.strictEqual(lo, 0);
  assert.strictEqual(hi, 2495);
}
{
  // 拖到最右：hi 贴边后 lo 被顶回来
  const [lo, hi] = L.clampWindow(2490, 2495, 2496, 20);
  assert.strictEqual(hi, 2495);
  assert.strictEqual(lo, 2475, `lo 应被夹到 ${hi - 20}，实际 ${lo}`);
}

// ---- ② xAt：窗口两端映射到绘图区两端
{
  const x0 = L.xAt(500, 500, 800, 44, 846);
  const x1 = L.xAt(800, 500, 800, 44, 846);
  assert.ok(Math.abs(x0 - 44) < 1e-9, `左端 ${x0}`);
  assert.ok(Math.abs(x1 - 890) < 1e-9, `右端 ${x1}`);
}

// ---- ③ segSpan：段与窗口的交集；段跨窗口时被截断
{
  assert.deepStrictEqual(L.segSpan(10, 20, 15, 30), [15, 20]);
  assert.deepStrictEqual(L.segSpan(10, 20, 0, 99), [10, 20]);
  assert.strictEqual(L.segSpan(10, 20, 21, 30), null, "窗口外的段应隐藏");
}

// ---- ④ 行为断言：窗口变小 → 可见段数变少（等价于 DOM 上的可见点数变化）
{
  const n = 2496;
  // 造 30 段（每段 ~83 天），模拟真实 chart 的 seg 列表
  const segs = [];
  for (let a = 0; a < n - 1; a += 83) segs.push([a, Math.min(a + 82, n - 1)]);

  const countVisible = (lo, hi) =>
    segs.filter(([a, b]) => L.segSpan(a, b, lo, hi) !== null).length;

  const year = countVisible(n - 251, n - 1);
  const all = countVisible(0, n - 1);
  const half = countVisible(1200, 1800);
  assert.ok(all > year, `全段应比一年可见段多：${all} vs ${year}`);
  assert.ok(year > 0, "最近一年至少要有一段");
  assert.ok(half < all && half > 0, "中段窗口应介于两者之间");
  console.log("visible segs  all=%d year=%d mid=%d", all, year, half);
}

// ---- ⑤ 相邻点对连线：段数 = 点数-1，且 x 首尾相接
{
  const n = 60, padL = 44, plotW = 846;
  const xs = [];
  for (let i = 0; i < n - 1; i++) {
    xs.push([L.xAt(i, 0, n - 1, padL, plotW), L.xAt(i + 1, 0, n - 1, padL, plotW)]);
  }
  assert.strictEqual(xs.length, n - 1, "段数应为点数-1");
  for (let i = 0; i < xs.length - 1; i++) {
    assert.ok(Math.abs(xs[i][1] - xs[i + 1][0]) < 1e-9,
      `相邻点对 x 断开：${xs[i][1]} vs ${xs[i + 1][0]}`);
  }
  console.log("pair segs %d, continuous OK", xs.length);
}

console.log("chart_layout OK");
