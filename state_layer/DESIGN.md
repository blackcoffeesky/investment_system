# state_layer 设计文档

> 现状文档（2026-11-28 整体重写）。历史演进集中在最后一节，正文只讲现在。
> 文中所有数字都是**实测**（`python -m state_layer.cli fit` 之后从 `saved_models/` 读出来的）。

---

## 1. 一句话定位

把一条价格序列切成**少数几个可解释的状态**（下跌/震荡/上涨、低波/中波/高波……），
按资产给出**一个或多个分组**，供页面观察、供下游（causual）消费。

它不预测涨跌，不评价好坏，也不做因果推断——那些分别是 causual 与 metrics 的事。

## 2. 分层：模型层 vs 资产层

```
state_layer/
├── model/                  统计模型（**不许知道"组"，不许知道资产**）
│   ├── base.py             Model 协议 + ModelOutput + to_numpy + (label 指纹在别处)
│   ├── hmm/                Gaussian HMM（价格/波动都能用；黄金用它）
│   │   ├── fit.py          RegimeFit：全样本拟合 + walk-forward 逐日解码
│   │   └── params.py       dict → hmmlearn 对象（不碰盘）
│   └── thmm/               Student-t HMM（价格维度的主选）
│       ├── numerics.py     t 对数密度 / 前向过滤 / 前向-后向 / Cholesky 修复
│       ├── fit.py          t-EM（KMeans 初值、返回 hist/iters）
│       └── __init__.py     StudentTHMM（fit / predict / 序列化）
├── state_assets/           资产装配（**产出分组**）
│   ├── base.py             GroupResult + Estimator 协议 + label_fingerprint
│   ├── labels.py           order_labeler（按训练画像序位命名）
│   ├── ashare/             config / features / estimator
│   └── gold/               config / features / estimator / labels（K=4 命名）
├── store.py                v2 产物读写（spec.json / groups.csv / params.pkl）
├── profile.py              状态画像（核心计算，port 用）
├── port.py                 对外唯一入口
├── cli.py                  装配点 + 唯一写入口（fit_asset）
├── metrics.py              状态分离度（只作参考，不作门槛）
└── web/
    ├── routes.py           /state 页面（装配点）
    ├── combine.py          九宫格组合（纯函数，唯一实现）
    └── templates/diagnose.html
```

**判据：模型层不该知道有组。** 模型只认"特征矩阵 + K"；"这个资产出几组、每组什么语义"是资产层的事。
反过来，资产层不自己算统计——它调用模型、拿编号、再用自己的词表命名。

## 3. 协议

### 3.1 `Model`（`model/base.py`）

```python
class Model(ABC):
    def fit(self, features: pd.DataFrame, *, labeler=None) -> None   # 结果放 states_/labels_/proba_/last_window
    def predict(self, features: pd.DataFrame) -> ModelOutput         # 只用传入的行，不重训
    def state_params(self) -> dict | None                            # 纯数组，供 metrics
    def to_dict(self) / from_dict(cls, d)                            # JSON 可序列化
    def valid_index(self, features) -> pd.Index                      # **只用于对齐**
```

- `labeler(raw_means_df) -> {编号: 标签}` 由**资产注入**：模型不知道"下跌"是什么。
- **不得前视**：`predict` 只用传进来的行；t-HMM 只用过滤概率（不允许平滑）。
- `valid_index` 的语义是"我能吃哪些行"，调用方用它取各组日期交集；与已退役的链式认领无关。

### 3.2 `Estimator` / `GroupResult`（`state_assets/base.py`）

```python
@dataclass
class GroupResult:
    name, title, k
    states: pd.Series            # 编号（int）
    labels: dict[int, str]       # 编号 → 稳定标签（随产物落盘）
    label_values: pd.Series|None # 逐日**权威**标签（walk-forward 每段映射不同，必须逐日记）
    proba: pd.DataFrame|None
    features: tuple[str, ...]
    profile, usable, fit_id, window, warmup_bars

class Estimator(ABC):
    asset: str
    @classmethod load(cls, asset) -> Estimator
    def fit(panel) -> dict[str, GroupResult]
    def predict(panel) -> dict[str, GroupResult]
    def spec() -> dict            # primary / fit_date / groups{...}
    def to_params() -> dict       # {group: {model, window}}
    @property warmup_bars -> int
```

