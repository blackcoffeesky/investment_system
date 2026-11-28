# state_layer —— 状态层

把价格序列切成少数几个可解释的状态，按资产给出一组或多组结果。
设计取舍与踩坑记录见 [DESIGN.md](DESIGN.md)。

---

## 快速开始

```bash
# 拟合 + 落盘（唯一的写入口；改特征/改窗口/数据更新后跑）
python -m state_layer.cli fit --asset ashare
python -m state_layer.cli fit --asset gold

# `regime` 是旧命令名，等价
python -m state_layer.cli regime --asset ashare
```

也可以从代码里调用（与 CLI 同一个函数）：

```python
from state_layer.cli import fit_asset
fit_asset("ashare")          # panel=None 时内部从 shared.adapter 取数
```

**跑完 `fit` 之后**：`label_signature` 可能变了 → 下游（causual）的边快照要重估，
否则卡片会被签名守卫拒绝：

```bash
python -m causual.causual_core.cli edges --asset ashare
```

## 产物在哪

```
state_layer/saved_models/<asset>/
├── spec.json    分组元信息（primary / fit_date / 每组 k·特征·标签·签名·refit_dates）
├── groups.csv   逐日标签长表：date,group,state,label,p_max
└── params.pkl   每组模型参数 + 最后窗口（在线推断用）
```

- 旧格式（`current.json` / `labels.csv` / `chain.pkl` / `hmm.pkl`）已退役，
  历史文件搬到 `data/state_models_legacy/`（gitignore）。
- **外推的标签不写进 `groups.csv`**：存盘只放"拟合出来的"；页面会临时补到面板最后一天并标注。
- 产物不进 git，部署时要单独搬。

## 页面

```
/state?asset=ashare           # A 股：4 张图 + 3 张画像表
/state?asset=gold             # 黄金：1 张图 + 1 张表
/state?asset=ashare&lo=0&hi=2499   # 指定窗口（索引，服务端夹紧）
```

- 默认窗口 = 最近 **251** 个交易日；两个滑块在同一条轨道上，**松手才提交**（`onchange`）。
- 没有前端 JS：脚本版试过三次（本地测试通过、浏览器不生效），已改为服务端渲染。
- 图上：折线按状态染色；**阴影带 = 当前拟合所用训练窗口**；阴影右侧 = 预测（标注 `→ 预测 N 天`）。

## 对外能力（`port.py`）

```python
from state_layer.port import get_port
p = get_port()

p.labels("ashare")                 # primary 组的逐日标签（Series）
p.labels("ashare", "short_vol")    # 指定组
p.spec("ashare")                   # spec.json
p.states("ashare", start="2026-01-01")
p.current_state("ashare", panel)   # 当日状态 + 后验（panel 由装配点传入）
p.extend("ashare", panel)          # 标签补到面板最后一天（外推，不入盘）
p.transition("ashare", panel)      # 经验转移/停留（从历史标签估）
p.profile("ashare", panel)         # 画像表（每组一张由页面调）
p.state_view("ashare", panel)      # 页面/下游要的一整包（labels/version/line…）
p.required_tables("ashare")        # 装配清单
```

`panel` 一律由**装配点**（`cli.py` / `web/routes.py` / service）用 `get_data` 取好再传进来；
核心代码不取数。

## 加一个分组

只在 config 里加 key，**不改协议、不改签名算法**：

```python
# state_assets/ashare/config.py
GROUPS_ENABLED = ("short_price", "short_vol", "long_price", "my_group")
K["my_group"] = 2
GROUP_MODEL["my_group"] = "thmm"          # 或 "hmm"
GROUP_FEATURES["my_group"] = ("ret_5", "rv_5")
GROUP_LABELS["my_group"] = ("weak", "strong")
ORDER_COL["my_group"] = "ret_5"
GROUP_TITLE["my_group"] = "自定义"
WARMUP_BARS["my_group"] = 10
```

再在 `features.py` 里加一个构造函数，并让 `build_groups()` 认识它。
`primary` 不变 → **`label_signature` 不变** → 下游无感（这条有测试守着）。

## 改窗口参数

```python
# state_assets/ashare/config.py
WALK_FORWARD = {"min_train": 1000, "refit_every": 125, "window": 1000}
```

黄金的参数钉在 `state_assets/gold/estimator.py::GOLD_WALK_FORWARD`（改它会让黄金行为变化，
属另一个决定，不是重构）。改完重跑 `fit`；**签名会变，记得重估 edges**。

## 测试与检查

```bash
python tests/run_tests.py state_layer     # 本模块
python tests/run_tests.py state_layer cross service
python -m pyright --project pyrightconfig_off.json    # 0 error
```

## 已知限制

- 页面默认 ~240 KB；全段（`lo=0&hi=2499`）2.3 MB。
- `pyright` 有 19 条 warning（0 error），是 pandas/联合类型的推断噪声。
- 外推只到面板最后一天，不做多步预测。
