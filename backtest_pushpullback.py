#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stocksurfing Phase 0 — 12:05「前場→後場 押し目」ロジックのバックテスト計測 (計測専用)

本番ロジック(fetch_am.py / send_email.py / index.html / weights.json)には一切触れない。
提案中の押し目判定の閾値が実データでエッジを持つかを、計測のみで検証する。

データ: J-Quants V2 /equities/bars/daily を common.py の個別24銘柄について
        直近およそ250営業日分取得(前場 MO/MH/ML/MC・後場 AO/AH/AL/AC 含む=Premium)。
出力: push_backtest_report.json + 人が読む要約(標準出力)。

【価格系列の注記】
分割・併合による見かけ上の巨大リターンを避けるため、内部計算は「調整済み株価」を優先使用する
(日次 AdjC/AdjH/AdjL、前場 MAdjO/MAdjH/MAdjL/MAdjC、後場 AAdjO/AAdjH/AAdjL/AAdjC)。
調整済みが無い行は調整前(C/MO..等)にフォールバックし、その日の AdjFactor!=1(権利落ち)の
観測は歪み回避のため除外する。式・閾値は指示どおり。
"""
import os
import sys
import json
import time
import statistics
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_STOCKS
from jquants_client import jq_get

JST = timezone(timedelta(hours=9))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LOOKBACK_CALENDAR_DAYS = 400   # ~250営業日をカバーする暦日
SLEEP_BETWEEN_CALLS = 0.3      # レート制限配慮
STOP_PCT = -2.0                # 損切
TARGET_PCT = 4.0               # 利確


# ---------- 数値ヘルパ ----------
def _f(row, *keys):
    """rowから最初に見つかった非Noneのキーをfloatで返す(調整済み→調整前の順で渡す)。"""
    for k in keys:
        v = row.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _stats(vals):
    """平均/中央値/勝率(>0%)/標準偏差 をまとめて返す。"""
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n == 0:
        return {'n': 0, 'mean': None, 'median': None, 'winRate': None, 'std': None}
    wins = sum(1 for v in vals if v > 0)
    return {
        'n': n,
        'mean': round(statistics.mean(vals), 3),
        'median': round(statistics.median(vals), 3),
        'winRate': round(wins / n * 100, 1),
        'std': round(statistics.pstdev(vals), 3) if n >= 2 else 0.0,
    }


# ---------- データ取得 ----------
def fetch_series(code):
    """1銘柄の日次四本値(前場/後場含む)を日付昇順で返す。"""
    today = datetime.now(JST).date()
    frm = (today - timedelta(days=LOOKBACK_CALENDAR_DAYS)).strftime('%Y-%m-%d')
    to = today.strftime('%Y-%m-%d')
    data = jq_get('/equities/bars/daily', {'code': code, 'from': frm, 'to': to})
    if not data:
        return []
    rows = data.get('data', [])
    rows = [r for r in rows if r.get('Date')]
    rows.sort(key=lambda r: r['Date'])
    days = []
    for r in rows:
        adj_factor = _f(r, 'AdjFactor')
        days.append({
            'date': r.get('Date'),
            'adjFactor': adj_factor if adj_factor is not None else 1.0,
            'c':  _f(r, 'AdjC', 'C'),
            'h':  _f(r, 'AdjH', 'H'),
            'l':  _f(r, 'AdjL', 'L'),
            'mo': _f(r, 'MAdjO', 'MO'), 'mh': _f(r, 'MAdjH', 'MH'),
            'ml': _f(r, 'MAdjL', 'ML'), 'mc': _f(r, 'MAdjC', 'MC'),
            'ao': _f(r, 'AAdjO', 'AO'), 'ah': _f(r, 'AAdjH', 'AH'),
            'al': _f(r, 'AAdjL', 'AL'), 'ac': _f(r, 'AAdjC', 'AC'),
        })
    return days


# ---------- -2%損切/+4%利確 到達順シミュ ----------
def sim_trade(entry, bars):
    """entry(前場終値)基準で、後場〜翌3日の(安値,高値)列を時系列に走査。
    先に -2% に触れたら LOSE、先に +4% に触れたら WIN、どちらも触れなければ EVEN。
    同一バーで両到達は判定不能だが保守的に LOSE(損切優先)とする。"""
    if entry is None or entry <= 0:
        return 'EVEN'
    stop = entry * (1 + STOP_PCT / 100.0)
    target = entry * (1 + TARGET_PCT / 100.0)
    for lo, hi in bars:
        if lo is None or hi is None:
            continue
        hit_stop = lo <= stop
        hit_tgt = hi >= target
        if hit_stop:      # 同日両到達も保守的にLOSE
            return 'LOSE'
        if hit_tgt:
            return 'WIN'
    return 'EVEN'


# ---------- バケット判定 ----------
def classify(morning_ret, close_pos, range_pct):
    """優先順位: range外→OUT / 前場マイナス→D / A窓→A / 高値引け→B / 安値引け→C / その他→E。
    (優先順位は透明性のためレポートにも明記)"""
    if range_pct < 0.8 or range_pct > 6:
        return 'OUT'
    if morning_ret < 0:
        return 'D'
    if 0 <= morning_ret <= 3 and 0.30 <= close_pos <= 0.65:
        return 'A'
    if close_pos > 0.80:
        return 'B'
    if close_pos < 0.20:
        return 'C'
    return 'E'


def main():
    print("=" * 60)
    print("  Stocksurfing Phase0 押し目バックテスト (計測専用)")
    print(f"  実行: {datetime.now(JST).isoformat()}")
    print("=" * 60)

    observations = []              # 各観測(銘柄×営業日)
    market_ret_by_date = {}        # 地合い代理: 日付->各銘柄の日次終値リターン
    ok_stocks = 0

    for st in DEFAULT_STOCKS:
        code = st['code']
        days = fetch_series(code)
        time.sleep(SLEEP_BETWEEN_CALLS)
        if len(days) < 10:
            print(f"  {code} {st['name']}: データ不足({len(days)}日) スキップ")
            continue
        ok_stocks += 1

        # 地合い代理: 各営業日の日次終値リターンを蓄積
        for i in range(1, len(days)):
            cprev = days[i - 1]['c']
            c = days[i]['c']
            if cprev and c and cprev > 0:
                market_ret_by_date.setdefault(days[i]['date'], []).append((c - cprev) / cprev * 100)

        # 観測の生成
        for i in range(1, len(days)):
            prev, cur = days[i - 1], days[i]
            # 権利落ち等の歪み回避
            if cur['adjFactor'] != 1.0 or prev['adjFactor'] != 1.0:
                continue
            cprev = prev['c']
            mo, mh, ml, mc = cur['mo'], cur['mh'], cur['ml'], cur['mc']
            ac = cur['ac']
            if None in (cprev, mh, ml, mc) or cprev <= 0 or mh == ml:
                continue

            morning_ret = (mc - cprev) / cprev * 100
            close_pos = (mc - ml) / (mh - ml)
            range_pct = (mh - ml) / cprev * 100
            push_score = morning_ret * (1 - close_pos)
            pm_ret = ((ac - mc) / mc * 100) if (ac is not None and mc) else None

            cur_c = cur['c']
            next1_ret = None
            next3_ret = None
            if cur_c:
                if i + 1 < len(days) and days[i + 1]['c']:
                    next1_ret = (days[i + 1]['c'] - cur_c) / cur_c * 100
                if i + 3 < len(days) and days[i + 3]['c']:
                    next3_ret = (days[i + 3]['c'] - cur_c) / cur_c * 100

            # -2/+4 シミュ: 後場(al,ah) + 翌1〜3日(l,h)
            bars = [(cur['al'], cur['ah'])]
            for k in (1, 2, 3):
                if i + k < len(days):
                    bars.append((days[i + k]['l'], days[i + k]['h']))
            sim = sim_trade(mc, bars)

            observations.append({
                'code': code, 'date': cur['date'],
                'morning_ret': morning_ret, 'close_pos': close_pos, 'range_pct': range_pct,
                'push_score': push_score, 'pm_ret': pm_ret,
                'next1_ret': next1_ret, 'next3_ret': next3_ret, 'sim': sim,
                'bucket': classify(morning_ret, close_pos, range_pct),
            })
        print(f"  {code} {st['name']}: {len(days)}日取得")

    # 地合いレジーム: 各日の中央値リターンを3分位に
    date_market = {d: statistics.median(v) for d, v in market_ret_by_date.items() if v}
    med_vals = sorted(date_market.values())
    regime_of_date = {}
    if len(med_vals) >= 6:
        lo_thr = med_vals[len(med_vals) // 3]
        hi_thr = med_vals[2 * len(med_vals) // 3]
        for d, m in date_market.items():
            regime_of_date[d] = '逆風' if m <= lo_thr else ('追い風' if m >= hi_thr else '中立')

    # ---------- 集計 ----------
    def agg(rows):
        return {
            'n': len(rows),
            'pm_ret': _stats([r['pm_ret'] for r in rows]),
            'next1_ret': _stats([r['next1_ret'] for r in rows]),
            'next3_ret': _stats([r['next3_ret'] for r in rows]),
            'sim': _sim_counts(rows),
        }

    def _sim_counts(rows):
        w = sum(1 for r in rows if r['sim'] == 'WIN')
        l = sum(1 for r in rows if r['sim'] == 'LOSE')
        e = sum(1 for r in rows if r['sim'] == 'EVEN')
        tot = w + l + e
        decided = w + l
        return {
            'win': w, 'lose': l, 'even': e,
            'winPct_ofAll': round(w / tot * 100, 1) if tot else None,
            'winPct_ofDecided': round(w / decided * 100, 1) if decided else None,
        }

    buckets = {}
    for b in ['A', 'B', 'C', 'D', 'E', 'OUT']:
        rows = [o for o in observations if o['bucket'] == b]
        buckets[b] = agg(rows) if b != 'OUT' else {'n': len(rows)}

    # A内 push_score 3分位の単調性
    a_rows = [o for o in observations if o['bucket'] == 'A' and o['push_score'] is not None]
    a_rows.sort(key=lambda o: o['push_score'])
    tercile = {}
    monotonic_note = "サンプル不足"
    if len(a_rows) >= 9:
        t = len(a_rows) // 3
        groups = {'low': a_rows[:t], 'mid': a_rows[t:2 * t], 'high': a_rows[2 * t:]}
        for name, rows in groups.items():
            tercile[name] = {
                'n': len(rows),
                'push_score_range': [round(rows[0]['push_score'], 3), round(rows[-1]['push_score'], 3)],
                'pm_ret': _stats([r['pm_ret'] for r in rows]),
                'next3_ret': _stats([r['next3_ret'] for r in rows]),
            }
        # 単調性(next3平均が low<=mid<=high か)
        def m(g, key):
            return tercile[g][key]['mean']
        n3 = [m('low', 'next3_ret'), m('mid', 'next3_ret'), m('high', 'next3_ret')]
        pm = [m('low', 'pm_ret'), m('mid', 'pm_ret'), m('high', 'pm_ret')]
        def mono(x):
            x = [v for v in x if v is not None]
            return len(x) == 3 and x[0] <= x[1] <= x[2]
        monotonic_note = (
            f"next3: {'単調↑' if mono(n3) else '非単調'} ({n3}) / "
            f"pm_ret: {'単調↑' if mono(pm) else '非単調'} ({pm})"
        )

    # close_pos 感度スキャン(morning_ret 0〜3%・range OK の行を close_pos で細分)
    scan_src = [o for o in observations
                if o['bucket'] != 'OUT' and 0 <= o['morning_ret'] <= 3]
    scan = []
    edges = [0.0, 0.20, 0.35, 0.50, 0.65, 0.80, 1.01]
    for a, bnd in zip(edges[:-1], edges[1:]):
        rows = [o for o in scan_src if a <= o['close_pos'] < bnd]
        scan.append({
            'close_pos': f'[{a:.2f},{bnd:.2f})',
            'n': len(rows),
            'pm_ret_mean': _stats([r['pm_ret'] for r in rows])['mean'],
            'next3_mean': _stats([r['next3_ret'] for r in rows])['mean'],
            'next3_winRate': _stats([r['next3_ret'] for r in rows])['winRate'],
        })

    # 地合い別(Aバケット)
    regime_A = {}
    if regime_of_date:
        for reg in ['追い風', '中立', '逆風']:
            rows = [o for o in observations if o['bucket'] == 'A' and regime_of_date.get(o['date']) == reg]
            regime_A[reg] = {
                'n': len(rows),
                'pm_ret': _stats([r['pm_ret'] for r in rows]),
                'next3_ret': _stats([r['next3_ret'] for r in rows]),
                'sim': _sim_counts(rows),
            }

    # ---------- 所見(データ駆動・3行) ----------
    def _mean(b, key):
        v = buckets.get(b, {}).get(key, {})
        return v.get('mean') if isinstance(v, dict) else None

    verdict = []
    a_n3 = _mean('A', 'next3_ret'); a_pm = _mean('A', 'pm_ret')
    others_rows = [o for o in observations if o['bucket'] in ('B', 'C', 'D')]
    o_n3 = _stats([r['next3_ret'] for r in others_rows])['mean']
    o_pm = _stats([r['pm_ret'] for r in others_rows])['mean']
    a_simw = buckets.get('A', {}).get('sim', {}).get('winPct_ofDecided')
    if a_n3 is not None and o_n3 is not None:
        diff = round(a_n3 - o_n3, 2)
        superior = (a_n3 > o_n3) and ((a_pm or 0) >= (o_pm or 0))
        verdict.append(
            f"(1) A優位性: A next3平均={a_n3}% vs B/C/D={o_n3}% (差{diff}pt), "
            f"A pm_ret平均={a_pm}% vs {o_pm}%, A決着勝率={a_simw}% → "
            f"{'明確に優位とは言い切れない' if not superior or abs(diff) < 0.3 else 'Aが優位'}"
        )
    else:
        verdict.append("(1) A優位性: サンプル/データ不足で判定不能")

    best = max([s for s in scan if s['n'] >= 5], key=lambda s: (s['next3_mean'] or -99), default=None)
    if best:
        verdict.append(
            f"(2) 閾値妥当性: close_pos感度スキャンで next3平均が最良の帯は {best['close_pos']} "
            f"(next3平均 {best['next3_mean']}% / n={best['n']})。現行 0.30-0.65 と比べ、"
            f"この帯にずらす/絞る余地を検討。range/morning_retの現行値は据え置きで可否を要判断"
        )
    else:
        verdict.append("(2) 閾値妥当性: スキャン各帯のサンプル不足で数値提案は保留")
    verdict.append(f"(3) push_score単調性: {monotonic_note}")

    report = {
        'generatedAt': datetime.now(JST).isoformat(),
        'note': '計測専用。本番ロジック非変更。価格は調整済み(Adj*)優先、権利落ち日は除外。',
        'lookbackCalendarDays': LOOKBACK_CALENDAR_DAYS,
        'stocksUsed': ok_stocks,
        'totalObservations': len(observations),
        'stopPct': STOP_PCT, 'targetPct': TARGET_PCT,
        'bucketPrecedence': 'range外→OUT / morning_ret<0→D / A窓(0<=mr<=3 & 0.30<=cp<=0.65)→A / cp>0.80→B / cp<0.20→C / それ以外→E',
        'buckets': buckets,
        'pushScoreTerciles_withinA': tercile,
        'closePosSensitivityScan_mr0to3': scan,
        'regimeBreakdown_A': regime_A,
        'verdict': verdict,
    }

    out_path = os.path.join(SCRIPT_DIR, 'push_backtest_report.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)

    # ---------- 人が読む要約 ----------
    print("\n" + "=" * 60)
    print(f"  観測数 {len(observations)} / 銘柄 {ok_stocks} / lookback {LOOKBACK_CALENDAR_DAYS}日")
    print("=" * 60)
    print(f"{'バケット':<6}{'n':>6}{'pm_ret平均':>12}{'pm勝率':>9}{'next1平均':>11}{'next3平均':>11}{'next3勝率':>10}{'決着勝率':>9}")
    for b in ['A', 'B', 'C', 'D', 'E']:
        bk = buckets[b]
        if bk['n'] == 0:
            print(f"{b:<6}{0:>6}{'--':>12}"); continue
        print(f"{b:<6}{bk['n']:>6}{str(bk['pm_ret']['mean']):>12}{str(bk['pm_ret']['winRate']):>9}"
              f"{str(bk['next1_ret']['mean']):>11}{str(bk['next3_ret']['mean']):>11}"
              f"{str(bk['next3_ret']['winRate']):>10}{str(bk['sim']['winPct_ofDecided']):>9}")
    print(f"OUT   {buckets['OUT']['n']:>6}  (range<0.8% or >6% で対象外)")

    print("\n[A内 push_score 3分位の単調性]")
    for name in ['low', 'mid', 'high']:
        if name in tercile:
            t = tercile[name]
            print(f"  {name:<5} n={t['n']:>4} pushScore{t['push_score_range']} "
                  f"pm_ret平均={t['pm_ret']['mean']} next3平均={t['next3_ret']['mean']}")
    print(f"  → {monotonic_note}")

    print("\n[close_pos 感度スキャン (morning_ret 0〜3%)]")
    for s in scan:
        print(f"  {s['close_pos']:<14} n={s['n']:>4} pm平均={s['pm_ret_mean']} "
              f"next3平均={s['next3_mean']} next3勝率={s['next3_winRate']}")

    if regime_A:
        print("\n[Aバケットの地合い別]")
        for reg, r in regime_A.items():
            print(f"  {reg:<4} n={r['n']:>4} pm平均={r['pm_ret']['mean']} "
                  f"next3平均={r['next3_ret']['mean']} 決着勝率={r['sim']['winPct_ofDecided']}")

    print("\n" + "=" * 60)
    print("  所見(計測者):")
    for line in verdict:
        print("  " + line)
    print("=" * 60)
    print(f"\n  ✓ push_backtest_report.json 書き出し完了")


if __name__ == '__main__':
    main()