- **组清单不在协议里**：`fit()`/`predict()` 返回的 dict 的 key 就是组集合；顺序由 `config.GROUPS_ENABLED` 决定。
- `primary` 在 `config`（A 股 `short_price`、黄金 `raw`），下游只读它。

### 3.3 两条签名契约

`label_fingerprint(labels)` = sha1(「日期:标签」逐行) 前 12 位。**只由 primary 组的标签决定。**

| 变更 | 签名 | 后果 |
|---|---|---|
| 加分组（`GROUPS_ENABLED` 加名字） | **不变** | causual 无感（2026-11-22 实测：1 组→3 组，`b7f0879d7c3d` 前后一致） |
| 改特征 / 改窗口参数 / 改数据 → 标签变 | **必变** | **必须重估边快照**，否则卡片被守卫拒绝 |

## 4. 当前口径

窗口：`min_train=1000 / refit_every=125 / window=1000`（A 股，`config.WALK_FORWARD`）。
黄金钉死 `min_train=500 / refit_every=60 / window=250 / seed=42 / k=4`（行为与重构前逐位一致）。

### A 股（3 组，primary = `short_price`）

| 组 | 模型 | K | 特征 | 标签 | 天数 | 区间 | 占比 |
|---|---|---|---|---|---|---|---|
| `short_price` | Student-t ν=5 | 3 | ret_5 / ret_10 / er_10 / snr_10 | trend_down / range / trend_up（下跌/震荡/上涨） | 2502 | 2016-08-02 ~ 2026-11-23 | .463 / .297 / .240 |
| `short_vol` | Gaussian | 3 | rv_5 / atr_5 / gk_10 / abs_ret_5 | low / normal / high（低波/中波/高波） | 2508 | 2016-07-25 ~ 2026-11-23 | .522 / .374 / .105 |
| `long_price` | Student-t ν=5 | 2 | ret_60 / ret_120 / er_60 / er_120 / snr_60 | trend_down / trend_up | 2392 | 2017-01-13 ~ 2026-11-23 | .523 / .477 |

签名：`short_price 6ff7be8101a8` · `short_vol 2f08f3a83016` · `long_price ef80fd8af4f5`。
预热（交易日）：10 / 5 / 120 → `needed_bars = 120`。

### 黄金（1 组）

`raw`，Gaussian，K=4，标签 trend_down / range / trend_up / distribution，4074 天
（2010-02-26 ~ 2026-11-23），占比 .265 / .278 / .231 / .227，签名 `861783a55adc`。

### 组合（九宫格）

`web/combine.py::grid(price, vol)`：返回 `"i-j"`（价格档-波动档），**index = 两条序列的交集**。
`routes` 再用 `grid_legend()` 翻成 `下跌 - 低波` 作为 hover 与配色 key。**这是组合的唯一实现。**

## 5. 产物

```
state_layer/saved_models/<asset>/
├── spec.json    {schema:"estimator-v2", asset, primary, fit_date,
│                 groups:{name:{title,k,model,features,labels,fit_id,
│                               label_signature,fit_date,refit_dates,usable}}}
├── groups.csv   长表 date,group,state,label,p_max   ← **只放拟合产物**
└── params.pkl   {schema, groups:{name:{model,window}}}
```

- `fit_date` = 该组标签的**最后一个数据日**（不是今天）；顶层 = primary 组的。
- **外推不进 `groups.csv`**：存盘是"参数 + 拟合日 → 可复现的模型产物"；外推是"当下数据 + 同一参数"
  的临时推导，每天都会变。混在一起以后分不清哪些是模型给的。页面显式标注外推天数即可。
