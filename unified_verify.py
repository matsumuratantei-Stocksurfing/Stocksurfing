#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
unified_verify.py — 松村式一本化戦略v1 の「答え合わせ」(前向き・発注なし)

unified_signals.py が出した本日の買い候補を、初回だけ仮想トレードとして
台帳(unified_ledger.json)に **open で追記** する(append-only・不可変)。
open トレードは backtest_unified と同じ出口ルールで仮想決済し、close 項目を追記する。
決済ルール = 構造損切り / +5%で半分利確→建値ストップ / 25日線割れトレール / 最長40営業日。

集計(勝率/期待値/R/大勝ち率/最大DD)と格上げ達成状況は、
**現行 LOGIC_VERSION のクローズ済みのみ**で再計算する(時計リセット規約)。

【発注禁止】証券API・発注モジュールは一切importしない。
【前向き】決済シミュは検出日より後の“実際に経過した営業日”のみ使用。未来非参照。
"""
import os
import sys
import json
import time
import statistics
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from po_detector import JST, fetch_days, _sma

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SIG_PATH = os.path.join(SCRIPT_DIR, 'unified_signals.json')
LEDGER_PATH = os.path.join(SCRIPT_DIR, 'unified_ledger.json')
SLEEP_BETWEEN_CALLS = 0.3

LOGIC_VERSION = 'unified-v1'
TP_PCT = 5.0
MAXHOLD = 40

# 一本化戦略の格上げ基準(開始前に確定・開始後変更不可)
UNIFIED_PROMOTION_V1 = {
    'min_observation_days': 90,
    'min_closed': 30,
    'min_win_rate': 55.0,      # 勝率 >= 55%
    'min_expectancy': 0.0,     # 期待値 > 0
    'min_r_multiple': 1.3,     # R倍数 >= 1.3
}


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def simulate_exit(days, i, entry, stop0, tp_pct=TP_PCT, maxhold=MAXHOLD):
    """検出日index i の後の実バーで仮想決済。resolボ済みなら結果dict、未決着ならNone。"""
    n = len(days)
    closes = [d['c'] for d in days]
    highs = [d['h'] for d in days]
    lows = [d['l'] for d in days]
    sma25 = [_sma(closes, k, 25) for k in range(n)]
    if entry is None or entry <= 0 or stop0 is None:
        return None
    stop = stop0
    half = False
    realized = 0.0
    tp = entry * (1 + tp_pct / 100)
    for k in range(1, maxhold + 1):
        j = i + k
        if j >= n:
            return None  # まだバーが無い(未決着)
        lo, hi, cl = lows[j], highs[j], closes[j]
        if lo is not None and lo <= stop:
            rem = 0.5 if half else 1.0
            realized += rem * (stop - entry) / entry * 100
            return {'exit_date': days[j]['date'], 'exit_reason': ('stop_after_half' if half else 'stop'),
                    'net_return_pct': round(realized, 3), 'holding_days': k}
        if not half and hi is not None and hi >= tp:
            realized += 0.5 * tp_pct
            half = True
            stop = max(stop, entry)
        s25 = sma25[j]
        if cl is not None and s25 is not None and cl < s25:
            rem = 0.5 if half else 1.0
            realized += rem * (cl - entry) / entry * 100
            return {'exit_date': days[j]['date'], 'exit_reason': ('trail_after_half' if half else 'trail'),
                    'net_return_pct': round(realized, 3), 'holding_days': k}
        if k >= maxhold and cl is not None:
            rem = 0.5 if half else 1.0
            realized += rem * (cl - entry) / entry * 100
            return {'exit_date': days[j]['date'], 'exit_reason': ('time_after_half' if half else 'time'),
                    'net_return_pct': round(realized, 3), 'holding_days': k}
    return None


def open_new_trades(ledger, sig):
    """本日の候補のうち、まだ台帳に無いものを open で追記(append-only・冪等)。"""
    today = (sig or {}).get('today') or {}
    cands = today.get('candidates') or []
    existing = {t['trade_id'] for t in ledger['trades']}
    now = datetime.now(JST).isoformat()
    added = 0
    for c in cands:
        tid = f"{c.get('date')}_{c.get('ticker')}"
        if tid in existing:
            continue
        ledger['trades'].append({
            'trade_id': tid, 'logic_version': LOGIC_VERSION, 'opened_at': now,
            'ticker': c.get('ticker'), 'ticker_name': c.get('ticker_name'), 'group': c.get('group'),
            'entry_date': c.get('date'), 'entry': c.get('entry_ref'),
            'structural_stop': c.get('structural_stop'), 'target_half': c.get('target_half'),
            'is_new_52w_high': c.get('is_new_52w_high'), 'regime_on_at_entry': today.get('regime_on'),
            'status': 'open',
        })
        existing.add(tid)
        added += 1
    return added


def close_open_trades(ledger):
    opens = [t for t in ledger['trades'] if t.get('status') == 'open']
    if not opens:
        return 0
    codes = sorted({t['ticker'] for t in opens})
    series = {}
    for c in codes:
        series[c] = fetch_days(c)
        time.sleep(SLEEP_BETWEEN_CALLS)
    now = datetime.now(JST).isoformat()
    closed = 0
    for t in opens:
        days = series.get(t['ticker']) or []
        if not days:
            continue
        order = [d['date'] for d in days]
        if t['entry_date'] not in order:
            continue
        i = order.index(t['entry_date'])
        res = simulate_exit(days, i, t['entry'], t['structural_stop'])
        if not res:
            continue
        t['closed_at'] = now
        t['exit_date'] = res['exit_date']
        t['exit_reason'] = res['exit_reason']
        t['net_return_pct'] = res['net_return_pct']
        t['holding_days'] = res['holding_days']
        t['status'] = 'closed'
        closed += 1
        print(f"  決済: {t['ticker']} {t['exit_reason']} {t['net_return_pct']}% {t['holding_days']}日")
    return closed


def recompute_summary(ledger):
    ver = LOGIC_VERSION
    closed = [t for t in ledger['trades'] if t.get('status') == 'closed' and t.get('logic_version') == ver]
    nets = [t.get('net_return_pct') for t in closed if t.get('net_return_pct') is not None]
    wins = [x for x in nets if x > 0]
    losses = [abs(x) for x in nets if x <= 0]
    avg_win = _mean(wins)
    avg_loss = _mean(losses)
    r_mult = round(avg_win / avg_loss, 2) if (avg_win and avg_loss) else None
    max_dd = None
    if nets:
        eq = 0.0; peak = 0.0; dd = 0.0
        for x in nets:
            eq += x; peak = max(peak, eq); dd = min(dd, eq - peak)
        max_dd = round(dd, 1)
    return {
        'logic_version': ver,
        'closed': len(closed),
        'open': len([t for t in ledger['trades'] if t.get('status') == 'open' and t.get('logic_version') == ver]),
        'win_rate': round(len(wins) / len(nets) * 100, 1) if nets else None,
        'expectancy': _mean(nets),
        'avg_win': avg_win,
        'avg_loss': (round(-avg_loss, 3) if avg_loss is not None else None),
        'r_multiple': r_mult,
        'bigwin_pct': round(sum(1 for x in nets if x >= 8) / len(nets) * 100, 1) if nets else None,
        'max_drawdown': max_dd,
        'avg_hold': _mean([t.get('holding_days') for t in closed]),
    }


def evaluate_promotion(summary, observation_days, criteria=UNIFIED_PROMOTION_V1):
    s = summary or {}
    days = observation_days or 0
    closed = s.get('closed') or 0
    wr = s.get('win_rate')
    ev = s.get('expectancy')
    r = s.get('r_multiple')
    checks = [
        {'no': 1, 'label': '観測期間>=90暦日', 'target': criteria['min_observation_days'], 'actual': days,
         'pass': days >= criteria['min_observation_days']},
        {'no': 2, 'label': 'クローズ済み>=30件', 'target': criteria['min_closed'], 'actual': closed,
         'pass': closed >= criteria['min_closed']},
        {'no': 3, 'label': '勝率>=55%', 'target': criteria['min_win_rate'], 'actual': wr,
         'pass': (wr is not None and wr >= criteria['min_win_rate'])},
        {'no': 4, 'label': '期待値>0', 'target': f">{criteria['min_expectancy']}", 'actual': ev,
         'pass': (ev is not None and ev > criteria['min_expectancy'])},
        {'no': 5, 'label': 'R倍数>=1.3', 'target': criteria['min_r_multiple'], 'actual': r,
         'pass': (r is not None and r >= criteria['min_r_multiple'])},
    ]
    return {'criteria_version': 'unified-V1', 'criteria': criteria, 'checks': checks,
            'all_pass': all(c['pass'] for c in checks), 'days_elapsed': days}


def main():
    now = datetime.now(JST)
    print("=" * 56)
    print(f"  一本化戦略 答え合わせ  {now.isoformat()}  {LOGIC_VERSION}")
    print("=" * 56)
    sig = load_json(SIG_PATH, {})
    ledger = load_json(LEDGER_PATH, {'meta': {}, 'trades': []})
    ledger.setdefault('trades', [])

    if not ledger['meta'].get('observation_start'):
        ledger['meta']['observation_start'] = now.date().strftime('%Y-%m-%d')

    n_open = open_new_trades(ledger, sig)
    n_closed = close_open_trades(ledger)

    summary = recompute_summary(ledger)
    start = ledger['meta'].get('observation_start')
    days_elapsed = None
    if start:
        try:
            days_elapsed = (now.date() - datetime.strptime(start, '%Y-%m-%d').date()).days
        except Exception:
            days_elapsed = None
    promotion = evaluate_promotion(summary, days_elapsed)

    ledger['meta'].update({
        'last_run': now.isoformat(), 'logic_version': LOGIC_VERSION,
        'days_elapsed': days_elapsed, 'summary': summary, 'promotion': promotion,
        'criteria': UNIFIED_PROMOTION_V1,
        'note': '検証中(前向き実測)。発注は行われません。集計は現行logic_versionのクローズ済みのみ。',
    })
    with open(LEDGER_PATH, 'w', encoding='utf-8') as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"\n  新規建て {n_open} / 今回決済 {n_closed} / クローズ累計 {summary['closed']} / "
          f"勝率 {summary['win_rate']} / 期待値 {summary['expectancy']} / 格上げ可 {promotion['all_pass']}")


if __name__ == '__main__':
    main()
