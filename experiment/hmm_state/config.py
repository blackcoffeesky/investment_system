"""全局参数配置，所有实验超参统一在此"""

from __future__ import annotations



# 时间切分

TRAIN_FRAC = 0.7

# HMM状态数量

K_SHORT_PRICE = 3

K_SHORT_VOL = 3

K_LONG_PRICE = 2



# 滚动窗口

Z_WIN = 250

BQ = (1 / 3, 2 / 3)  # 滚动分档：低/中/高

BASIS_CLIP = 0.5     # 年化基差率截尾



# 资金条件转移参数

HORIZONS = (1, 5, 10, 20)

BLOCK_BOOT_BLOCK = 20

BOOT_REPS = 1000

MIN_N = 60

MIN_N_SEG = 25



# 模型评估硬约束

MIN_STATE_SHARE = 0.05

MIN_MEDIAN_DUR = 5

MIN_ES5_SPREAD = 1.0



# t-HMM固定自由度

NU = 5.0

MAX_ITER = 400

RTOL = 1e-7



# Wilson置信区间z=1.96 95%

WILSON_Z = 1.96

# 随机种子（bootstrap / 初始化共用）
SEED = 42
