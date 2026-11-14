"""手工数据接入层。

目录约定
--------
```
datahub/fin/intake/
├── raw/        # 原始文件（用户丢进来的 xls/csv/pdf 导出），不做任何修改
├── clean/      # 解析后的标准 CSV（列契约与库表一致），只有待写入/覆盖的行
├── parsers/    # 一源一个解析函数
├── registry.py # 源标识 → (parser, 目标表, 频率)
├── cli.py      # python -m datahub.fin.intake <source> <raw 文件>
└── stock_daily.py  # 一次性：同花顺 parquet → stock_daily / adj_factor
```

`clean/` 替代了旧的 `datahub/manual/`：它只承载**要写进库的数据**——
新增的日期，以及被源方修订过的旧日期（会覆盖库里的值）。

为什么要有这一层
----------------
手工数据的来源太杂（WGC 的 xlsx、东财的 csv、新闻里看到的一个数）。
把"解析"和"入库"分开：raw 原样留档，clean 是可审阅的中间产物，入库可重放。
"""