- `refit_dates` 留在 spec 里（诊断/审计用），但**页面不再画**。

## 6. 页面（`/state`）

- **服务端窗口渲染**：`/state?asset=&lo=&hi=`，两个 `<input type=range>` 在**同一条轨道**
  （绝对定位重叠，`pointer-events` 只开拇指），`onchange` 提交（松手才刷）。
  默认窗口 = 最近 251 个交易日（`_window()` 兜底：至少 20 天、非法/反向参数退回默认）。
- **没有任何前端 JS**（`/state/chart.js` 已删）。原因见 §7 的踩坑记录：
  三次"测试通过、浏览器不生效"，收益不足以继续赌。
- 图上：折线按状态染色（**点对连线**，相邻段端点相同 → 无断缝）；**训练窗口阴影带**
  （`rect.trainband`，opacity 0.12）标出"当前拟合用的是哪段训练数据"；
  阴影右侧的天数是预测（文字 `→ 预测 N 天` + 图注）。**不再画每次重估的竖虚线**。
- 下面三张画像表（每组一张，`profile.py` 计算；黄金一张）。

实测：A 股页默认窗口 **约 240 KB / 1.0 s**；全段（`lo=0&hi=2499`）**2.3 MB**；黄金页约 66 KB。

## 7. 踩过的坑（这节最值钱）

1. **t-HMM 用最后一次 refit 的映射重写整段历史**（2026-11-23）。
   症状：追加尾部 6 天数据后，`short_price` 历史标签 **2496/2496 全变**，签名也变。
   判别法：把两次标签做**交叉表**——干净的对角置换说明"状态划分没变、只是名字换了"；
   散乱才说明非确定性/前视。根因是 `self.labels_ = states_.map(最后一次 label_map)`；
   修为**逐日**用当时的映射（`labels.iloc[i] = label_map[sid]`），A/B 立刻从 2496 变 0。
   教训：`labels_` 必须与 `proba_`/`states_` 一样"逐日"，任何"最后一份映射套全段"都是错的。

2. **模板区块误删导致 CSS/JS 注入点消失（两次）**。
   第一次：`<script>` 被插到 `{% block title %}` 的 `{% endblock %}` 里 → 落进 `<title>`、**不执行**；
   第二次：删「观测矩阵」区块时把末尾的 `<style>` 与 `<script src>` 一起删了 → 两条轨道 + 无反应。
   教训：*"删一段文本"和"删一个语义区块"是两件事*，模板里 `{% endblock %}` 会撞，删完必须回头确认注入点。

3. **九宫格分支漏赋值**（2026-11-24）。重写 `_charts` 时 grid 分支丢了 `labels` 与 `colors`，
   于是它继承了上一轮 `short_vol` 的标签、颜色回退到单组调色板（hover 只剩 `low`）。
   现在有测试直接解析渲染出的 svg：9 色命中 ≥6、无 default 灰、hover 必须是中文组合。

4. **`_WARMUP` 不是特征预热**。它是"取数起点往前多取多少**日历天**，给低频列 ffill 用"。
   特征预热是另一件事，按各组的 `min_periods` 算（A 股 10/5/120），由
   `Estimator.warmup_bars` + `needed_bars()` 表达；把 120 加进 `_WARMUP` 会连带改 causual 的取数窗口。

5. **页面性能**：一次请求原来跑 15 次模型预测（每组各调一次 `extend`）+ 高斯逐日循环 → 35 秒。
   改为"整页一次 `predict`（只喂 `window+warmup+20` 行）+ 向量化前向过滤" → **约 1 秒**。

## 8. 已删除（以及为什么）

