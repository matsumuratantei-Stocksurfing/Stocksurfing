#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
po_detector.py — PO押し目「検出」と「構造損切り」の単一ソース (共有モジュール)

Phase 1 / 1.5 の backtest_perfectorder.py にあった検出ロジックと構造損切りを、
バックテスト(計測)と観測モード(前向き検証)の両方が同じコードを使うために抽出したもの。
検出ロジック・構造損切りの定義はここが唯一の正典。挙動は backtest 版と同一。

【重要・発注禁止】このモジュールは価格系列から検出/決済を計算するだけであり、
証券会社API・発注・注文キューなど、実発注につながるモジュールを一切importしない。

────────────────────────────────────────────────────────────────
■ 時計リセット規約 (logic_version)
  観測モードでは、下記の検出ロジック(iter_detections / PO・資格の定義・
  トリガー条件・pivot/構造損切りの計算)または close_trade(決済ルール)を
  変更した場合、必ず LOGIC_VERSION を上げること。
  格上げ判定の集計は「現行 LOGIC_VERSION のレコードのみ」を対象とし、
  旧バージョンのレコードは参照用に保持するが判定には混ぜない
  (observe_close.py / index.html 側で version でフィルタする)。
  このモジュールを変えたら LOGIC_VERSION を上げる、を鉄則とする。
