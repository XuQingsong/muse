"""逐日量能升级层（免费，行情自带成交量，point-in-time）。
每只票每日输出：
  vol_ratio   20日均量/60日均量（参与度）
  up_share    20日上涨日成交量占比%
  accdist     20日 涨日量/跌日量
  dist_days   25日内派发日数（跌>=0.2%且量大于昨日，IBD口径）
  obv_trend   OBV 20日斜率（以20日均量归一化）
  price_obv_div 价格20日涨而OBV走平/走弱=顶背离标记（-1/0/1，1=底背离吸筹）
  breakout    当日创20日新高且量>=1.5倍20日均量
  climax      当日量>=2.5倍20日均量
  vol_score   0-100 合成（涨日量结构+参与度+OBV，派发日扣分、>=5封顶45）
输出 volume_factors_daily.csv（2023年起全日度序列，供回测）+ 打印最新快照。
未验证：分数为结构性描述，未做收益预测有效性验证前不作买卖依据。
"""
import sys
import numpy as np
import pandas as pd
sys.path.insert(0, ".")
from factor_scorecard_v2 import get_history
from paper_trade_2026 import TICKERS

TICKS = TICKERS + ["MU", "GOOGL"]

def interp(x, pts):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return float(np.interp(x, xs, ys))

def volume_daily(df):
    c, v = df["close"], df["volume"]
    ret = c.pct_change()
    ma20, ma60 = v.rolling(20).mean(), v.rolling(60).mean()
    out = pd.DataFrame(index=df.index)
    out["vol_ratio"] = ma20 / ma60
    up_v = v.where(ret > 0, 0.0); dn_v = v.where(ret < 0, 0.0)
    out["up_share"] = up_v.rolling(20).sum() / v.rolling(20).sum() * 100
    out["accdist"] = up_v.rolling(20).sum() / dn_v.rolling(20).sum().replace(0, np.nan)
    is_dist = (ret <= -0.002) & (v > v.shift(1))
    out["dist_days"] = is_dist.rolling(25).sum()
    obv = (np.sign(ret.fillna(0)) * v).cumsum()
    out["obv_trend"] = (obv - obv.shift(20)) / (ma20 * 20)
    p_chg = c / c.shift(20) - 1
    o_chg = (obv - obv.shift(20)) / (v.rolling(60).mean() * 20).replace(0, np.nan)
    out["price_obv_div"] = np.where((p_chg > 0.03) & (o_chg < 0), -1,
                             np.where((p_chg < -0.03) & (o_chg > 0), 1, 0))
    prior_high = c.shift(1).rolling(20).max()
    out["breakout"] = (c >= prior_high) & (v >= 1.5 * ma20)
    out["climax"] = v >= 2.5 * ma20
    # 合成分（阈值均为暂定先验，未验证）
    s_acc = out["accdist"].apply(lambda x: interp(x, [(0.5, 15), (0.8, 38), (1.0, 55), (1.4, 78), (2.0, 95)]) if pd.notna(x) else 50)
    s_up = out["up_share"].apply(lambda x: interp(x, [(30, 15), (45, 38), (55, 58), (65, 80), (75, 95)]) if pd.notna(x) else 50)
    s_part = out["vol_ratio"].apply(lambda x: interp(x, [(0.6, 30), (0.85, 45), (1.0, 58), (1.3, 78), (1.8, 92)]) if pd.notna(x) else 50)
    s_obv = out["obv_trend"].apply(lambda x: interp(x, [(-0.6, 12), (-0.2, 35), (0, 52), (0.25, 75), (0.6, 95)]) if pd.notna(x) else 50)
    score = 0.30 * s_acc + 0.25 * s_up + 0.20 * s_part + 0.25 * s_obv
    score = score - 6 * (out["dist_days"].fillna(0) - 2).clip(lower=0)
    score = score + out["breakout"].fillna(False) * 5
    score = np.where(out["dist_days"].fillna(0) >= 5, np.minimum(score, 45), score)
    out["vol_score"] = score.clip(0, 100)
    return out

frames = []
for t in TICKS:
    f = volume_daily(get_history(t))
    f["ticker"] = t
    frames.append(f)
    last = f.dropna(subset=["vol_score"]).iloc[-1]
    print(f"{t}: 量能分{last['vol_score']:.0f} 量比{last['vol_ratio']:.2f} 涨日量占比{last['up_share']:.0f}% "
          f"涨跌量比{last['accdist']:.2f} 派发日{last['dist_days']:.0f} 背离{last['price_obv_div']:.0f} "
          f"突破{bool(last['breakout'])} 高潮量{bool(last['climax'])}", flush=True)

allf = pd.concat(frames).reset_index()
allf.to_csv("volume_factors_daily.csv", index=False)
print("已写入 volume_factors_daily.csv 行数", len(allf))