| 东西 | 为什么删 |
|---|---|
| `model/extreme/`（规则模型 + `extreme_*` 标签） | 用户决定去掉；极端行情由 t 分布厚尾 + 短期波动 `high` 档表达 |
| `MODEL_CHAIN` / `claim` / `remaining_index` / `run_infer` / `MODELS` 注册表 | 链式"依次认领日期"在新架构里没有语义；组合交给 estimator |
| `model/hmm/select.py`（BIC 选 K） | K 由 config 固定；自动选 K 会让人以为状态数随时会变 |
| `model/hmm/_ops.py` | 3 个 helper 与 `shared.utils.data_processing` 重复，另 3 个是 hmmlearn 三行包装 |
| `model/hmm/serialize.py` | 与 `store` 写同一目录同一种 pickle；剩下的函数全是死代码 |
| `align.py` | 逐窗命名对齐随新命名机制退役（`order_labeler` 在 `state_assets/labels.py`） |
| `analysis/` 包（`observations` / `stats` / `combine`） | 观测矩阵整体移除；`profile` 升为顶层核心；`combine` 归 `web/`（只有页面用） |
| `port.availability()` | 观测矩阵没了，它没有别的消费者 |
| `port.timeline()` | 图直接用 `groups.csv` + 面板，不需要另一条取数路径 |
| `fit.py`（并入 `cli.py`） | 46 行、只被 cli 用；合并后 `fit_asset` 仍在装配点 |
| `chart.js` + 两个前端测试 | 前端 JS 三次"本地测试通过、用户浏览器不生效"；改用服务端渲染 |
| `current.json` / `labels.csv` / `chain.pkl` / `hmm.pkl` | 旧格式；产物已迁到 `data/state_models_legacy/`（gitignore） |

## 9. 遗留

- **页面体积**：默认窗口 ~240 KB（4 张图 × 251 天），全段 2.3 MB。若用户仍嫌慢，
  下一步是"按图分段拉取"或"只发可见窗口"（但现在没有前端 JS，改法会动架构）。
- **`pyright` 19 条 warning**（0 error）：集中在 `_charts`/`port` 对 `dict` 值的宽泛推断
  （如 `port.py:187` 对 `.get` 的联合类型告警），属 stub 噪声，不影响运行。
- **`pyrightconfig_off.json`** 仍列着已删的 `state_layer/analysis`（根配置，未改）。
- **edges 重估**：`label_signature` 每次换数据/换窗口都会变，causual 侧要跟着重估（不归本模块）。

## 10. 历史演进（决策轨迹，别当现状读）

| 时间 | 变更 | 当时的理由 |
|---|---|---|
| 2026-11-15 | 各模块 `panel.py` 退役，统一走 `shared.adapter.get_data` | 面板拼装散在四处，随数据层改表名而漂 |
| 2026-11-16 | `port.causual_view` → `state_view` | 端口名里带下游模块名 = 替别人想问题 |
| 2026-11-16 | 删 `hmm_last.pkl` 回退 | 从没人写它；无条件覆盖链内窗口 → `fit_id` 校验永远失败 → 标签停止外推 |
| 2026-11-22 | 去 `extreme`、去模型链、加 Student-t HMM、每资产一个 `estimator.py` | 用户：极端日不再单独认领；组合交给资产 |
| 2026-11-22 | 多分组协议（`dict[str, GroupResult]`） | A 股要三组（短价/短波/长价），黄金仍一组 |
| 2026-11-22 | 加组不改签名（`label_fingerprint` 只哈希 primary） | 用户：加组只服务 state_layer 自己，causual 不变 |
| 2026-11-23 | 窗口 250/60 → 1000/125 | 用户：窗口太小导致状态漂移严重 |
| 2026-11-23 | 彻底清旧（legacy 产物/兼容分支/观测矩阵/`align`/`fit.py`） | 旧架构的路径都还在，读代码要来回切换口径 |
| 2026-11-24 | `/state` 改服务端窗口渲染，删前端 JS | 前端 JS 三次"测试通过、浏览器不生效"，收益 < 成本 |
| 2026-11-24 | 训练窗口阴影带取代 61 条重估竖线 | 用户要看"这次拟合用了哪段训练数据"，一条带子比密集竖线清楚 |