────────────────────────────────────────────────────────────────
"""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jquants_client import jq_get

JST = timezone(timedelta(hours=9))

# 検出ロジック/構造損切りの版。ここを変えたら必ず上げる(時計リセット規約)。
LOGIC_VERSION = 'po-v1'

# ---- 検出・決済パラメータ(Phase 1 / 1.5 確定値。観測期間中は変更しない) ----
LOOKBACK_CALENDAR_DAYS = 720   # SMA200(200営業日)+助走を賄う
PULLBACK_BANDS = [(2, 5), (5, 8), (8, 12), (12, 9999)]
M_LIST = [5, 10, 20]           # 前方ホライズン(営業日) ※backtestの前方メトリクス用
M_PRIMARY = 10
STOP_HORIZON = 20              # 構造損切りの決着ホライズン(営業日)
FIXED_STOP_PCT = -7.0          # 参考(固定損切り。backtest比較用のみ)


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


def band_label(p):
    for lo, hi in PULLBACK_BANDS:
        if lo <= p < hi:
            return f'[{lo},{hi if hi < 9999 else "∞"})'
    return '[<2)'


# ---------- データ取得 (J-Quants V2 日足) ----------
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
            'vo': _f(r, 'Vo'),   # 取引高(観測モードの volume_snapshot 用。backtestは未使用)
        })
    return out


# ---------- 構造損切りシミュ (WIN/LOSE/EVEN。backtest集計用) ----------
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


# ==================================================================
#  検出の単一ソース: iter_detections
#  各営業日を走査し「押し目イベント」の“検出時点のみ”の属性を yield する。
#  未来のデータは一切参照しない(前向き)。backtest はここに前方メトリクスを足し、
#  観測モードは detect_latest 経由で「今日の検出」だけを使う。
# ==================================================================
def iter_detections(days, apply_gate):
    n = len(days)
    closes = [d['c'] for d in days]
    highs = [d['h'] for d in days]
    lows = [d['l'] for d in days]

    armed = True
    frozen = None  # 発火中イベントの回復目標(高値)。close>=frozen で再アーム

    start = 200  # SMA200が立つまで待つ
    for i in range(start, n):
        c = closes[i]
        if c is None or days[i]['adjFactor'] != 1.0:
            continue
        s5, s25, s75, s200 = _sma(closes, i, 5), _sma(closes, i, 25), _sma(closes, i, 75), _sma(closes, i, 200)
        if None in (s5, s25, s75, s200):
            continue

        seg_h = [h for h in highs[max(0, i - 19):i + 1] if h is not None]
        if not seg_h:
            continue
        pivot20 = max(seg_h)
        pullback = (pivot20 - c) / pivot20 * 100 if pivot20 else 0

        if frozen is not None and c >= frozen:
            armed = True
            frozen = None
        if not armed:
            continue

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

        if c > s25:
            level = '>SMA25'
        elif c > s75:
            level = 'SMA25-75'
        elif c > s200:
            level = 'SMA75-200'
        else:
            level = '<SMA200'

        s25_prev = _sma(closes, i - 10, 25)
        slope = ((s25 - s25_prev) / s25_prev * 100) if (s25_prev and s25_prev != 0) else None

        seg_l = [l for l in lows[max(0, i - 9):i + 1] if l is not None]
        swing_low = min(seg_l) if seg_l else None

        yield {
            'i': i, 'date': days[i]['date'], 'entry': c,
            'pullback': round(pullback, 2), 'band': band_label(pullback),
            'level': level, 'po': po, 'gate': gate,
            'slope25': round(slope, 3) if slope is not None else None,
            'pivot': pivot20, 'swing_low': swing_low,
            'sma5': s5, 'sma25': s25, 'sma75': s75, 'sma200': s200,
        }


# ---------- backtest 用: 検出 + 前方メトリクス ----------
def scan_events(days, group, apply_gate):
    """backtest_perfectorder.py が使う。iter_detections の各検出に前方メトリクスを付す。
    ※前方メトリクスは未来データを使うため、これは“計測(バックテスト)専用”。
    観測モードは絶対にこれを使わず detect_latest を使う。"""
    n = len(days)
    closes = [d['c'] for d in days]
    highs = [d['h'] for d in days]
    lows = [d['l'] for d in days]
    sma200 = [_sma(closes, i, 200) for i in range(n)]

    events = []
    for det in iter_detections(days, apply_gate):
        i = det['i']
        c = det['entry']
        pivot20 = det['pivot']
        swing_low = det['swing_low']
        ev = {
            'group': group, 'date': det['date'], 'entry': c,
            'pullback': det['pullback'], 'band': det['band'],
            'level': det['level'], 'po': det['po'], 'gate': det['gate'],
            'trend': ('baseline' if not apply_gate else ('PO' if det['po'] else '資格')),
            'slope25': det['slope25'], 'pivot': pivot20,
        }
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
        ev['stop_struct'] = sim_stop(c, pivot20, (swing_low * 0.98 if swing_low else None),
                                     highs, lows, i, STOP_HORIZON)
        ev['stop_fixed7'] = sim_stop(c, pivot20, c * (1 + FIXED_STOP_PCT / 100.0),
                                     highs, lows, i, STOP_HORIZON)
        events.append(ev)
    return events


# ==================================================================
#  観測モード用 (前向き): 今日の検出 と 仮想決済
# ==================================================================
def detect_latest(days, apply_gate=True):
    """days の“最終日(=今日)”が新規のPO押し目検出かを判定して返す。
    検出時点(=days の最後まで)のデータのみ使用。未来データ非参照(前向き)。
    検出でなければ None。"""
    if not days:
        return None
    last_idx = len(days) - 1
    latest = None
    for det in iter_detections(days, apply_gate):
        if det['i'] == last_idx:
            latest = det
    return latest


def structural_stop_line(swing_low):
    """構造損切りライン = 押し安値 × 0.98 (Phase 1.5 確定版)。"""
    return swing_low * 0.98 if swing_low is not None else None


def close_trade(entry, pivot, swing_low, fut, horizon=STOP_HORIZON):
    """Phase 1.5 確定版の構造損切りで仮想決済を判定する。
    fut = 検出翌日以降の (low, high, close) を古い順に並べたリスト(実際に経過した営業日のみ)。
    - 先に構造損切りライン割れ(low<=stop) → exit_reason='structural_stop'(exit=stop)
    - 先に回復目標到達(high>=pivot)       → exit_reason='target_recovery'(exit=pivot)
    - horizon(20営業日)到達で未決着       → exit_reason='time_exit'(exit=その日の終値)
    同日両到達は保守的に structural_stop。
    まだ結着せず horizon にも未到達なら None を返す(=まだ open のまま)。
    ※ 決済ルールは Phase 1.5 の構造損切りをそのまま流用。トレーリング等の新ルールは
      追加しない(単一変数の原則)。exit_reason に 'トレーリング' は出力されない。"""
    stop = structural_stop_line(swing_low)
    if entry is None or entry <= 0 or pivot is None or stop is None:
        return None
    for k, bar in enumerate(fut, start=1):
        if k > horizon:
            break
        lo, hi, cl = bar
        if lo is not None and lo <= stop:
            return {'exit_reason': 'structural_stop', 'exit_price': round(stop, 2),
                    'holding_days': k, 'return_pct': round((stop - entry) / entry * 100, 3)}
        if hi is not None and hi >= pivot:
            return {'exit_reason': 'target_recovery', 'exit_price': round(pivot, 2),
                    'holding_days': k, 'return_pct': round((pivot - entry) / entry * 100, 3)}
    # horizon 到達で未決着 → 時間切れ決済(その日の終値)
    if len(fut) >= horizon:
        cl = fut[horizon - 1][2]
        if cl is not None:
            return {'exit_reason': 'time_exit', 'exit_price': round(cl, 2),
                    'holding_days': horizon, 'return_pct': round((cl - entry) / entry * 100, 3)}
    return None  # まだ決着せず open 継続
