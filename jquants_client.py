#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J-Quants API クライアント (V2 / x-api-key 方式)

2026-06 に J-Quants は V1(api.jquants.com/v1, Bearer/refresh token)を廃止し、
V2(api.jquants.com/v2, x-api-key ヘッダ)へ移行した。本モジュールは V2 専用。

- ベースURL : https://api.jquants.com/v2
- 認証      : x-api-key ヘッダ (Dashboard で発行したAPIキー。有効期限なし)
- レスポンス: 原則 {"data": [...], "pagination_key": "..."}
- 主要EP    : /equities/bars/daily, /equities/bars/daily/am,
              /equities/earnings-calendar, /equities/master,
              /indices/bars/daily/topix, /markets/calendar

GitHub Secret `JQUANTS_API_KEY` には V2 のAPIキーを設定すること(V1キーは無効)。
"""
import os
import requests

JQ_BASE = 'https://api.jquants.com/v2'
API_KEY = os.environ.get('JQUANTS_API_KEY', '')

# pagination を辿る際の安全上限(無限ループ防止)
_MAX_PAGES = 20


def jq_get(path, params=None, follow_pagination=True):
    """J-Quants V2 GET。

    成功時は {"data": [...]} 形式の dict を返す(pagination_key があれば
    全ページを辿って data を連結する)。失敗時は None。
    呼び出し側は戻り値の 'data' 配列を見るだけでよい。
    """
    if not API_KEY:
        print("  [JQ] JQUANTS_API_KEY が未設定")
        return None

    url = f'{JQ_BASE}{path}'
    headers = {'x-api-key': API_KEY}
    p = dict(params or {})
    all_data = []
    pages = 0

    while True:
        try:
            r = requests.get(url, headers=headers, params=p, timeout=30)
        except Exception as e:
            print(f"  [JQ] {path} ex: {e}")
            return None

        if r.status_code != 200:
            # 410(V1廃止)・401(キー不正)・403(プラン外)等はここで可視化
            print(f"  [JQ] {path}: HTTP {r.status_code} {r.text[:200]}")
            return None

        try:
            j = r.json()
        except Exception as e:
            print(f"  [JQ] {path} JSON parse失敗: {e}")
            return None

        data = j.get('data')
        if isinstance(data, list):
            all_data.extend(data)

        pages += 1
        pk = j.get('pagination_key')
        if not follow_pagination or not pk or pages >= _MAX_PAGES:
            break
        p['pagination_key'] = pk

    return {'data': all_data}


# ---------- API利用関数 (V2) ----------

def get_daily_quote(code, date=None):
    """株価四本値 /equities/bars/daily。

    レスポンス data[].O/H/L/C(調整前), Vo/Va, 前場 MO/MH/ML/MC,
    後場 AO/AH/AL/AC(前場/後場はPremiumのみ)。code は4桁でも5桁でも可。
    """
    params = {'code': code}
    if date:
        params['date'] = date
    return jq_get('/equities/bars/daily', params)


def get_prices_am(code=None, date=None):
    """前場四本値 /equities/bars/daily/am。

    V2では当日分のみ(翌6:00頃まで)を返し、date パラメータは存在しない。
    引数 date は後方互換のため受けるが無視する。
    レスポンス data[].MO/MH/ML/MC, MVo/MVa。
    """
    params = {}
    if code:
        params['code'] = code
    return jq_get('/equities/bars/daily/am', params or None)


def get_earnings_calendar():
    """決算発表予定 /equities/earnings-calendar。

    V2は「翌営業日」に決算発表予定の銘柄(3月期・9月期)のみを返す。
    レスポンス data[].Date/Code/CoName/FY/SectorNm/FQ/Section。
    """
    return jq_get('/equities/earnings-calendar')


# 後方互換エイリアス(旧名 get_announcements を呼ぶ箇所のため)
get_announcements = get_earnings_calendar


def get_listed_info(code=None):
    """上場銘柄一覧 /equities/master。

    レスポンス data[].Code/CoName/CoNameEn/S17.../Mkt/MktNm 等。
    """
    params = {'code': code} if code else None
    return jq_get('/equities/master', params)


def get_topix_daily(from_date=None, to_date=None):
    """TOPIX指数四本値 /indices/bars/daily/topix (v3.4.5 追加)。

    レスポンス data[].Date/O/H/L/C。TOPIXの公式値が取れる(Standard/Premium)。
    Yahoo の ^TPX/^TOPX が取得不能になり、1306.T は2026-04の1:10分割で
    価格がTOPIX実数と桁違いになったため、これを TOPX の一次ソースにする。
    """
    params = {}
    if from_date:
        params['from'] = from_date
    if to_date:
        params['to'] = to_date
    return jq_get('/indices/bars/daily/topix', params or None)


def get_market_calendar(from_date=None, to_date=None):
    """取引カレンダー /markets/calendar (v3.4.5 追加)。

    レスポンス data[].Date/HolDiv。
    HolDiv: '0'=非営業日, '1'=営業日, '2'=東証半日立会日, '3'=非営業日(祝日取引あり)。
    東証の立会があるのは HolDiv '1' または '2'。祝日(海の日等)の誤判定防止に使う。
    """
    params = {}
    if from_date:
        params['from'] = from_date
    if to_date:
        params['to'] = to_date
    return jq_get('/markets/calendar', params or None)


# ---------- 動作テスト ----------
def selftest():
    print("[J-Quants V2 動作テスト]")
    if not API_KEY:
        print("  JQUANTS_API_KEY が未設定。テストできません")
        return False
    print(f"  API Key: {API_KEY[:6]}...{API_KEY[-4:]} (length={len(API_KEY)})")

    # 上場銘柄情報を1社取得してみる (トヨタ 7203)
    r = get_listed_info('7203')
    if r:
        info = r.get('data', [])
        if info:
            print(f"  ✓ 接続成功: {info[0].get('CoName', '?')}")
            return True
    print("  ✗ 接続失敗")
    return False


if __name__ == '__main__':
    selftest()
