#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stocksurfing Phase 1 — パーフェクトオーダー押し目の戻り率バックテスト (計測専用)

本番ロジック(index.html / send_email.py / weights.json / 予測ロジック)には一切触れない。
仮説「日足で移動平均が正順(パーフェクトオーダー)の強い銘柄/セクターETFは、
5〜10%の押しを作っても数日〜数週間で戻りやすい」を実データで検証する。

3群を別々に集計:
  (1) 個別24銘柄   : common.py の個別リスト
  (2) セクターETF  : 半導体 213A/221A/200A/282A/2243, 防衛テック 513A, 建設・資材 1619
  (3) ベースライン : TOPIX ETF 1306 (パーフェクトオーダー条件を課さない対照群)

出力: po_backtest_report.json + 人が読む要約(標準出力)。

【重要な限界(結論を過信しないこと)】
本標本は上昇相場ひと相場ぶんに偏っている可能性が高い。下げ相場・もみ合いの
イベントが不足していると戻り率は楽観方向にバイアスがかかる。取引コスト/
スリッページは未考慮。価格は調整済み終値(AdjC)を使用し、権利落ち日は除外。
"""
import os
import sys
import json
import time
import statistics
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_STOCKS, INDICATORS
from jquants_client import jq_get

JST = timezone(timedelta(hours=9))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LOOKBACK_CALENDAR_DAYS = 720   # SMA200(200営業日)+検証期間 を賄う
SLEEP_BETWEEN_CALLS = 0.3
M_LIST = [5, 10, 20]           # 前方ホライズン(営業日)
M_PRIMARY = 10
STOP_HORIZON = 20              # 損切りシミュの決着ホライズン
FIXED_STOP_PCT = -7.0          # 松村の通常ルール(参考)
PULLBACK_BANDS = [(2, 5), (5, 8), (8, 12), (12, 9999)]

# セクターETF(レバ/インバースは対象外)
ETF_UNIVERSE = [
    {'code': '213A', 'name': '半導体ETF-A'}, {'code': '221A', 'name': '半導体ETF-B'},
    {'code': '200A', 'name': '半導体ETF-C'}, {'code': '282A', 'name': '半導体ETF-D'},
    {'code': '2243', 'name': '半導体ETF-E'}, {'code': '513A', 'name': '防衛テックETF'},
    {'code': '1619', 'name': '建設・資材ETF'},
]
BASELINE_UNIVERSE = [{'code': '1306', 'name': 'TOPIX ETF(ベースライン)'}]


# ---------- ヘルパ ----------
def _f(row, *keys):
    for k in keys:
        v = row.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _sma(vals, i, w):
    """i を末尾に含む直近 w 本の単純移動平均。足りなければ None。"""
    if i + 1 < w:
        return None
    seg = vals[i - w + 1:i + 1]
    if any(v is None for v in seg):
        return None
    return sum(seg) / w


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def _rate(flags):
    flags = [f for f in flags if f is not None]
    return round(sum(flags) / len(flags) * 100, 1) if flags else None


def band_label(p):
    for lo, hi in PULLBACK_BANDS:
        if lo <= p < hi:
            return f'[{lo},{hi if hi < 9999 else "∞"})'
    return '[<2)'


# ---------- データ取得 ----------
def fetch_days(code, frm=None, to=None):
    today = datetime.now(JST).date()
    if frm is None:
        frm = (today - timedelta(days=LOOKBACK_CALENDAR_DAYS)).strftime('%Y-%m-%d')
    if to is None:
        to = today.strftime('%Y-%m-%d')
    data = jq_get('/equities/bars/daily', {'code': code, 'from': frm, 'to': to})
    if not data:
        return []
    rows = [r for r in data.get('data', []) if r.get('Date')]
    rows.sort(key=lambda r: r['Date'])
    out = []
    for r in rows:
        af = _f(r, 'AdjFactor')
        out.append({
            'date': r.get('Date'),
            'adjFactor': af if af is not None else 1.0,
            'c': _f(r, 'AdjC', 'C'), 'h': _f(r, 'AdjH', 'H'), 'l': _f(r, 'AdjL', 'L'),
        })
    return out


# ---------- 損切りシミュ ----------
def sim_stop(entry, pivot, stop, highs, lows, i, horizon):
    """entry後、先に stop(安値<=stop)割れ→LOSE、先に pivot 回復(高値>=pivot)→WIN、
    どちらも無ければEVEN。同日両到達は保守的にLOSE。"""
    if entry is None or stop is None or pivot is None:
        return 'EVEN'
    n = len(highs)
    for k in range(1, horizon + 1):
        if i + k >= n:
            break
        lo, hi = lows[i + k], highs[i + k]
        if lo is None or hi is None:
            continue
        if lo <= stop:
            return 'LOSE'
        if hi >= pivot:
            return 'WIN'
    return 'EVEN'


# ---------- 1銘柄→押し目イベント抽出 ----------
def scan_events(days, group, apply_gate):
    n = len(days)
    closes = [d['c'] for d in days]
    highs = [d['h'] for d in days]
    lows = [d['l'] for d in days]
    sma200 = [_sma(closes, i, 200) for i in range(n)]

    events = []
    armed = True
    frozen = None  # 発火中イベントの回復目標(高値)。close>=frozen で再アーム

    start = 200  # SMA200が立つまで待つ
    for i in range(start, n):
        c = closes[i]
        if c is None or days[i]['adjFactor'] != 1.0:
            continue
        s5, s25, s75, s200 = _sma(closes, i, 5), _sma(closes, i, 25), _sma(closes, i, 75), sma200[i]
        if None in (s5, s25, s75, s200):
            continue

        # 直近20営業日(当日含む)の高値
        seg_h = [h for h in highs[max(0, i - 19):i + 1] if h is not None]
        if not seg_h:
            continue
        pivot20 = max(seg_h)
        pullback = (pivot20 - c) / pivot20 * 100 if pivot20 else 0

        # 再アーム: 回復目標に到達(=高値更新)したら次の押しを拾える
        if frozen is not None and c >= frozen:
            armed = True
            frozen = None
        if not armed:
            continue

        # トレンド資格(gate)。ベースラインは課さない
        po = (c > s5 > s25 > s75 > s200)
        gate = (c > s200 and s25 > s75)
        if apply_gate and not gate:
            continue

        trigger = (c < s5) or (pullback >= 2.0)
        if not trigger:
            continue

        # --- 発火 ---
        frozen = pivot20
        armed = False

        # 押し先(どの線まで押したか)
        if c > s25:
            level = '>SMA25'
        elif c > s75:
            level = 'SMA25-75'
        elif c > s200:
            level = 'SMA75-200'
        else:
            level = '<SMA200'

        # SMA25傾き(直近10日変化率%)
        s25_prev = _sma(closes, i - 10, 25)
        slope = ((s25 - s25_prev) / s25_prev * 100) if (s25_prev and s25_prev != 0) else None

        # 押し安値(直近10日安値)→構造損切り
        seg_l = [l for l in lows[max(0, i - 9):i + 1] if l is not None]
        swing_low = min(seg_l) if seg_l else None

        ev = {
            'group': group, 'date': days[i]['date'], 'entry': c,
            'pullback': round(pullback, 2), 'band': band_label(pullback),
            'level': level, 'po': po, 'gate': gate,
            'trend': ('baseline' if not apply_gate else ('PO' if po else '資格')),
            'slope25': round(slope, 3) if slope is not None else None,
            'pivot': pivot20,
        }
        # 前方メトリクス(各M)
        for M in M_LIST:
            if i + M < n and closes[i + M] is not None:
                fwd = (closes[i + M] - c) / c * 100
                rec = 0
                rec_days = None
                dd = 0.0
                broke = 0
                for k in range(1, M + 1):
                    hk, lk, ck, s2k = highs[i + k], lows[i + k], closes[i + k], sma200[i + k]
                    if hk is not None and hk >= pivot20 and rec == 0:
                        rec = 1
                        rec_days = k
                    if lk is not None:
                        dd = min(dd, (lk - c) / c * 100)
                    if ck is not None and s2k is not None and ck < s2k * 0.995:
                        broke = 1
                ev[f'fwd{M}'] = round(fwd, 3)
                ev[f'rec{M}'] = rec
                ev[f'recdays{M}'] = rec_days
                ev[f'dd{M}'] = round(dd, 3)
                ev[f'broke{M}'] = broke
            else:
                for suf in ('fwd', 'rec', 'recdays', 'dd', 'broke'):
                    ev[f'{suf}{M}'] = None

        # 損切りシミュ(決着ホライズン STOP_HORIZON)
        ev['stop_struct'] = sim_stop(c, pivot20, (swing_low * 0.98 if swing_low else None),
                                     highs, lows, i, STOP_HORIZON)
        ev['stop_fixed7'] = sim_stop(c, pivot20, c * (1 + FIXED_STOP_PCT / 100.0),
                                     highs, lows, i, STOP_HORIZON)
        events.append(ev)

    return events


# ---------- 集計 ----------
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
    """None を除いたペアでピアソン相関を返す。(corr, n)。"""
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
    """verification_log.json から、各指標の朝chgPct(逆相関は符号反転)と
    当日日経実勢%の相関・方向一致率を測り、現行重みと対比。多重共線性も出す。"""
    path = os.path.join(SCRIPT_DIR, 'verification_log.json')
    if not os.path.exists(path):
        return {'available': False, 'note': 'verification_log.json が見つからない'}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            log = json.load(f)
    except Exception as e:
        return {'available': False, 'note': f'読込失敗: {e}'}

    rows = []  # (y=N225実勢chgPct, morningIndicators dict)
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

    # 多重共線性(指標同士の生chgPct相関、|r|>=0.8を重複として抽出)
    high_pairs = []
    for a in range(len(keys)):
        for b in range(a + 1, len(keys)):
            r_ab, nn = _pearson([r[1].get(keys[a]) for r in rows],
                                [r[1].get(keys[b]) for r in rows])
            if r_ab is not None and abs(r_ab) >= 0.8:
                high_pairs.append({'pair': [keys[a], keys[b]], 'corr': r_ab, 'n': nn})
    high_pairs.sort(key=lambda p: abs(p['corr']), reverse=True)

    # そぎ落とし提案: (a)相関低&重み低=削除, (b)重複=統合, (c)相関高=芯
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

    # --- クロス集計: 群 × 押し深さ帯 × トレンド種別 ---
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

    # --- 群×トレンドのプール(帯まとめ) ---
    pooled = {}
    for group in ['個別', 'ETF', 'ベースライン']:
        trends = ['baseline'] if group == 'ベースライン' else ['PO', '資格']
        for tr in trends:
            rows = [e for e in banded if e['group'] == group and e['trend'] == tr]
            if rows:
                pooled[f'{group}/{tr}'] = cell_summary(rows)

    # --- M感度(戻り率) 群×トレンド ---
    m_sens = {}
    for key_group in ['個別', 'ETF']:
        for tr in ['PO', '資格']:
            rows = [e for e in banded if e['group'] == key_group and e['trend'] == tr]
            if rows:
                m_sens[f'{key_group}/{tr}'] = {f'recovered_M{M}': _rate([r[f'rec{M}'] for r in rows]) for M in M_LIST}

    # ---------- パートII: 指標貢献度 ----------
    partii = analyze_indicators()

    # ---------- 所見(データ駆動・6点) ----------
    def rp(key):
        return (pooled.get(key) or {}).get('recovered_pct')

    verdict = []
    # (1) PO vs 資格 vs baseline (個別+ETF PO をプール)
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

    # (2) ETF vs 個別
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

    # (3) 押し深さと戻り率(PO・個別+ETF)
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

    # (4) 構造損切り vs 固定-7% (PO)
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

    # (5) パートII: 指標そぎ落とし
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

    # (6) 総括
    implementable = (po_rec is not None and base_rec is not None
                     and (po_rec - base_rec) >= 5 and (st or 0) >= 45)
    verdict.append(
        f"(6) 実装可否: {'エッジの芽あり(ただし単一相場・単一セル過学習に注意、別期間の再現確認が必須)' if implementable else 'エッジ薄〜不明、現時点で実装は非推奨'}。"
        f" 本標本は上昇相場に偏り・コスト未考慮のため、数値は楽観バイアスがある前提で解釈すること。"
    )

    report = {
        'generatedAt': datetime.now(JST).isoformat(),
        'note': '計測専用・本番非変更。価格は調整済み(AdjC)、権利落ち日除外。取引コスト/スリッページ未考慮。',
        'structuralGap': '本標本は上昇相場ひと相場ぶんに偏っている可能性が高い。下げ/もみ合い相場のイベント不足により戻り率は楽観方向にバイアス。結論を過信しないこと。別期間での再現確認を必須とする。',
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

    # ---------- 人が読む要約 ----------
    print("\n" + "=" * 64)
    print(f"  総イベント {len(all_events)} / 帯対象(押し≥2%) {len(banded)}")
    print("  ※構造的限界: 上昇相場偏り・コスト未考慮。戻り率は楽観バイアス前提。")
    print("=" * 64)
    print("\n[群×トレンド プール] (戻り率=M10で高値回復した割合)")
    hdr = f"{'群/トレンド':<16}{'n':>5}{'戻り率':>8}{'fwd平均':>9}{'DD中央':>9}{'崩壊率':>8}{'構造勝率':>9}{'固定-7勝率':>10}"
    print(hdr)
    for k, v in pooled.items():
        print(f"{k:<16}{v['n']:>5}{str(v['recovered_pct']):>8}{str(v['fwd_mean']):>9}"
              f"{str(v['maxdd_median']):>9}{str(v['broke200_pct']):>8}"
              f"{str(v['stop_struct']['winPct_decided']):>9}{str(v['stop_fixed7']['winPct_decided']):>10}")

    print("\n[クロス集計 群×押し深さ帯×トレンド]")
    print(f"{'群':<7}{'トレンド':<9}{'帯':<9}{'n':>5}{'戻り率':>8}{'fwd平均':>9}{'DD中央':>9}{'崩壊率':>8}{'構造勝率':>9}")
    for c in cross:
        print(f"{c['group']:<7}{c['trend']:<9}{c['band']:<9}{c['n']:>5}{str(c['recovered_pct']):>8}"
              f"{str(c['fwd_mean']):>9}{str(c['maxdd_median']):>9}{str(c['broke200_pct']):>8}"
              f"{str(c['stop_struct']['winPct_decided']):>9}")

    print("\n[戻り率のM感度]")
    for k, v in m_sens.items():
        print(f"  {k:<12} " + " ".join(f"{kk}={vv}%" for kk, vv in v.items()))

    print("\n" + "=" * 64)
    print("  パートII: 場の判定12指標の貢献度ランキング")
    print("=" * 64)
    if partii.get('available'):
        print(f"  標本 {partii['samples']}日" + ("" if partii['samples'] >= 30 else "  ※標本薄・確度低"))
        print(f"  {'指標':<8}{'重み':>6}{'逆相関':>7}{'対N225相関':>11}{'方向一致%':>10}{'n':>5}")
        for p in partii['perIndicator']:
            print(f"  {p['key']:<8}{p['weight']:>6}{('◯' if p['inverse'] else '-'):>7}"
                  f"{str(p['corr_vs_N225']):>11}{str(p['direction_hit_pct']):>10}{p['n']:>5}")
        hp = partii['multicollinearity_highPairs']
        print("\n  多重共線性(|r|≥0.8の重複ペア):")
        if hp:
            for h in hp:
                print(f"    {h['pair'][0]}↔{h['pair'][1]}  r={h['corr']} (n={h['n']})")
        else:
            print("    強い重複ペアなし(または標本不足)")
        pr = partii['pruning']
        print(f"\n  削除候補: {pr['delete_candidates'] or 'なし'}")
        print(f"  統合候補: {pr['merge_pairs'] or 'なし'}")
        print(f"  芯(残すべき): {pr['core'] or 'なし'}")
    else:
        print(f"  判定不能: {partii.get('note')}")

    print("\n" + "=" * 64)
    print("  所見(計測者):")
    for line in verdict:
        print("  " + line)
    print("=" * 64)
    print("\n  ✓ po_backtest_report.json 書き出し完了")


# ==================================================================
#  Phase 1.5: アウトオブサンプル(下げ・もみ合い・暴落)モード
#  ※ ロジックは Phase 1 と完全同一。期間(イベントの振り分け窓)だけ変える。
# ==================================================================
OOS_FETCH_FROM = '2023-09-01'   # SMA200 助走を賄う一括取得の起点
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
    """あるレジームのイベント群 → pooled(群/トレンド) と crossTab を返す。"""
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
    """PO(個別+ETF)プールの主要指標を返す(三期間比較用)。"""
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
    print("  ※ロジックはPhase1と完全同一。期間の振り分けのみ変更。")
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

    # レジーム別集計
    regimes = {}
    for name, _, _ in REGIME_WINDOWS:
        evs = [e for e in all_events if e.get('regime') == name]
        pooled, cross, banded = _pooled_and_cross(evs)
        regimes[name] = {'events': len(evs), 'bandedEvents': len(banded),
                         'pooled_group_trend': pooled, 'crossTab': cross,
                         'po_summary': _po_metrics(banded)}

    # 三期間比較(PO 個別+ETFプール)
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

    # ---------- 所見(5点) ----------
    def g(name, key):
        return regimes[name]['po_summary'].get(key)
    up, w1, w2 = order
    verdict = []
    verdict.append(
        f"(1) βか実力か: PO戻り率 上げ={g(up,'recovered_pct')}%(n{g(up,'n')}) / "
        f"窓①={g(w1,'recovered_pct')}%(n{g(w1,'n')}) / 窓②={g(w2,'recovered_pct')}%(n{g(w2,'n')}) → "
        "下げ窓で大きく低下ならPhase1の高戻り率はβ。維持ならトレンドフォローの実力寄り"
    )
    verdict.append(
        f"(2) 損切り頑健性【最重要】: 構造損切り勝率 上げ={g(up,'stop_struct_win')}% / "
        f"窓①={g(w1,'stop_struct_win')}% / 窓②={g(w2,'stop_struct_win')}% ; "
        f"固定-7% 上げ={g(up,'stop_fixed7_win')}% / 窓①={g(w1,'stop_fixed7_win')}% / 窓②={g(w2,'stop_fixed7_win')}% → "
        "窓②暴落でも構造>固定が生き残れば本物、崩壊なら『押し安値を割らなかっただけ』"
    )
    verdict.append(
        f"(3) PO優位頑健性: 資格のみ戻り率 上げ={g(up,'po_vs_shikaku_recovered')}% / "
        f"窓①={g(w1,'po_vs_shikaku_recovered')}% / 窓②={g(w2,'po_vs_shikaku_recovered')}% "
        f"(各PO戻り率と比較) → PO>資格 が下げ窓でも維持されるか"
    )
    verdict.append(
        f"(4) 崩壊率/DD: broke200 上げ={g(up,'broke200_pct')}% / 窓①={g(w1,'broke200_pct')}% / 窓②={g(w2,'broke200_pct')}% ; "
        f"最大DD中央 上げ={g(up,'maxdd_median')}% / 窓①={g(w1,'maxdd_median')}% / 窓②={g(w2,'maxdd_median')}% → 実運用で耐えられる水準か"
    )
    thin = any((g(n, 'n') or 0) < 15 for n in (w1, w2))
    verdict.append(
        "(5) 総合仕分け: 3期間を通じて生き残った要素のみ実装候補、上げ相場でしか成立しない要素はβ・不採用。"
        + (" ⚠下げ窓のサンプルが薄く(n<15のセルあり)確度低・過信禁物。" if thin else "")
        + " ETFは当時未上場でOOS検証不能なら確証は先送り。"
    )

    report = {
        'generatedAt': datetime.now(JST).isoformat(),
        'mode': 'oos',
        'note': '計測専用・本番非変更。ロジックはPhase1と完全同一・期間のみ変更。調整済み終値・権利落ち除外・コスト未考慮。',
        'structuralGap': '窓①②は短期間でイベント数が少なく、薄いセルの数字は過信しない。セクターETFは当時未上場が多くOOS検証がほぼ不能な可能性。取引コスト/スリッページ未考慮。',
        'fetchFrom': OOS_FETCH_FROM, 'fetchTo': today,
        'regimeWindows': [{'name': n, 'from': a, 'to': b} for n, a, b in REGIME_WINDOWS],
        'earliestByStock': earliest,
        'regimes': regimes,
        'threePeriodComparison_PO': comparison,
        'verdict': verdict,
    }
    with open(os.path.join(SCRIPT_DIR, 'po_backtest_oos_report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)

    # ---------- 人が読む要約 ----------
    print("\n[データ取得可否: 銘柄別 最古日]")
    for e in earliest:
        flag = '' if e['days'] >= 210 else '  ←SMA200不足でスキップ'
        print(f"  [{e['group']}] {e['code']} {e['name']:<14} {e['days']:>4}日  最古 {e['earliest']}{flag}")

    print("\n[三期間比較 (PO=個別+ETFプール)]")
    print(f"  {'指標':<24}{'上げ相場':>10}{'窓①下げ':>10}{'窓②暴落':>10}")
    for row in comparison:
        print(f"  {row['metric']:<24}{str(row[order[0]]):>10}{str(row[order[1]]):>10}{str(row[order[2]]):>10}")

    print("\n[レジーム別 群×トレンド プール]")
    for rname in order:
        print(f"\n  ◆ {rname} (イベント {regimes[rname]['events']})")
        for k, v in regimes[rname]['pooled_group_trend'].items():
            print(f"    {k:<16} n={v['n']:>4} 戻り率={v['recovered_pct']} "
                  f"構造勝率={v['stop_struct']['winPct_decided']} 固定7勝率={v['stop_fixed7']['winPct_decided']} "
                  f"崩壊率={v['broke200_pct']} DD中央={v['maxdd_median']}")

    print("\n" + "=" * 64)
    print("  所見(計測者):")
    for line in verdict:
        print("  " + line)
    print("=" * 64)
    print("\n  ✓ po_backtest_oos_report.json 書き出し完了")


if __name__ == '__main__':
    if os.environ.get('BACKTEST_MODE', 'recent').lower() == 'oos':
        main_oos()
    else:
        main()
