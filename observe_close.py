#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
observe_close.py — 観測モード 決済ジョブ (仮想決済 + baseline + 集計)

夜のジョブ(observe.yml)で observe_detect の後に実行。open レコードを走査し、
po_detector.close_trade(=Phase1.5確定版の構造損切り)で仮想決済を判定する。
決済時は既存レコードに close 項目を**追記**し status を closed にするのみ。
検出時フィールドには一切触れない(不可変性)。

各レコードに dumb baseline(同銘柄・同保有日数の無条件売買)と TOPIX比 を併記する。
格上げ判定はベースライン比超過で行うため(Monster Scout と同じKPI思想)。

集計(meta.summary)と格上げ達成状況(meta.promotion)は、
**現行 logic_version のクローズ済みレコードのみ**で再計算する(時計リセット規約)。
"""
import os
import sys
import json
import time
import statistics
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from po_detector import JST, LOGIC_VERSION, fetch_days, close_trade
from observe_criteria import evaluate_promotion, PROMOTION_CRITERIA_V1

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OBS_PATH = os.path.join(SCRIPT_DIR, 'observations.json')
SLEEP_BETWEEN_CALLS = 0.3
TOPIX_ETF = '1306'  # TOPIX比較のベースライン(依頼: TOPIXリターン)


def load_obs():
    if not os.path.exists(OBS_PATH):
        return {'meta': {}, 'records': []}
    with open(OBS_PATH, 'r', encoding='utf-8') as f:
        d = json.load(f)
    if 'records' not in d:
        d = {'meta': {}, 'records': d if isinstance(d, list) else []}
    return d


def _days_map(days):
    """date -> day dict の辞書と、日付昇順リストを返す。"""
    m = {d['date']: d for d in days}
    order = [d['date'] for d in days]
    return m, order


def close_open_records(obs):
    """open レコードを構造損切りで仮想決済し、baseline を併記する。"""
    open_recs = [r for r in obs['records'] if r.get('status') == 'open']
    if not open_recs:
        return 0
    # 銘柄ごとに一度だけ日足取得
    codes = sorted({r['ticker'] for r in open_recs})
    series = {}
    for c in codes:
        series[c] = fetch_days(c)
        time.sleep(SLEEP_BETWEEN_CALLS)
    topix = fetch_days(TOPIX_ETF)
    tmap = {d['date']: d['c'] for d in topix}

    closed = 0
    for r in open_recs:
        days = series.get(r['ticker']) or []
        if not days:
            continue
        dmap, order = _days_map(days)
        detect_date = r['baseline_ref']['detect_date']
        if detect_date not in order:
            continue
        idx = order.index(detect_date)
        entry = r['hypothetical_entry']['price']
        sp = r['detection_context']['signal_params']
        pivot = sp['pivot_high']
        swing_low = sp['swing_low']
        # 検出翌日以降の (low, high, close)
        fut = [(days[j]['l'], days[j]['h'], days[j]['c']) for j in range(idx + 1, len(days))]
        res = close_trade(entry, pivot, swing_low, fut)
        if not res:
            continue  # まだ決着せず open 継続

        hold = res['holding_days']
        # dumb baseline: 同銘柄を同じ保有日数だけ持って無条件売却
        dumb_ret = None
        if idx + hold < len(days) and days[idx + hold]['c'] and entry:
            dumb_ret = round((days[idx + hold]['c'] - entry) / entry * 100, 3)
        # exit 実日付
        exit_date = order[idx + hold] if idx + hold < len(order) else order[-1]
        # TOPIX: 検出日→exit日 のリターン
        topix_ret = None
        t0 = tmap.get(detect_date)
        t1 = tmap.get(exit_date)
        if t0 and t1:
            topix_ret = round((t1 - t0) / t0 * 100, 3)

        # --- 追記のみ(検出時フィールドは不変) ---
        r['closed_at'] = datetime.now(JST).isoformat()
        r['exit_price'] = res['exit_price']
        r['exit_reason'] = res['exit_reason']
        r['return_pct'] = res['return_pct']
        r['holding_days'] = hold
        r['exit_date'] = exit_date
        r['baseline'] = {
            'dumb_return_pct': dumb_ret,
            'topix_return_pct': topix_ret,
            'excess_vs_dumb': (round(r['return_pct'] - dumb_ret, 3) if dumb_ret is not None else None),
            'excess_vs_topix': (round(r['return_pct'] - topix_ret, 3) if topix_ret is not None else None),
        }
        r['status'] = 'closed'
        closed += 1
        print(f"  決済: {r['ticker']} {r['exit_reason']} ret={r['return_pct']}% {hold}日 (dumb={dumb_ret} topix={topix_ret})")
    return closed


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def recompute_summary(obs):
    """現行 logic_version のクローズ済みのみで集計(時計リセット規約)。"""
    ver = LOGIC_VERSION
    closed = [r for r in obs['records'] if r.get('status') == 'closed' and r.get('logic_version') == ver]
    rets = [r.get('return_pct') for r in closed if r.get('return_pct') is not None]
    wins = [x for x in rets if x > 0]
    losses = [abs(x) for x in rets if x <= 0]
    avg_win = _mean(wins)
    avg_loss = _mean(losses)
    r_mult = round(avg_win / avg_loss, 2) if (avg_win and avg_loss) else None
    ex_dumb = _mean([ (r.get('baseline') or {}).get('excess_vs_dumb') for r in closed ])
    ex_topix = _mean([ (r.get('baseline') or {}).get('excess_vs_topix') for r in closed ])

    # 最大ドローダウン(クローズ列の累積リターンのエクイティカーブ)
    max_dd = None
    if rets:
        eq = 0.0; peak = 0.0; dd = 0.0
        for x in rets:
            eq += x
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        max_dd = round(dd, 3)

    # 地合い別内訳(検出時 market_condition.label ごと) + 弱地合サブグループ
    by_regime = {}
    for r in closed:
        mc = (r.get('detection_context') or {}).get('market_condition') or {}
        lab = mc.get('label', 'unknown')
        by_regime.setdefault(lab, []).append(r.get('return_pct'))
    regime_ev = {k: {'n': len([x for x in v if x is not None]), 'expectancy': _mean(v)} for k, v in by_regime.items()}
    weak_rets = [r.get('return_pct') for r in closed
                 if ((r.get('detection_context') or {}).get('market_condition') or {}).get('weak')]
    weak_ev = _mean(weak_rets)

    summary = {
        'logic_version': ver,
        'closed': len(closed),
        'open': len([r for r in obs['records'] if r.get('status') == 'open' and r.get('logic_version') == ver]),
        'total_records_all_versions': len(obs['records']),
        'win_rate': round(len(wins) / len(rets) * 100, 1) if rets else None,
        'avg_return': _mean(rets),
        'expectancy': _mean(rets),                 # 期待値 = 1トレード平均リターン
        'avg_win': avg_win, 'avg_loss': avg_loss,
        'r_multiple': r_mult,
        'excess_vs_dumb': ex_dumb,
        'excess_vs_topix': ex_topix,
        'max_drawdown': max_dd,
        'regime_breakdown': regime_ev,
        'weak_regime_expectancy': weak_ev,
    }
    return summary


def main():
    now = datetime.now(JST)
    print("=" * 56)
    print(f"  観測モード 決済ジョブ  {now.isoformat()}  logic={LOGIC_VERSION}")
    print("=" * 56)
    if not os.path.exists(OBS_PATH):
        print("  observations.json が無い(まだ検出なし)。何もしない。")
        return
    obs = load_obs()
    n_closed = close_open_records(obs)

    summary = recompute_summary(obs)
    start = obs['meta'].get('observation_start')
    days_elapsed = None
    if start:
        try:
            days_elapsed = (now.date() - datetime.strptime(start, '%Y-%m-%d').date()).days
        except Exception:
            days_elapsed = None
    promotion = evaluate_promotion(summary, days_elapsed)

    obs['meta']['last_close_run'] = now.isoformat()
    obs['meta']['logic_version'] = LOGIC_VERSION
    obs['meta']['days_elapsed'] = days_elapsed
    obs['meta']['summary'] = summary
    obs['meta']['promotion'] = promotion
    obs['meta']['criteria'] = PROMOTION_CRITERIA_V1

    with open(OBS_PATH, 'w', encoding='utf-8') as f:
        json.dump(obs, f, ensure_ascii=False, indent=2, allow_nan=False)

    print(f"\n  今回決済 {n_closed} 件 / クローズ累計 {summary['closed']} 件 / "
          f"期待値 {summary['expectancy']} / R {summary['r_multiple']} / "
          f"格上げ判定可能: {promotion['all_pass']}")


if __name__ == '__main__':
    main()
