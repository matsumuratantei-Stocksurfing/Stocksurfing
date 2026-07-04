#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stocksurfing 計測 — 松村式一本化戦略バックテスト (計測専用)

「5層ファネルを一本のルールに束ねたとき、高勝率・高収益になるか」を検証する。
本番ロジックには触れない。発注・証券APIは一切importしない。po_detectorを再利用。

■ 一本化ルール(すべて検出時点までのデータのみ=前向き)
  第1層 地合い : TOPIX(1306)の終値 > TOPIX200日線 の日だけエントリー可
  第2層 入口   : 銘柄が上昇トレンド(終値>SMA200 かつ SMA200が上向き)で、
                 かつ「52週新高値ブレイク(初日)」または「52週高値の3%以内」
  第4層 損切り : 初期ストップをボラ/構造で置く(構造=押し安値×0.98 / ATR×2 / ATR×3 / 固定-2%)
  第5層 出口   : +5%で半分利確→残りは建値ストップに引き上げ、SMA25割れでトレール決済
                 (=松村式の分割利確を、広いボラ損切りに載せ替えた版)。最長40営業日で時間切れ。
  1銘柄は同時に1ポジション(決済まで新規建てしない=リエントリーは決済後)。

■ 比較軸
  損切り種別(構造/ATR2/ATR3/固定-2%) × 地合いフィルター(ON/OFF)。
  高勝率(勝率)と高収益(1トレード期待値・R倍数・大勝ち率)の両面で評価。

