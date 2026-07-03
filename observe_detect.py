#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
observe_detect.py — 観測モード 検出ジョブ (前向き・append-only)

夜のジョブ(observe.yml, 23:00 JST)から呼ばれる。個別24銘柄＋セクターETF7の日足を
J-Quantsから取得し、その日にPO押し目検出が発火した銘柄を「観測レコード」として
observations.json に**追記のみ**する(検出時点のデータだけで生成=前向き)。

【前向き性】検出は po_detector.detect_latest を使い、系列の“最終日(=今日)”までの
  データのみで判定する。未来のデータを参照するコードパスは無い。
【不可変性】作成した検出レコードは追記専用。既存レコードの検出時フィールドは触らない。
【発注禁止】発注・注文・証券APIモジュールは一切importしない。
【間引き禁止】その日に多数検出しても全件記録する(選択バイアス防止)。

record スキーマは依頼書に従う(detection_context に地合い・出来高・signal_params)。
"""
import os
import sys
import json
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_STOCKS, calc_score
from po_detector import JST, LOGIC_VERSION, fetch_days, detect_latest, structural_stop_line

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OBS_PATH = os.path.join(SCRIPT_DIR, 'observations.json')
SLEEP_BETWEEN_CALLS = 0.3

# 観測ユニバース: 依頼の仮説「強い銘柄・強いセクターETF」→ 個別24 + セクターETF7
# (レバ/インバースETFはトレンド性が特殊なため対象外)
ETF_UNIVERSE = [
    {'code': '213A', 'name': '半導体ETF-A'}, {'code': '221A', 'name': '半導体ETF-B'},
    {'code': '200A', 'name': '半導体ETF-C'}, {'code': '282A', 'name': '半導体ETF-D'},
    {'code': '2243', 'name': '半導体ETF-E'}, {'code': '513A', 'name': '防衛テックETF'},
    {'code': '1619', 'name': '建設・資材ETF'},
]


def load_obs():
    if not os.path.exists(OBS_PATH):
        return {'meta': {}, 'records': []}
    try:
        with open(OBS_PATH, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if 'records' not in d:
            d = {'meta': {}, 'records': d if isinstance(d, list) else []}
        return d
    except Exception:
        return {'meta': {}, 'records': []}


def market_condition_snapshot():
    """検出時点の地合いを、既存の場スコア(common.calc_score)でそのまま記録する。"""
    path = os.path.join(SCRIPT_DIR, 'data.json')
    if not os.path.exists(path):
        return {'score': None, 'label': 'unknown', 'note': 'data.json なし'}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        score = calc_score(data.get('indicators', {}))
    except Exception:
        return {'score': None, 'label': 'unknown', 'note': 'data.json 読込失敗'}
    if score is None:
        label = 'unknown'
    elif score >= 40:
        label = '強い追い風'
    elif score >= 20:
        label = '追い風'
    elif score > -20:
        label = '中立'
    elif score > -40:
        label = '向かい風'
    else:
        label = '強い逆風'
    # 弱い地合い判定(格上げ基準6の弱地合サブグループ用): 中立以下(<20)を weak とする
    weak = (score is not None and score < 20)
    return {'score': round(score, 1) if score is not None else None, 'label': label, 'weak': weak,
            'fetchedAt': data.get('fetchedAt') if 'data' in dir() else None}


def main():
    now = datetime.now(JST)
    print("=" * 56)
    print(f"  観測モード 検出ジョブ  {now.isoformat()}  logic={LOGIC_VERSION}")
    print("=" * 56)

    obs = load_obs()
    existing = {r.get('record_id') for r in obs['records']}
    # 観測開始日(初回検出時に確定・以後不変)
    if not obs['meta'].get('observation_start'):
        obs['meta']['observation_start'] = now.date().strftime('%Y-%m-%d')
    market = market_condition_snapshot()

    universe = ([('個別', s['code'], s['name']) for s in DEFAULT_STOCKS]
                + [('ETF', e['code'], e['name']) for e in ETF_UNIVERSE])

    added = 0
    for group, code, name in universe:
        days = fetch_days(code)
        time.sleep(SLEEP_BETWEEN_CALLS)
        usable = [d for d in days if d['c'] is not None]
        if len(usable) < 210:
            continue
        det = detect_latest(days, apply_gate=True)
        if not det:
            continue
        rid = f"{det['date']}_{code}"
        if rid in existing:
            continue  # 冪等(同日再実行でも二重記録しない)

        rec = {
            'record_id': rid,
            'logic_version': LOGIC_VERSION,
            'detected_at': now.isoformat(),
            'ticker': code,
            'ticker_name': name,
            'group': group,
            'detection_price': det['entry'],
            'hypothetical_entry': {
                'price': det['entry'],
                'basis': 'PO押し目イベント発火日の調整済み終値(検出ロジックの定義)',
            },
            'structural_stop': structural_stop_line(det['swing_low']),
            'detection_context': {
                'market_condition': market,
                'volume_snapshot': {'volume': usable[-1].get('vo'), 'date': det['date']},
                'signal_params': {
                    'logic_version': LOGIC_VERSION,
                    'po': det['po'], 'gate': det['gate'],
                    'pullback_pct': det['pullback'], 'band': det['band'],
                    'level': det['level'], 'slope25': det['slope25'],
                    'pivot_high': det['pivot'], 'swing_low': det['swing_low'],
                    'sma5': round(det['sma5'], 2), 'sma25': round(det['sma25'], 2),
                    'sma75': round(det['sma75'], 2), 'sma200': round(det['sma200'], 2),
                },
            },
            # close ジョブが baseline を計算する際の起点(検出日・エントリー)
            'baseline_ref': {'detect_date': det['date'], 'entry': det['entry']},
            'status': 'open',
        }
        obs['records'].append(rec)
        existing.add(rid)
        added += 1
        print(f"  検出: {code} {name}  PO={det['po']} 押し{det['pullback']}% 損切{rec['structural_stop']:.1f}")

    obs['meta']['last_detect_run'] = now.isoformat()
    obs['meta']['logic_version'] = LOGIC_VERSION
    with open(OBS_PATH, 'w', encoding='utf-8') as f:
        json.dump(obs, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"\n  ✓ 本日の新規検出 {added} 件 → observations.json (総 {len(obs['records'])} 件)")


if __name__ == '__main__':
    main()
