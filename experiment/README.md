# experiment —— 试验场

临时想法、理论验证、参数研究都放这里。**不接进生产**：service、网页、卡片都不依赖这个目录。

规则只有一条：可以 import 任意模块（`datahub`、`state_layer`、`causual`、`shared`），
但**反过来不行**——生产代码不许 import `experiment`。

```
experiment/
├── k_study.py        K 值研究：不同状态数下的画像 / 分离度 / BIC / 分割图
├── k_study.ipynb     同一套逻辑的 notebook 版（方便边看边改）
└── out/              产物：profile_*.csv、segments_*.png、summary_*.csv
```

## 跑法

```bash
python -m experiment.k_study --asset ashare --k 2,3,4,5,6
```

或在 VS Code 里打开 `k_study.ipynb` 逐格跑。

## k_study 看什么

| 指标 | 含义 | 怎么用 |
|---|---|---|
| `separation` | 状态间 Bhattacharyya 最小距离 | 越大越分得开；< 0.4 说明两个状态在观测上没区别 |
| `BIC` | 惩罚后的似然 | 只用来横向比同一批特征下的 K；它永远偏好更大的 K |
| `min_share` | 最小的状态占比 | 低于 5% 基本是"给异常点开的小灶" |
| `avg_duration` | 平均持续天数 | 太短（<3 天）说明状态在抖动 |
| `stay_prob` | 留在本状态的概率 | 接近 0 说明这个状态是个"过渡态"，没有实际含义 |

分割图（`segments_*.png`）：价格曲线 + 按状态上色的背景，直接看市场被切成什么样。

> 注意：这里的拟合是**全样本**的（研究用，快），不做 walk-forward。
> 所以图中的状态划分带未来信息，**不能**拿来当实盘标签。