【限界】取得は上昇相場に偏り、取引コスト/スリッページ未考慮。価格は調整済み(AdjC)。
"""
import os
import sys
import json
import time
import statistics
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_STOCKS
from po_detector import JST, fetch_days, _sma

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SLEEP_BETWEEN_CALLS = 0.3
FETCH_FROM = '2022-01-01'
W52 = 250
ATR_WIN = 20
TP_PCT = 5.0          # 半分利確ライン
MAXHOLD = 40          # 時間切れ(営業日)
NEAR_HIGH_PCT = 3.0   # 52週高値の何%以内を「新高値圏」とするか
TICK_START = 250      # 52週高値/SMA200の助走

ETF_UNIVERSE = [
    {'code': '213A', 'name': '半導体ETF-A'}, {'code': '221A', 'name': '半導体ETF-B'},
    {'code': '200A', 'name': '半導体ETF-C'}, {'code': '282A', 'name': '半導体ETF-D'},
    {'code': '2243', 'name': '半導体ETF-E'}, {'code': '513A', 'name': '防衛テックETF'},
    {'code': '1619', 'name': '建設・資材ETF'},
]
TOPIX_CODE = '1306'
STOP_KINDS = ['struct', 'atr2', 'atr3', 'fix2']


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def _atr_series(highs, lows, closes, win=ATR_WIN):
    n = len(closes)
    tr = [None] * n
    for i in range(n):
        if highs[i] is None or lows[i] is None:
            continue
        if i == 0 or closes[i - 1] is None:
            tr[i] = highs[i] - lows[i]
        else:
            pc = closes[i - 1]
            tr[i] = max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc))
    atr = [None] * n
    for i in range(n):
        if i + 1 < win:
            continue
        seg = tr[i - win + 1:i + 1]
        if any(v is None for v in seg):
            continue
        atr[i] = sum(seg) / win
    return atr


def initial_stop(kind, entry, swing_low, atr):
    if kind == 'struct':
        return swing_low * 0.98 if swing_low else None
    if kind == 'atr2':
        return entry - 2 * atr if atr else None
    if kind == 'atr3':
        return entry - 3 * atr if atr else None
    if kind == 'fix2':
        return entry * 0.98
    return None


def sim_trade(i, closes, highs, lows, sma25, stop0):
    """一本化の出口エンジン。i=エントリー(当日終値約定)。
    戻り: (exit_i, net_return_pct, hold_days, reason, hit_tp)。stop0が無効ならNone。"""
    entry = closes[i]
    if entry is None or entry <= 0 or stop0 is None:
        return None
    n = len(closes)
    stop = stop0
    half_locked = False
    realized = 0.0
    tp_price = entry * (1 + TP_PCT / 100)
    for k in range(1, MAXHOLD + 1):
        j = i + k
        if j >= n:
            break
        lo, hi, cl = lows[j], highs[j], closes[j]
        # 1) ストップ(保守的に先に判定)
        if lo is not None and lo <= stop:
            rem = 0.5 if half_locked else 1.0
            realized += rem * (stop - entry) / entry * 100
            return j, round(realized, 3), k, ('stop_after_half' if half_locked else 'stop'), half_locked
        # 2) +5%半利確(初回のみ)→建値へストップ引き上げ
        if not half_locked and hi is not None and hi >= tp_price:
            realized += 0.5 * TP_PCT
            half_locked = True
            stop = max(stop, entry)
        # 3) トレール(SMA25割れで決済)
        s25 = sma25[j]
        if cl is not None and s25 is not None and cl < s25:
            rem = 0.5 if half_locked else 1.0
            realized += rem * (cl - entry) / entry * 100
            return j, round(realized, 3), k, ('trail_after_half' if half_locked else 'trail'), half_locked
    # 時間切れ
    j = min(i + MAXHOLD, n - 1)
    cl = closes[j]
    if cl is not None:
        rem = 0.5 if half_locked else 1.0
        realized += rem * (cl - entry) / entry * 100
        return j, round(realized, 3), j - i, ('time_after_half' if half_locked else 'time'), half_locked
    return None


def run_strategy(days, regime_up, stop_kind, use_regime):
    """1銘柄を一本化ルールで通しでトレードし、確定トレードのリストを返す。"""
    n = len(days)
    closes = [d['c'] for d in days]
    highs = [d['h'] for d in days]
    lows = [d['l'] for d in days]
    sma25 = [_sma(closes, i, 25) for i in range(n)]
    sma200 = [_sma(closes, i, 200) for i in range(n)]
    atr = _atr_series(highs, lows, closes)

    trades = []
    prev_new52 = False
    i = TICK_START
    while i < n:
        c = closes[i]
        if c is None or days[i]['adjFactor'] != 1.0:
            i += 1
            continue
        prior_seg = [h for h in highs[max(0, i - W52):i] if h is not None]
        prior52 = max(prior_seg) if len(prior_seg) >= 200 else None
        hi = highs[i]
        is_new52 = bool(prior52 is not None and hi is not None and hi >= prior52)
        near_high = bool(prior52 and (prior52 - c) / prior52 * 100 <= NEAR_HIGH_PCT)
        breakout = is_new52 and not prev_new52

        s200, s200p = sma200[i], (sma200[i - 20] if i >= 20 else None)
        stock_up = bool(s200 is not None and c > s200 and s200p is not None and s200 > s200p)
        regime = (regime_up.get(days[i]['date'], False) if use_regime else True)

        entry_ok = stock_up and regime and (breakout or near_high)
        prev_new52 = is_new52

        if not entry_ok:
            i += 1
            continue

        swing_seg = [l for l in lows[max(0, i - 9):i + 1] if l is not None]
        swing_low = min(swing_seg) if swing_seg else None
        stop0 = initial_stop(stop_kind, c, swing_low, atr[i])
        res = sim_trade(i, closes, highs, lows, sma25, stop0)
        if not res:
            i += 1
            continue
        exit_i, net, hold, reason, hit_tp = res
        trades.append({'entry_date': days[i]['date'], 'net': net, 'hold': hold,
                       'reason': reason, 'hit_tp': hit_tp, 'breakout': breakout})
        i = exit_i + 1  # 決済後にリエントリー可
    return trades


def summarize(trades):
    if not trades:
        return {'n': 0}
    nets = [t['net'] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    avg_win = _mean(wins)
    avg_loss = _mean([abs(x) for x in losses])
    # 資産曲線(等額・複利無視の累積)最大DD
    eq = 0.0; peak = 0.0; dd = 0.0
    for x in nets:
        eq += x; peak = max(peak, eq); dd = min(dd, eq - peak)
    return {
        'n': len(trades),
        'win_rate': round(len(wins) / len(nets) * 100, 1),
        'expectancy': _mean(nets),
        'median': _median(nets),
        'avg_win': avg_win,
        'avg_loss': (round(-avg_loss, 3) if avg_loss is not None else None),
        'r_multiple': (round(avg_win / avg_loss, 2) if (avg_win and avg_loss) else None),
        'bigwin_pct(>=8%)': round(sum(1 for x in nets if x >= 8) / len(nets) * 100, 1),
        'total_R_sum': round(sum(nets), 1),
        'avg_hold': _mean([t['hold'] for t in trades]),
        'max_drawdown_Rsum': round(dd, 1),
        'tp_hit_pct': round(sum(1 for t in trades if t['hit_tp']) / len(trades) * 100, 1),
    }


def main():
    today = datetime.now(JST).date().strftime('%Y-%m-%d')
    print("=" * 64)
    print("  Stocksurfing 松村式一本化戦略バックテスト (計測専用)")
    print(f"  実行: {datetime.now(JST).isoformat()}  取得: {FETCH_FROM}〜{today}")
    print("=" * 64)

    # 地合い: TOPIX 200日線
    topix = fetch_days(TOPIX_CODE, frm=FETCH_FROM, to=today)
    time.sleep(SLEEP_BETWEEN_CALLS)
    tcl = [d['c'] for d in topix]
    tsma = [_sma(tcl, i, 200) for i in range(len(topix))]
    regime_up = {}
    for i, d in enumerate(topix):
        regime_up[d['date']] = bool(d['c'] is not None and tsma[i] is not None and d['c'] > tsma[i])

    universe = ([('個別', s['code'], s['name']) for s in DEFAULT_STOCKS]
                + [('ETF', e['code'], e['name']) for e in ETF_UNIVERSE])

    series = {}
    data_status = []
    for group, code, name in universe:
        days = fetch_days(code, frm=FETCH_FROM, to=today)
        time.sleep(SLEEP_BETWEEN_CALLS)
        usable = [d for d in days if d['c'] is not None]
        if len(usable) < (200 + 30):
            data_status.append({'group': group, 'code': code, 'name': name,
                                'days': len(usable), 'status': 'データ不足/なし'})
            continue
        series[code] = (group, name, days)
        data_status.append({'group': group, 'code': code, 'name': name,
                            'days': len(usable), 'status': 'OK'})

    variants = {}
    for use_regime in (True, False):
        for kind in STOP_KINDS:
            all_trades = []
            for code, (group, name, days) in series.items():
                all_trades += run_strategy(days, regime_up, kind, use_regime)
            key = f"{'regimeON' if use_regime else 'regimeOFF'}/{kind}"
            variants[key] = summarize(all_trades)
            print(f"  {key:<22} n={variants[key].get('n')} 勝率={variants[key].get('win_rate')} "
                  f"期待値={variants[key].get('expectancy')} R={variants[key].get('r_multiple')} "
                  f"総R={variants[key].get('total_R_sum')} maxDD={variants[key].get('max_drawdown_Rsum')}")

    # 所見
    def g(key, m):
        return variants.get(key, {}).get(m)
    verdict = []
    best = max([k for k in variants if variants[k].get('n', 0) >= 30],
               key=lambda k: (variants[k].get('expectancy') or -99), default=None)
    verdict.append(
        "(1) 損切りの効き(地合いON): "
        + " / ".join(f"{kd} 勝率{g(f'regimeON/{kd}','win_rate')}%・期待値{g(f'regimeON/{kd}','expectancy')}%・R{g(f'regimeON/{kd}','r_multiple')}"
                     for kd in STOP_KINDS)
    )
    verdict.append(
        f"(2) 固定-2% vs ボラ損切り(地合いON): 固定-2%={g('regimeON/fix2','win_rate')}%/期待値{g('regimeON/fix2','expectancy')}% "
        f"vs 構造={g('regimeON/struct','win_rate')}%/期待値{g('regimeON/struct','expectancy')}% "
        f"vs ATR2={g('regimeON/atr2','win_rate')}%/期待値{g('regimeON/atr2','expectancy')}% "
        f"→ {'ボラ損切りが優位(固定-2%は不利)' if (g('regimeON/struct','expectancy') or -9) > (g('regimeON/fix2','expectancy') or 9) else '差は小'}"
    )
    verdict.append(
        f"(3) 地合いフィルターの価値(構造損切り): ON 期待値{g('regimeON/struct','expectancy')}%/maxDD{g('regimeON/struct','max_drawdown_Rsum')} "
        f"vs OFF 期待値{g('regimeOFF/struct','expectancy')}%/maxDD{g('regimeOFF/struct','max_drawdown_Rsum')} "
        f"→ {'地合いフィルターでDD縮小/質向上' if (g('regimeON/struct','max_drawdown_Rsum') or -999) > (g('regimeOFF/struct','max_drawdown_Rsum') or -1000) else '効果小'}"
    )
    if best:
        b = variants[best]
        verdict.append(
            f"(4) 最良構成(期待値基準)= {best}: 勝率{b['win_rate']}% 期待値{b['expectancy']}% R{b['r_multiple']} "
            f"大勝ち率(>=8%){b['bigwin_pct(>=8%)']}% 平均保有{b['avg_hold']}日 総R{b['total_R_sum']} maxDD{b['max_drawdown_Rsum']} "
            f"→ アプリ「一本化」タブの初期パラメータ候補"
        )
    verdict.append(
        "(5) 実装方針: 勝率と期待値・DDのバランス最良の損切りを採用し、+5%半利確→残りSMA25トレールで大勝ちも拾う。"
        " 本標本は上昇相場偏り・コスト未考慮のため、採用は観測モードでの前向き実測と併走が前提。"
    )

    report = {
        'generatedAt': datetime.now(JST).isoformat(),
        'note': '計測専用・本番非変更。価格は調整済み(AdjC)。取引コスト/スリッページ未考慮。上昇相場偏り。',
        'strategy': '松村式一本化: 地合い(TOPIX>200MA)×上昇トレンド新高値/近接入口×ボラ損切り×+5%半利確→SMA25トレール',
        'params': {'fetchFrom': FETCH_FROM, 'fetchTo': today, 'W52': W52, 'atrWin': ATR_WIN,
                   'tpPct': TP_PCT, 'maxHold': MAXHOLD, 'nearHighPct': NEAR_HIGH_PCT},
        'universeStatus': data_status,
        'variants': variants,
        'bestByExpectancy': best,
        'verdict': verdict,
    }
    with open(os.path.join(SCRIPT_DIR, 'unified_backtest_report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)

    print("\n  所見(計測者):")
    for line in verdict:
        print("  " + line)
    print("\n  ✓ unified_backtest_report.json 書き出し完了")


if __name__ == '__main__':
    main()
