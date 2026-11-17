# tests/ —— 按模块分类

```
tests/
├── run_tests.py      跑法（不依赖 pytest）
├── shared/           耦合层 + 纯工具：adapter 契约 / 日期对齐 / 单位原语
├── datahub/          数据层：写路径 / 派生注册表 / 窗口下推 / 命令行契约 / 新鲜度清单
├── state_layer/      状态层：特征新鲜度 / 预处理 / 模型契约
├── causual/          因果层：面板派生列 / 死列 / 卡片 / 标签词表 / latency
├── service/          框架层：状态机场景 / 端到端
└── cross/            跨模块**规则**：解耦约束 / 分层静态检查
```

## 怎么跑

```bash
python tests/run_tests.py                 # 全部，约 3 分钟
python tests/run_tests.py --fast          # 跳过 SLOW，约 16 秒
python tests/run_tests.py datahub causual # 只跑指定组
python tests/run_tests.py --list          # 看有哪些组
```

装了 pytest 之后 `pytest tests` 或 `pytest tests/datahub` 同样能跑（文件是 pytest 兼容的）。

## 两个约定

**① 新增测试放哪**

谁的功能，放谁的目录。**同时被两个模块守的规则放 `cross/`**（比如"核心代码不许取数"）。

`tests/` 顶层故意不放测试文件——`run_tests.py` 会把顶层的 `test_*.py` 归到「(未归类)」并跑，
但那是**兜底不是惯例**。

**② 超过 10 秒的加标记**

```python
SLOW = True      # 模块级，放在最后一个 import 之后
```

`--fast` 会跳过它。现在有 4 个：

| 文件 | 耗时 | 为什么慢 |
|---|---|---|
| `causual/test_card_smoke.py` | ~78s | 真跑出卡（两个资产 × 日频+月频） |
| `datahub/test_derived_registry.py` | ~60s | 每个派生量都要做窗口不变量（全量 vs 窗口各算一遍） |
| `service/test_e2e.py` | ~28s | 从空库到出卡整条链 |
| `state_layer/test_model_contract.py` | ~12s | 真 HMM 推理 |

**这 4 个占全量的 93%**（172s / 185s）。改代码时用 `--fast`，提交前跑全量。

## 为什么要这么分

之前 21 个文件平铺在 `tests/`，看不出"这个红是谁的"——改 causual 的时候跑一遍全量，
分不清哪条红是自己弄的还是别人在动。分组之后：

- 报错前缀就是组名：`[datahub/test_window_pushdown.py::...]`
- 改谁跑谁：`python tests/run_tests.py causual`
- 慢的可以单独跳
