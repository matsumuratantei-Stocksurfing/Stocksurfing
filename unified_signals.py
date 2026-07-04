#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
unified_signals.py — 松村式一本化戦略v1 の「本日の買い候補」生成 (前向き・発注なし)

backtest_unified.py で数値確定した v1 ルールを、毎日ライブで出す。
アプリの「🎯一本化」タブが unified_signals.json を読むだけで、
銘柄選定→買い(エントリー/損切り/利確/トレール)→株数(リスク2%)まで表示できる。

【発注禁止】証券API・発注モジュールは一切importしない。判定を出すだけ。
【前向き】各銘柄は系列の最終日(=直近確定営業日)までのデータのみで判定。未来非参照。
【不可変】unified_signals.json は today と append-only の history を持つ(検証用に日次で蓄積)。
検出・損切りの定義は po_detector と同じ思想(構造損切り)。ロジック変更時は LOGIC_VERSION を上げる。

── 一本化戦略v1(確定) ──
  地合い : TOPIX(1306) 終値 > TOPIX 200日線
  入口   : 終値>SMA200 かつ SMA200上向き かつ (52週新高値 or 52週高値の3%以内)
  損切り : 構造 = 直近10日安値 × 0.98
  利確   : +5%で半分利確 → 残りは建値ストップ&25日線割れでトレール(最長40営業日) ※アプリ側で案内
  株数   : 1トレードのリスク = 総資金 × RISK_PCT を、(エントリー-損切り)幅で割って算出
