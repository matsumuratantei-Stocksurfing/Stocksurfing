#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J-Quants API クライアント (Premium静的APIキー対応)
複数の認証方式を試行して、最初に通るものを使う
"""
import os
import sys
import requests
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
JQ_BASE = 'https://api.jquants.com/v1'
API_KEY = os.environ.get('JQUANTS_API_KEY', '')

# トークンキャッシュ (refresh→idtokenの場合に保持)
_id_token_cache = None

def _get_id_token_via_refresh():
    """API Keyをrefresh_tokenとしてid_tokenを発行"""
    global _id_token_cache
    if _id_token_cache:
        return _id_token_cache
    try:
        r = requests.post(
            f'{JQ_BASE}/token/auth_refresh',
            params={'refreshtoken': API_KEY},
            timeout=30
        )
        if r.status_code == 200:
            data = r.json()
            _id_token_cache = data.get('idToken')
            return _id_token_cache
    except Exception as e:
        print(f"  [JQ] auth_refresh失敗: {e}")
    return None

def jq_get(path, params=None):
    """J-Quants GET 複数の認証方式を試行"""
    if not API_KEY:
        print(f"  [JQ] JQUANTS_API_KEY が未設定")
        return None
    
    url = f'{JQ_BASE}{path}'
    
    # 試行1: Bearer (静的APIキー直接)
    try:
        r = requests.get(url, headers={'Authorization': f'Bearer {API_KEY}'},
                         params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        elif r.status_code != 401:
            print(f"  [JQ] {path} Bearer: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  [JQ] {path} Bearer ex: {e}")
    
    # 試行2: 静的キーをrefresh_tokenとして扱い、id_tokenで再試行
    id_token = _get_id_token_via_refresh()
    if id_token:
        try:
            r = requests.get(url, headers={'Authorization': f'Bearer {id_token}'},
                             params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            elif r.status_code != 401:
                print(f"  [JQ] {path} id_token: HTTP {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"  [JQ] {path} id_token ex: {e}")
    
    return None

# ---------- API利用関数 ----------

def get_daily_quote(code, date=None):
    """1銘柄の日次価格を取得 (4桁証券コード)"""
    params = {'code': code}
    if date:
        params['date'] = date
    return jq_get('/prices/daily_quotes', params)

def get_announcements():
    """直近の決算発表予定を取得"""
    return jq_get('/fins/announcement')

def get_listed_info(code=None):
    """上場銘柄情報"""
    params = {'code': code} if code else None
    return jq_get('/listed/info', params)

# ---------- 動作テスト ----------
def selftest():
    print("[J-Quants 動作テスト]")
    if not API_KEY:
        print("  JQUANTS_API_KEY が未設定。テストできません")
        return False
    print(f"  API Key: {API_KEY[:6]}...{API_KEY[-4:]} (length={len(API_KEY)})")
    
    # 上場銘柄情報を1社取得してみる (トヨタ)
    r = get_listed_info('7203')
    if r:
        info = r.get('info', [])
        if info:
            print(f"  ✓ 接続成功: {info[0].get('CompanyName', '?')}")
            return True
    print(f"  ✗ 接続失敗")
    return False

if __name__ == '__main__':
    selftest()
