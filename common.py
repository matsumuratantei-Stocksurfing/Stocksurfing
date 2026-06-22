#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
松村式Stocksurfing - 共通定義モジュール (v3.4)

これまで fetch_data.py / verify_predictions.py / fetch_am.py / send_email.py に
コピペ重複していた銘柄リスト・指標重み・スコア計算ロジックを一元化する。
定義のドリフト(特に銘柄リストのズレ)を防ぎ、重み変更を1か所で完結させるのが狙い。

DEFAULT_STOCKS は HTML(index.html) の DEFAULT_STOCKS と同期させること。
"""
import os
import json
import math

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------- 監視銘柄 (HTML の DEFAULT_STOCKS と同期) ----------
DEFAULT_STOCKS = [
    {'code': '285A', 'name': 'キオクシア',       'tags': ['SOX', 'NQ']},
    {'code': '8035', 'name': '東京エレクトロン',  'tags': ['SOX', 'NQ']},
    {'code': '6857', 'name': 'アドバンテスト',    'tags': ['SOX', 'NQ']},
    {'code': '6920', 'name': 'レーザーテック',    'tags': ['SOX', 'NQ']},
    {'code': '6146', 'name': 'ディスコ',          'tags': ['SOX', 'NQ']},
    {'code': '4062', 'name': 'イビデン',          'tags': ['SOX', 'NQ']},
    {'code': '6981', 'name': '村田製作所',        'tags': ['SOX', '景気']},
    {'code': '5803', 'name': 'フジクラ',          'tags': ['AIインフラ', 'NQ']},
    {'code': '5801', 'name': '古河電工',          'tags': ['AIインフラ', 'NQ']},
    {'code': '4063', 'name': '信越化学',          'tags': ['SOX', '景気']},
    {'code': '5016', 'name': 'JX金属',            'tags': ['SOX', '資源']},
    {'code': '9984', 'name': 'ソフトバンクG',     'tags': ['NQ', '日経寄与']},
    {'code': '7011', 'name': '三菱重工業',        'tags': ['防衛', '重工']},
    {'code': '7012', 'name': '川崎重工業',        'tags': ['防衛', '造船']},
    {'code': '7013', 'name': 'IHI',               'tags': ['防衛', '宇宙']},
    {'code': '6501', 'name': '日立製作所',        'tags': ['NY', '景気']},
    {'code': '6758', 'name': 'ソニーG',           'tags': ['NY', 'NQ', '景気']},
    {'code': '7203', 'name': 'トヨタ自動車',      'tags': ['NY', 'USDJPY', '景気']},
    {'code': '6506', 'name': '安川電機',          'tags': ['フィジカルAI', 'NQ']},
    {'code': '8058', 'name': '三菱商事',          'tags': ['商社', '資源']},
    {'code': '8031', 'name': '三井物産',          'tags': ['商社', '資源']},
    {'code': '8306', 'name': '三菱UFJ FG',        'tags': ['金融', 'USDJPY']},
    {'code': '8316', 'name': '三井住友FG',        'tags': ['金融', 'USDJPY']},
    {'code': '9107', 'name': '川崎汽船',          'tags': ['資源', '景気']},
]

# 決算カレンダー等で「コードのみ」必要な箇所はこれを使う(DEFAULT_STOCKSから自動生成)
TRACKED_STOCKS = [s['code'] for s in DEFAULT_STOCKS]

# ---------- 場の強弱スコア用 指標と重み (v3.1) ----------
INDICATORS = [
    {'key': 'N225F',  'weight': 3.0, 'inverse': False},
    {'key': 'TOPX',   'weight': 2.5, 'inverse': False},
    {'key': 'NDX',    'weight': 2.0, 'inverse': False},
    {'key': 'SPX',    'weight': 1.8, 'inverse': False},
    {'key': 'SOX',    'weight': 2.0, 'inverse': False},
    {'key': 'DJI',    'weight': 1.5, 'inverse': False},
    {'key': 'USDJPY', 'weight': 1.5, 'inverse': False},
    {'key': 'EURJPY', 'weight': 0.8, 'inverse': False},
    {'key': 'TNX',    'weight': 0.8, 'inverse': True},
    {'key': 'WTI',    'weight': 0.6, 'inverse': False},
    {'key': 'VIX',    'weight': 0.8, 'inverse': True},
    {'key': 'NKVI',   'weight': 1.0, 'inverse': True},
]

TAG_MAP = {
    'SOX': 'SOX', 'NQ': 'NDX', 'NY': 'DJI', 'USDJPY': 'USDJPY',
    '景気': 'SPX', '内需': 'TOPX', '金融': 'TNX', '商社': 'WTI', '資源': 'WTI',
    '日経寄与': 'N225F', '防衛': 'N225F', '造船': 'N225F', '重工': 'SPX',
    '宇宙': 'NDX', 'AIインフラ': 'NDX', 'フィジカルAI': 'NDX',
}

NAME_MAP = {
    'N225F': '日経先物', 'TOPX': 'TOPIX', 'NDX': 'ナスダック100', 'SPX': 'S&P500',
    'SOX': 'SOX半導体', 'DJI': 'NYダウ', 'USDJPY': 'ドル円', 'EURJPY': 'ユーロ円',
    'TNX': '米10年金利', 'WTI': 'WTI原油', 'VIX': 'VIX恐怖指数', 'NKVI': '日経VI',
}


# ---------- 数値正規化 (NaN/Inf を None に) ----------
def _num(x):
    """NaN/Inf を None に正規化する (厳密JSONは NaN を許さない)。"""
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(xf) or math.isinf(xf):
        return None
    return xf


def _clean(obj):
    """dict/list を再帰的に走査し NaN/Inf を None に置換する。"""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if isinstance(obj, float):
        return _num(obj)
    return obj


# ---------- 重みの自己学習連携 ----------
def load_weights():
    """weights.json があれば INDICATORS の重みを上書きし version を返す。

    存在しない/壊れている場合はベタ書きのデフォルト重みのまま動く。
    全ファイルが共通の重み源(この関数)を使うことでスコアが完全に同期する。
    """
    path = os.path.join(SCRIPT_DIR, 'weights.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            wj = json.load(f)
    except Exception:
        return None
    wmap = wj.get('weights', {})
    for ind in INDICATORS:
        if ind['key'] in wmap:
            v = _num(wmap[ind['key']])
            if v is not None and v >= 0:
                ind['weight'] = v
    return wj.get('version')


# import 時に一度だけ重みを適用 (INDICATORS をその場で書き換える)
WEIGHTS_VERSION = load_weights()


# ---------- スコア計算 ----------
def calc_score(indicators):
    """12指標の重み付き合成で場の強弱スコア(-100〜+100)を返す。"""
    s, w = 0, 0
    for ind in INDICATORS:
        v = indicators.get(ind['key'])
        if not v or v.get('chgPct') is None:
            continue
        d = -1 if ind['inverse'] else 1
        cp = max(-5, min(5, v['chgPct']))
        s += cp * 15 * d * ind['weight']
        w += ind['weight']
    return None if w == 0 else max(-100, min(100, s / (w * 0.75)))


def stock_score(stock, indicators):
    """銘柄のタグに紐づく指標 chgPct の平均(連動性の目安)。"""
    s, n = 0, 0
    for tag in stock['tags']:
        k = TAG_MAP.get(tag)
        if not k:
            continue
        v = indicators.get(k)
        if not v or v.get('chgPct') is None:
            continue
        s += v['chgPct']
        n += 1
    return s / n if n else None


def stars_for(stock, market_score, indicators):
    """銘柄の期待度を★1〜★5で返す。"""
    ss = stock_score(stock, indicators)
    if ss is None or market_score is None:
        return 0
    combined = ss * 10 + market_score * 0.3
    if combined >= 25:
        return 5
    if combined >= 12:
        return 4
    if combined >= 3:
        return 3
    if combined >= -8:
        return 2
    return 1