"""
import os
import sys
import json
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_STOCKS
from po_detector import JST, fetch_days, _sma

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(SCRIPT_DIR, 'unified_signals.json')
SLEEP_BETWEEN_CALLS = 0.3

LOGIC_VERSION = 'unified-v1'
W52 = 250
NEAR_HIGH_PCT = 3.0
TP_PCT = 5.0
RISK_PCT_DEFAULT = 2.0      # 松村さん確定(積極的)。アプリ側で変更可
ATR_WIN = 20
TOPIX_CODE = '1306'

ETF_UNIVERSE = [
    {'code': '213A', 'name': '半導体ETF-A'}, {'code': '221A', 'name': '半導体ETF-B'},
    {'code': '200A', 'name': '半導体ETF-C'}, {'code': '282A', 'name': '半導体ETF-D'},
    {'code': '2243', 'name': '半導体ETF-E'}, {'code': '513A', 'name': '防衛テックETF'},
    {'code': '1619', 'name': '建設・資材ETF'},
]


def _atr_last(highs, lows, closes, win=ATR_WIN):
    n = len(closes)
    if n < win + 1:
        return None
    trs = []
    for i in range(n - win, n):
        if highs[i] is None or lows[i] is None or closes[i - 1] is None:
            return None
        pc = closes[i - 1]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    return sum(trs) / win


def latest_signal(days, regime_on):
    """系列の最終日が v1 の買い候補かを判定。候補ならエントリー/損切り等の辞書、違えば None。"""
    n = len(days)
    if n < W52 + 5:
        return None
    closes = [d['c'] for d in days]
    highs = [d['h'] for d in days]
    lows = [d['l'] for d in days]
    i = n - 1
    c = closes[i]
    if c is None or c <= 0 or days[i]['adjFactor'] != 1.0:
        return None

    s200 = _sma(closes, i, 200)
    s200p = _sma(closes, i - 20, 200)
    s25 = _sma(closes, i, 25)
    if None in (s200, s200p, s25):
        return None
    stock_up = c > s200 and s200 > s200p

    prior_seg = [h for h in highs[max(0, i - W52):i] if h is not None]
    prior52 = max(prior_seg) if len(prior_seg) >= 200 else None
    if prior52 is None:
        return None
    hi = highs[i]
    is_new52 = hi is not None and hi >= prior52
    dist_pct = (prior52 - c) / prior52 * 100  # 52週高値まで下に何%
    near_high = dist_pct <= NEAR_HIGH_PCT
    trigger = is_new52 or near_high

    if not (regime_on and stock_up and trigger):
        return None

    seg_l = [l for l in lows[max(0, i - 9):i + 1] if l is not None]
    swing_low = min(seg_l) if seg_l else None
    if not swing_low:
        return None
    stop = round(swing_low * 0.98, 1)
    if stop >= c:
        return None
    target = round(c * (1 + TP_PCT / 100), 1)
    atr = _atr_last(highs, lows, closes)
    return {
        'entry_ref': round(c, 1),                 # エントリー目安(直近終値。実約定は寄り後)
        'date': days[i]['date'],
        'structural_stop': stop,
        'stop_pct': round((stop - c) / c * 100, 2),
        'target_half': target,                    # +5%半利確ライン
        'target_pct': TP_PCT,
        'trail_line_sma25': round(s25, 1),        # 残りはこの線割れでトレール
        'is_new_52w_high': bool(is_new52),
        'dist_from_52w_high_pct': round(dist_pct, 2),
        'atr20': round(atr, 1) if atr else None,
        'sma200': round(s200, 1),
    }


def load_prev():
    if not os.path.exists(OUT_PATH):
        return {'meta': {}, 'today': {}, 'history': []}
    try:
        with open(OUT_PATH, 'r', encoding='utf-8') as f:
            d = json.load(f)
        d.setdefault('history', [])
        return d
    except Exception:
        return {'meta': {}, 'today': {}, 'history': []}


def main():
    now = datetime.now(JST)
    print("=" * 60)
    print(f"  松村式一本化戦略 シグナル生成  {now.isoformat()}  {LOGIC_VERSION}")
    print("=" * 60)

    # 地合い: TOPIX 200日線
    topix = fetch_days(TOPIX_CODE)
    time.sleep(SLEEP_BETWEEN_CALLS)
    tcl = [d['c'] for d in topix]
    regime_on = False
    topix_date = None
    if topix:
        i = len(topix) - 1
        tsma200 = _sma(tcl, i, 200)
        topix_date = topix[i]['date']
        if tcl[i] is not None and tsma200 is not None:
            regime_on = tcl[i] > tsma200
    print(f"  地合い(TOPIX>200日線): {'ON(追い風)' if regime_on else 'OFF(様子見)'}  基準日 {topix_date}")

    universe = ([('個別', s['code'], s['name']) for s in DEFAULT_STOCKS]
                + [('ETF', e['code'], e['name']) for e in ETF_UNIVERSE])

    candidates = []
    for group, code, name in universe:
        days = fetch_days(code)
        time.sleep(SLEEP_BETWEEN_CALLS)
        usable = [d for d in days if d['c'] is not None]
        if len(usable) < (W52 + 5):
            continue
        sig = latest_signal(days, regime_on)
        if not sig:
            continue
        candidates.append({'ticker': code, 'ticker_name': name, 'group': group, **sig})
        tag = '★新高値' if sig['is_new_52w_high'] else ('高値-' + str(sig['dist_from_52w_high_pct']) + '%')
        print(f"  候補: {code} {name}  目安{sig['entry_ref']} 損切{sig['structural_stop']}"
              f"({sig['stop_pct']}%) 利確{sig['target_half']} {tag}")

    # 距離が近い(強い)順
    candidates.sort(key=lambda x: x['dist_from_52w_high_pct'])

    out = load_prev()
    today = {
        'date': (topix_date or now.date().strftime('%Y-%m-%d')),
        'regime_on': regime_on,
        'n_candidates': len(candidates),
        'candidates': candidates,
    }
    out['meta'] = {
        'generatedAt': now.isoformat(),
        'logic_version': LOGIC_VERSION,
        'strategy': '松村式一本化v1: 地合い(TOPIX>200MA)×上昇トレンド新高値/3%以内×構造損切り×+5%半利確→SMA25トレール',
        'risk_pct_default': RISK_PCT_DEFAULT,
        'params': {'W52': W52, 'nearHighPct': NEAR_HIGH_PCT, 'tpPct': TP_PCT},
        'note': '検証中(前向き実測と併走)。発注は行われません。エントリーは寄り後、目安は直近終値。',
    }
    out['today'] = today
    # append-only 履歴(同日は上書きせず、無ければ追加)
    if not any(h.get('date') == today['date'] for h in out['history']):
        out['history'].append({'date': today['date'], 'regime_on': regime_on,
                               'tickers': [c['ticker'] for c in candidates]})
    out['history'] = out['history'][-400:]

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"\n  ✓ 本日の一本化候補 {len(candidates)} 件 → unified_signals.json")


if __name__ == '__main__':
    main()
