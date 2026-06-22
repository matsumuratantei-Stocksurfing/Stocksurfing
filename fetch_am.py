#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
松村式Stocksurfing - 前場4本値→「後場の入り」判定 (v3.3 / J-Quants Premium)

正午すぎ(12:05 JST)に GitHub Actions から起動。
J-Quants の前場4本値(prices_am, Premium限定)を全監視銘柄について取得し、
「前場で上げ切った(後場は追わない)／押し目買い候補／反発待ち」などを判定して
am.json に書き出す。PWAの「場中」タブが am.json を読んで表示する。

狙い: 奥様の悩み「寄りで上げてしまい、そこから横横で飛び乗れない」に、
      J-Quantsの確実なデータ(リアルタイムではないが正午に確定)で直接答える。
"""
import json
import os
import sys
import math
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jquants_client import get_prices_am

JST = timezone(timedelta(hours=9))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_STOCKS = [
    {'code':'285A', 'name':'キオクシア'},      {'code':'8035', 'name':'東京エレクトロン'},
    {'code':'6857', 'name':'アドバンテスト'},  {'code':'6920', 'name':'レーザーテック'},
    {'code':'6146', 'name':'ディスコ'},        {'code':'4062', 'name':'イビデン'},
    {'code':'6981', 'name':'村田製作所'},      {'code':'5803', 'name':'フジクラ'},
    {'code':'5801', 'name':'古河電工'},        {'code':'4063', 'name':'信越化学'},
    {'code':'5016', 'name':'JX金属'},          {'code':'9984', 'name':'ソフトバンクG'},
    {'code':'7011', 'name':'三菱重工業'},      {'code':'7012', 'name':'川崎重工業'},
    {'code':'7013', 'name':'IHI'},             {'code':'6501', 'name':'日立製作所'},
    {'code':'6758', 'name':'ソニーG'},         {'code':'7203', 'name':'トヨタ自動車'},
    {'code':'6506', 'name':'安川電機'},        {'code':'8058', 'name':'三菱商事'},
    {'code':'8031', 'name':'三井物産'},        {'code':'8306', 'name':'三菱UFJ FG'},
    {'code':'8316', 'name':'三井住友FG'},      {'code':'9107', 'name':'川崎汽船'},
]


def _num(x):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def pick_morning(rec):
    """レスポンス1件から前場 O/H/L/C を取り出す(フィールド名の差異に耐える)。"""
    def g(*keys):
        for k in keys:
            if k in rec and rec[k] is not None:
                return _num(rec[k])
        return None
    o = g('MorningOpen', 'O', 'Open')
    h = g('MorningHigh', 'H', 'High')
    l = g('MorningLow', 'L', 'Low')
    c = g('MorningClose', 'C', 'Close')
    return o, h, l, c


def latest_record(data):
    """prices_am レスポンスから最新日の1件を返す。"""
    if not data:
        return None
    arr = data.get('prices_am') or data.get('data') or []
    if not arr:
        return None
    try:
        arr = sorted(arr, key=lambda r: r.get('Date', ''), reverse=True)
    except Exception:
        pass
    return arr[0]


def judge(o, h, l, c):
    """前場の値動きから「後場の入り」を判定。"""
    if None in (o, h, l, c) or h == l or o == 0:
        return {'label': 'データ不足', 'cls': 'sub', 'note': ''}
    chg = (c - o) / o * 100        # 前場 寄り→引け
    pos = (c - l) / (h - l)        # 0=安値引け, 1=高値引け
    rng = (h - l) / o * 100        # 前場の値幅
    note = f'前場 {chg:+.1f}% / 値幅{rng:.1f}% / 引け位置{pos*100:.0f}%'
    if chg >= 2.5 or (chg >= 1.2 and pos >= 0.8):
        return {'label': '買われすぎ気味⚠ 後場は追わない', 'cls': 'down', 'note': note}
    if chg >= 1.2 and pos >= 0.6:
        return {'label': '高値圏・飛び乗り注意', 'cls': 'down', 'note': note}
    if chg <= -2.0 and pos <= 0.3:
        return {'label': '前場軟調→反発待ち(無理しない)', 'cls': 'sub', 'note': note}
    if chg > 0.4 and 0.3 <= pos <= 0.65:
        return {'label': '上げて押し目→後場の押し目買い候補', 'cls': 'up', 'note': note}
    if abs(chg) <= 0.5:
        return {'label': '前場もみ合い→方向待ち', 'cls': 'sub', 'note': note}
    if chg > 0:
        return {'label': '堅調・許容範囲', 'cls': 'sub', 'note': note}
    return {'label': 'やや軟調・様子見', 'cls': 'sub', 'note': note}


def main():
    today = datetime.now(JST).date().strftime('%Y-%m-%d')
    print(f"[fetch_am] {datetime.now(JST).isoformat()} 前場4本値→後場判定")
    items, success = [], 0
    for st in DEFAULT_STOCKS:
        data = get_prices_am(st['code'], today) or get_prices_am(st['code'])
        rec = latest_record(data)
        if not rec:
            print(f"  {st['code']} {st['name']}: 取得失敗")
            items.append({'code': st['code'], 'name': st['name'], 'ok': False})
            continue
        o, h, l, c = pick_morning(rec)
        v = judge(o, h, l, c)
        if o is None:
            items.append({'code': st['code'], 'name': st['name'], 'ok': False})
            continue
        success += 1
        items.append({
            'code': st['code'], 'name': st['name'], 'ok': True,
            'morningClose': c, 'morningOpen': o, 'morningHigh': h, 'morningLow': l,
            'label': v['label'], 'cls': v['cls'], 'note': v['note'],
        })
        print(f"  {st['code']} {st['name']:12s}: {v['label']} ({v['note']})")

    order = {'up': 0, 'sub': 1, 'down': 2}
    items.sort(key=lambda x: order.get(x.get('cls'), 1))

    out = {
        'generatedAt': datetime.now(JST).isoformat(),
        'date': today,
        'success': success,
        'total': len(DEFAULT_STOCKS),
        'items': items,
    }
    path = os.path.join(SCRIPT_DIR, 'am.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"  ✓ am.json 書き出し (成功 {success}/{len(DEFAULT_STOCKS)})")


if __name__ == '__main__':
    main()
