#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stocksurfing Phase 1 — パーフェクトオーダー押し目の戻り率バックテスト (計測専用)

本番ロジック(index.html / send_email.py / weights.json / 予測ロジック)には一切触れない。
検出ロジック・構造損切りは po_detector.py を単一ソースとして import する
(観測モード observe_* と同じコードを共有。ロジック重複を避けるため)。

3群を別々に集計:
  (1) 個別24銘柄   : common.py の個別リスト
  (2) セクターETF  : 半導体 213A/221A/200A/282A/2243, 防衛テック 513A, 建設・資材 1619
  (3) ベースライン : TOPIX ETF 1306 (パーフェクトオーダー条件を課さない対照群)

出力: po_backtest_report.json + 人が読む要約(標準出力)。
BACKTEST_MODE=oos で Phase1.5(下げ/暴落再現)モード。

【重要な限界】本標本は上昇相場に偏り。取引コスト/スリッページ未考慮。価格は調整済み(AdjC)。
"""
import os
import sys
import json
import time
import statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_STOCKS, INDICATORS
# 検出・構造損切りは単一ソース(po_detector)から。ロジックはここでは定義しない。
from po_detector import (
    JST, LOOKBACK_CALENDAR_DAYS, PULLBACK_BANDS, M_LIST, M_PRIMARY,
    STOP_HORIZON, FIXED_STOP_PCT, _f, _sma, band_label, fetch_days,
    sim_stop, scan_events,
)
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SLEEP_BETWEEN_CALLS = 0.3

# セクターETF(レバ/インバースは対象外)
ETF_UNIVERSE = [
    {'code': '213A', 'name': '半導体ETF-A'}, {'code': '221A', 'name': '半導体ETF-B'},
    {'code': '200A', 'name': '半導体ETF-C'}, {'code': '282A', 'name': '半導体ETF-D'},
    {'code': '2243', 'name': '半導体ETF-E'}, {'code': '513A', 'name': '防衛テックETF'},
    {'code': '1619', 'name': '建設・資材ETF'},
]
BASELINE_UNIVERSE = [{'code': '1306', 'name': 'TOPIX ETF(ベースライン)'}]


# ---------- 集計ヘルパ (backtest専用) ----------
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def _rate(flags):
    flags = [f for f in flags if f is not None]
    return round(sum(flags) / len(flags) * 100, 1) if flags else None


def cell_summary(rows, M=M_PRIMARY):
    def sim_counts(key):
        w = sum(1 for r in rows if r[key] == 'WIN')
        l = sum(1 for r in rows if r[key] == 'LOSE')
        e = sum(1 for r in rows if r[key] == 'EVEN')
        dec = w + l
        return {'win': w, 'lose': l, 'even': e,
                'winPct_decided': round(w / dec * 100, 1) if dec else None}
    return {
        'n': len(rows),
        'recovered_pct': _rate([r[f'rec{M}'] for r in rows]),
        'fwd_mean': _mean([r[f'fwd{M}'] for r in rows]),
        'fwd_median': _median([r[f'fwd{M}'] for r in rows]),
        'maxdd_mean': _mean([r[f'dd{M}'] for r in rows]),
        'maxdd_median': _median([r[f'dd{M}'] for r in rows]),
        'broke200_pct': _rate([r[f'broke{M}'] for r in rows]),
        'recover_days_median': _median([r[f'recdays{M}'] for r in rows if r[f'rec{M}']]),
        'slope25_mean': _mean([r['slope25'] for r in rows]),
        'stop_struct': sim_counts('stop_struct'),
        'stop_fixed7': sim_counts('stop_fixed7'),
    }


# ==================================================================
#  パートII: 場の判定12指標の貢献度ランキング
# ==================================================================
def _pearson(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return None, n
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sx = sum((p[0] - mx) ** 2 for p in pairs)
    sy = sum((p[1] - my) ** 2 for p in pairs)
    if sx <= 0 or sy <= 0:
        return None, n
    cov = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    return round(cov / (sx ** 0.5 * sy ** 0.5), 3), n


def analyze_indicators():
    path = os.path.join(SCRIPT_DIR, 'verification_log.json')
    if not os.path.exists(path):
        return {'available': False, 'note': 'verification_log.json が見つからない'}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            log = json.load(f)
    except Exception as e:
        return {'available': False, 'note': f'読込失敗: {e}'}

    rows = []
    for l in log:
        y = ((l.get('actualIndices') or {}).get('N225') or {}).get('chgPct')
        mi = l.get('morningIndicators') or {}
        if y is None or not mi:
            continue
        rows.append((y, mi))
    N = len(rows)
    keys = [ind['key'] for ind in INDICATORS]
    inv = {ind['key']: ind['inverse'] for ind in INDICATORS}
    wt = {ind['key']: ind['weight'] for ind in INDICATORS}
    ys = [r[0] for r in rows]

    per = []
    for k in keys:
        sign = -1 if inv[k] else 1
        xs_adj = [(r[1].get(k) * sign if r[1].get(k) is not None else None) for r in rows]
        corr, nn = _pearson(xs_adj, ys)
        hit = tot = 0
        for x, y in zip(xs_adj, ys):
            if x is None or y is None or x == 0 or y == 0:
                continue
            tot += 1
            if (x > 0) == (y > 0):
                hit += 1
        per.append({
            'key': k, 'weight': wt[k], 'inverse': inv[k], 'n': nn,
            'corr_vs_N225': corr,
            'direction_hit_pct': round(hit / tot * 100, 1) if tot else None,
            'direction_n': tot,
        })
    per.sort(key=lambda p: (abs(p['corr_vs_N225']) if p['corr_vs_N225'] is not None else -1), reverse=True)

    high_pairs = []
    for a in range(len(keys)):
        for b in range(a + 1, len(keys)):
            r_ab, nn = _pearson([r[1].get(keys[a]) for r in rows],
                                [r[1].get(keys[b]) for r in rows])
            if r_ab is not None and abs(r_ab) >= 0.8:
                high_pairs.append({'pair': [keys[a], keys[b]], 'corr': r_ab, 'n': nn})
    high_pairs.sort(key=lambda p: abs(p['corr']), reverse=True)

    delete = [p['key'] for p in per
              if (abs(p['corr_vs_N225']) if p['corr_vs_N225'] is not None else 0) < 0.15 and p['weight'] <= 1.0]
    core = [p['key'] for p in per
            if (abs(p['corr_vs_N225']) if p['corr_vs_N225'] is not None else 0) >= 0.4]
    merge = [f"{h['pair'][0]}↔{h['pair'][1]}(r={h['corr']})" for h in high_pairs]

    return {
        'available': True, 'samples': N,
        'perIndicator': per,
        'multicollinearity_highPairs': high_pairs,
        'pruning': {'delete_candidates': delete, 'merge_pairs': merge, 'core': core},
    }


def main():
    print("=" * 64)
    print("  Stocksurfing Phase1 パーフェクトオーダー押し目バックテスト (計測専用)")
    print(f"  実行: {datetime.now(JST).isoformat()}")
    print("=" * 64)

    universe = (
        [('個別', s['code'], s['name']) for s in DEFAULT_STOCKS]
        + [('ETF', e['code'], e['name']) for e in ETF_UNIVERSE]
        + [('ベースライン', b['code'], b['name']) for b in BASELINE_UNIVERSE]
    )

    all_events = []
    data_status = []
    for group, code, name in universe:
        days = fetch_days(code)
        time.sleep(SLEEP_BETWEEN_CALLS)
        usable = [d for d in days if d['c'] is not None]
        if len(usable) < 210:
            data_status.append({'group': group, 'code': code, 'name': name,
                                 'days': len(usable), 'status': 'データ不足/なし'})
            print(f"  [{group}] {code} {name}: データ不足({len(usable)}日) → スキップ")
            continue
        apply_gate = (group != 'ベースライン')
        evs = scan_events(days, group, apply_gate)
        all_events.extend(evs)
        data_status.append({'group': group, 'code': code, 'name': name,
                            'days': len(usable), 'events': len(evs), 'status': 'OK'})
        print(f"  [{group}] {code} {name}: {len(usable)}日 → イベント{len(evs)}件")

    banded = [e for e in all_events if e['band'] != '[<2)']
    cross = []
    for group in ['個別', 'ETF', 'ベースライン']:
        trends = ['baseline'] if group == 'ベースライン' else ['PO', '資格']
        for tr in trends:
            for lo, hi in PULLBACK_BANDS:
                bl = f'[{lo},{hi if hi < 9999 else "∞"})'
                rows = [e for e in banded if e['group'] == group and e['trend'] == tr and e['band'] == bl]
                if rows:
                    cross.append({'group': group, 'trend': tr, 'band': bl, **cell_summary(rows)})

    pooled = {}
    for group in ['個別', 'ETF', 'ベースライン']:
        trends = ['baseline'] if group == 'ベースライン' else ['PO', '資格']
        for tr in trends:
            rows = [e for e in banded if e['group'] == group and e['trend'] == tr]
            if rows:
                pooled[f'{group}/{tr}'] = cell_summary(rows)

    m_sens = {}
    for key_group in ['個別', 'ETF']:
        for tr in ['PO', '資格']:
            rows = [e for e in banded if e['group'] == key_group and e['trend'] == tr]
            if rows:
                m_sens[f'{key_group}/{tr}'] = {f'recovered_M{M}': _rate([r[f'rec{M}'] for r in rows]) for M in M_LIST}

    partii = analyze_indicators()

    verdict = []
    po_rows = [e for e in banded if e['trend'] == 'PO']
    shik_rows = [e for e in banded if e['trend'] == '資格']
    base_rows = [e for e in banded if e['trend'] == 'baseline']
    po_rec = _rate([r[f'rec{M_PRIMARY}'] for r in po_rows])
    shik_rec = _rate([r[f'rec{M_PRIMARY}'] for r in shik_rows])
    base_rec = _rate([r[f'rec{M_PRIMARY}'] for r in base_rows])
    if None not in (po_rec, base_rec):
        edge = round(po_rec - base_rec, 1)
        clear = (po_rec is not None and base_rec is not None and edge >= 5
                 and (shik_rec is None or po_rec >= shik_rec))
        verdict.append(
            f"(1) PO優位性: PO戻り率(M{M_PRIMARY})={po_rec}% / 資格のみ={shik_rec}% / "
            f"baseline(TOPIX)={base_rec}% (PO−base={edge}pt) → "
            f"{'PO選別に固有の価値あり' if clear else '上げ相場の押し目一般と大差なし(PO固有の価値は弱い)'}"
        )
    else:
        verdict.append("(1) PO優位性: サンプル不足で判定不能")

    ind_po = pooled.get('個別/PO', {})
    etf_po = pooled.get('ETF/PO', {})
    if ind_po and etf_po:
        verdict.append(
            f"(2) ETF vs 個別(PO): 戻り率 ETF={etf_po.get('recovered_pct')}% vs 個別={ind_po.get('recovered_pct')}% / "
            f"崩壊率(broke200) ETF={etf_po.get('broke200_pct')}% vs 個別={ind_po.get('broke200_pct')}% / "
            f"最大DD中央値 ETF={etf_po.get('maxdd_median')}% vs 個別={ind_po.get('maxdd_median')}% → "
            f"{'ETFが安定(仮説どおり)' if (etf_po.get('broke200_pct') or 99) <= (ind_po.get('broke200_pct') or 0) else '個別と大差なし/個別優位'}"
        )
    else:
        verdict.append("(2) ETF vs 個別: どちらかのサンプル不足で判定保留")

    band_rec = []
    for lo, hi in PULLBACK_BANDS:
        bl = f'[{lo},{hi if hi < 9999 else "∞"})'
        rows = [e for e in po_rows if e['band'] == bl]
        band_rec.append((bl, len(rows), _rate([r[f'rec{M_PRIMARY}'] for r in rows])))
    seq = [r for _, nn, r in band_rec if r is not None and nn >= 10]
    mono = len(seq) >= 2 and all(seq[k] >= seq[k + 1] for k in range(len(seq) - 1))
    best_band = max([b for b in band_rec if b[1] >= 10 and b[2] is not None], key=lambda x: x[2], default=None)
    verdict.append(
        f"(3) 押し深さ×戻り率(PO): " + " / ".join(f"{bl}:{r}%(n{nn})" for bl, nn, r in band_rec) +
        f" → {'浅いほど戻りやすい(単調)' if mono else '非単調'}" +
        (f"、最良帯={best_band[0]}" if best_band else "")
    )

    def wl(rows, key):
        w = sum(1 for r in rows if r[key] == 'WIN'); l = sum(1 for r in rows if r[key] == 'LOSE')
        return round(w / (w + l) * 100, 1) if (w + l) else None
    st = wl(po_rows, 'stop_struct'); fx = wl(po_rows, 'stop_fixed7')
    trap = (po_rec is not None and po_rec >= 55 and st is not None and st < 45)
    verdict.append(
        f"(4) 損切り(PO決着勝率): 構造(押し安値下)={st}% vs 固定-7%={fx}% → "
        f"{'構造の方が良い' if (st or 0) > (fx or 0) else '固定-7%と同等/劣位'}。"
        f"{'⚠戻り率は高いのに損切り勝率が低い=戻る前に刈られる罠が発生' if trap else '大きな刈られ罠は見られない'}"
    )

    if partii.get('available'):
        pr = partii['pruning']
        top = partii['perIndicator'][:3]
        top_s = ", ".join(f"{p['key']}({p['corr_vs_N225']})" for p in top)
        verdict.append(
            f"(5) 指標そぎ落とし(n={partii['samples']}日): 相関上位={top_s} / "
            f"削除候補(相関低&重み低)={pr['delete_candidates'] or 'なし'} / "
            f"統合候補(重複r≥0.8)={pr['merge_pairs'] or 'なし'} / 芯(相関≥0.4)={pr['core'] or 'なし'}"
            + ("" if partii['samples'] >= 30 else " ※標本薄く確度低。蓄積後に再評価")
        )
    else:
        verdict.append(f"(5) 指標そぎ落とし: 判定不能 ({partii.get('note')})")

    implementable = (po_rec is not None and base_rec is not None
                     and (po_rec - base_rec) >= 5 and (st or 0) >= 45)
    verdict.append(
        f"(6) 実装可否: {'エッジの芽あり(ただし単一相場・単一セル過学習に注意、別期間の再現確認が必須)' if implementable else 'エッジ薄〜不明、現時点で実装は非推奨'}。"
        f" 本標本は上昇相場に偏り・コスト未考慮のため、数値は楽観バイアスがある前提で解釈すること。"
    )

    report = {
        'generatedAt': datetime.now(JST).isoformat(),
        'note': '計測専用・本番非変更。価格は調整済み(AdjC)、権利落ち日除外。取引コスト/スリッページ未考慮。',
        'structuralGap': '本標本は上昇相場ひと相場ぶんに偏っている可能性が高い。別期間での再現確認を必須とする。',
        'params': {'lookbackDays': LOOKBACK_CALENDAR_DAYS, 'M_list': M_LIST, 'M_primary': M_PRIMARY,
                   'stopHorizon': STOP_HORIZON, 'fixedStopPct': FIXED_STOP_PCT, 'bands': PULLBACK_BANDS},
        'universeStatus': data_status,
        'totalEvents': len(all_events),
        'bandedEvents': len(banded),
        'pooled_group_trend': pooled,
        'mSensitivity_recovered': m_sens,
        'crossTab': cross,
        'partII_indicatorContribution': partii,
        'verdict': verdict,
    }
    with open(os.path.join(SCRIPT_DIR, 'po_backtest_report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)

    print("\n" + "=" * 64)
    print(f"  総イベント {len(all_events)} / 帯対象(押し≥2%) {len(banded)}")
    print("=" * 64)
    print("\n[群×トレンド プール]")
    for k, v in pooled.items():
        print(f"{k:<16} n={v['n']:>4} 戻り率={v['recovered_pct']} 構造勝率={v['stop_struct']['winPct_decided']} 固定7={v['stop_fixed7']['winPct_decided']}")
    print("\n  所見(計測者):")
    for line in verdict:
        print("  " + line)
    print("\n  ✓ po_backtest_report.json 書き出し完了")


# ==================================================================
#  Phase 1.5: アウトオブサンプル(下げ・もみ合い・暴落)モード
# ==================================================================
OOS_FETCH_FROM = '2023-09-01'
REGIME_WINDOWS = [
    ('窓②暴落(2024-08)',        '2024-07-01', '2024-10-31'),
    ('窓①下げもみ合い(2025上期)', '2025-01-01', '2025-06-30'),
    ('上げ相場(参考)',           '2025-10-01', '2100-01-01'),
]


def regime_of(date_str):
    for name, a, b in REGIME_WINDOWS:
        if a <= date_str <= b:
            return name
    return None


def _pooled_and_cross(events):
    banded = [e for e in events if e['band'] != '[<2)']
    pooled = {}
    for group in ['個別', 'ETF', 'ベースライン']:
        trends = ['baseline'] if group == 'ベースライン' else ['PO', '資格']
        for tr in trends:
            rows = [e for e in banded if e['group'] == group and e['trend'] == tr]
            if rows:
                pooled[f'{group}/{tr}'] = cell_summary(rows)
    cross = []
    for group in ['個別', 'ETF', 'ベースライン']:
        trends = ['baseline'] if group == 'ベースライン' else ['PO', '資格']
        for tr in trends:
            for lo, hi in PULLBACK_BANDS:
                bl = f'[{lo},{hi if hi < 9999 else "∞"})'
                rows = [e for e in banded if e['group'] == group and e['trend'] == tr and e['band'] == bl]
                if rows:
                    cross.append({'group': group, 'trend': tr, 'band': bl, **cell_summary(rows)})
    return pooled, cross, banded


def _po_metrics(banded):
    po = [e for e in banded if e['trend'] == 'PO']
    if not po:
        return {'n': 0}
    s = cell_summary(po)
    b25 = [e for e in po if e['band'] == '[2,5)']
    def wl(rows, key):
        w = sum(1 for r in rows if r[key] == 'WIN'); l = sum(1 for r in rows if r[key] == 'LOSE')
        return round(w / (w + l) * 100, 1) if (w + l) else None
    return {
        'n': s['n'],
        'recovered_pct': s['recovered_pct'],
        'fwd_mean': s['fwd_mean'],
        'maxdd_median': s['maxdd_median'],
        'broke200_pct': s['broke200_pct'],
        'stop_struct_win': wl(po, 'stop_struct'),
        'stop_fixed7_win': wl(po, 'stop_fixed7'),
        'recovered_2to5': _rate([r[f'rec{M_PRIMARY}'] for r in b25]) if b25 else None,
        'recovered_2to5_n': len(b25),
        'po_vs_shikaku_recovered': _rate([r[f'rec{M_PRIMARY}'] for r in banded if r['trend'] == '資格']),
    }


def main_oos():
    today = datetime.now(JST).date().strftime('%Y-%m-%d')
    print("=" * 64)
    print("  Stocksurfing Phase1.5 OOS(下げ・もみ合い・暴落)再現確認 (計測専用)")
    print(f"  実行: {datetime.now(JST).isoformat()}  取得: {OOS_FETCH_FROM}〜{today}")
    print("=" * 64)

    universe = (
        [('個別', s['code'], s['name']) for s in DEFAULT_STOCKS]
        + [('ETF', e['code'], e['name']) for e in ETF_UNIVERSE]
        + [('ベースライン', b['code'], b['name']) for b in BASELINE_UNIVERSE]
    )

    all_events = []
    earliest = []
    for group, code, name in universe:
        days = fetch_days(code, frm=OOS_FETCH_FROM, to=today)
        time.sleep(SLEEP_BETWEEN_CALLS)
        usable = [d for d in days if d['c'] is not None]
        first_date = usable[0]['date'] if usable else None
        earliest.append({'group': group, 'code': code, 'name': name,
                         'days': len(usable), 'earliest': first_date})
        if len(usable) < 210:
            print(f"  [{group}] {code} {name}: {len(usable)}日(最古 {first_date}) → SMA200不足でスキップ")
            continue
        apply_gate = (group != 'ベースライン')
        evs = scan_events(days, group, apply_gate)
        for e in evs:
            e['regime'] = regime_of(e['date'])
        all_events.extend(evs)
        print(f"  [{group}] {code} {name}: {len(usable)}日(最古 {first_date}) → イベント{len(evs)}件")

    regimes = {}
    for name, _, _ in REGIME_WINDOWS:
        evs = [e for e in all_events if e.get('regime') == name]
        pooled, cross, banded = _pooled_and_cross(evs)
        regimes[name] = {'events': len(evs), 'bandedEvents': len(banded),
                         'pooled_group_trend': pooled, 'crossTab': cross,
                         'po_summary': _po_metrics(banded)}

    order = ['上げ相場(参考)', '窓①下げもみ合い(2025上期)', '窓②暴落(2024-08)']
    comp_rows = [
        ('PO n(イベント数)', 'n'),
        ('PO戻り率% (M10)', 'recovered_pct'),
        ('PO fwd平均%', 'fwd_mean'),
        ('PO構造損切り勝率%', 'stop_struct_win'),
        ('PO固定-7%勝率%', 'stop_fixed7_win'),
        ('PO崩壊率%(broke200)', 'broke200_pct'),
        ('PO最大DD中央%', 'maxdd_median'),
        ('押し2-5%帯PO戻り率%', 'recovered_2to5'),
        ('資格のみ戻り率%(対比)', 'po_vs_shikaku_recovered'),
    ]
    comparison = []
    for label, key in comp_rows:
        comparison.append({'metric': label,
                           **{name: regimes[name]['po_summary'].get(key) for name in order}})

    def g(name, key):
        return regimes[name]['po_summary'].get(key)
    up, w1, w2 = order
    verdict = []
    verdict.append(
        f"(1) βか実力か: PO戻り率 上げ={g(up,'recovered_pct')}%(n{g(up,'n')}) / "
        f"窓①={g(w1,'recovered_pct')}%(n{g(w1,'n')}) / 窓②={g(w2,'recovered_pct')}%(n{g(w2,'n')})"
    )
    verdict.append(
        f"(2) 損切り頑健性【最重要】: 構造 上げ={g(up,'stop_struct_win')}% / 窓①={g(w1,'stop_struct_win')}% / 窓②={g(w2,'stop_struct_win')}% ; "
        f"固定-7% 上げ={g(up,'stop_fixed7_win')}% / 窓①={g(w1,'stop_fixed7_win')}% / 窓②={g(w2,'stop_fixed7_win')}%"
    )
    verdict.append(
        f"(3) PO優位頑健性: 資格のみ戻り率 上げ={g(up,'po_vs_shikaku_recovered')}% / 窓①={g(w1,'po_vs_shikaku_recovered')}% / 窓②={g(w2,'po_vs_shikaku_recovered')}%"
    )
    verdict.append(
        f"(4) 崩壊率/DD: broke200 上げ={g(up,'broke200_pct')}% / 窓①={g(w1,'broke200_pct')}% / 窓②={g(w2,'broke200_pct')}% ; "
        f"最大DD中央 上げ={g(up,'maxdd_median')}% / 窓①={g(w1,'maxdd_median')}% / 窓②={g(w2,'maxdd_median')}%"
    )
    thin = any((g(n, 'n') or 0) < 15 for n in (w1, w2))
    verdict.append(
        "(5) 総合仕分け: 3期間を通じて生き残った要素のみ実装候補。"
        + (" ⚠下げ窓のサンプルが薄く(n<15のセルあり)確度低。" if thin else "")
        + " ETFは当時未上場でOOS検証不能なら確証は先送り。"
    )

    report = {
        'generatedAt': datetime.now(JST).isoformat(),
        'mode': 'oos',
        'note': '計測専用・本番非変更。ロジックはPhase1と完全同一・期間のみ変更。',
        'fetchFrom': OOS_FETCH_FROM, 'fetchTo': today,
        'regimeWindows': [{'name': n, 'from': a, 'to': b} for n, a, b in REGIME_WINDOWS],
        'earliestByStock': earliest,
        'regimes': regimes,
        'threePeriodComparison_PO': comparison,
        'verdict': verdict,
    }
    with open(os.path.join(SCRIPT_DIR, 'po_backtest_oos_report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)

    print("\n[三期間比較 (PO=個別+ETFプール)]")
    print(f"  {'指標':<24}{'上げ相場':>10}{'窓①下げ':>10}{'窓②暴落':>10}")
    for row in comparison:
        print(f"  {row['metric']:<24}{str(row[order[0]]):>10}{str(row[order[1]]):>10}{str(row[order[2]]):>10}")
    print("\n  所見(計測者):")
    for line in verdict:
        print("  " + line)
    print("\n  ✓ po_backtest_oos_report.json 書き出し完了")


if __name__ == '__main__':
    if os.environ.get('BACKTEST_MODE', 'recent').lower() == 'oos':
        main_oos()
    else:
        main()
