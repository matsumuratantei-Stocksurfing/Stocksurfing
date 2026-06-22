#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
松村式Stocksurfing - データ取得スクリプト (v3.4)
yfinance + Nikkei公式 + J-Quants V2(決算カレンダー) を統合
"""
import json
import sys
import os
import re
from datetime import datetime, timezone, timedelta

import yfinance as yf
import requests
from bs4 import BeautifulSoup

# 同一ディレクトリの共通モジュール / J-Quants V2 クライアントをimport
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import TRACKED_STOCKS
from jquants_client import get_earnings_calendar

JST = timezone(timedelta(hours=9))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UA_DESKTOP = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'

SYMBOL_OPTIONS = {
    'N225F':  ['NKD=F'],
    'TOPX':   ['^TPX', '^TOPX', '1306.T'],
    'NDX':    ['^NDX'],
    'SPX':    ['^GSPC'],
    'SOX':    ['^SOX'],
    'DJI':    ['^DJI'],
    'USDJPY': ['JPY=X'],
    'EURJPY': ['EURJPY=X'],
    'TNX':    ['^TNX'],
    'WTI':    ['CL=F'],
    'VIX':    ['^VIX'],
    'NKVI':   ['^N225VI', '^JNIV'],
}
REFERENCE_OPTIONS = {
    'N225_CASH': ['^N225'],
    'SOX_PROXY': ['8035.T'],
}

def fetch_yfinance_one(symbol):
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period='5d', interval='1d')
        if hist.empty or len(hist) < 1:
            return None
        latest = hist.iloc[-1]
        close = float(latest['Close'])
        if len(hist) >= 2:
            prev_close = float(hist.iloc[-2]['Close'])
            chg_pct = ((close - prev_close) / prev_close) * 100 if prev_close != 0 else None
        else:
            open_p = float(latest['Open'])
            chg_pct = ((close - open_p) / open_p) * 100 if open_p != 0 else None
        return {'price': round(close, 4), 'chgPct': round(chg_pct, 4) if chg_pct is not None else None}
    except Exception:
        return None

def fetch_with_options(symbols):
    for sym in symbols:
        r = fetch_yfinance_one(sym)
        if r and r['price'] > 0:
            return r, sym
    return None, None

# ---------- 日経VI(現物) ----------
def scrape_nkvi_nikkei_official():
    url = 'https://indexes.nikkei.co.jp/nkave/index?idx=nk225vi'
    try:
        r = requests.get(url, headers={
            'User-Agent': UA_DESKTOP,
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'ja,en;q=0.9',
        }, timeout=15)
        r.encoding = r.apparent_encoding
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, 'html.parser')
        text = soup.get_text('\n', strip=True)
        price_match = re.search(r'現値\s*[\n\s]*([0-9]+\.[0-9]+)', text)
        chg_pct_match = re.search(
            r'前日比[\s\S]{0,300}?[（(]\s*([+\-▲]?\s*[0-9]+\.[0-9]+)\s*%[)）]',
            text
        )
        abs_change_match = re.search(
            r'前日比[\s\S]{0,200}?([+\-▲]?\s*[0-9]+\.[0-9]+)\s*[（(]',
            text
        )
        if price_match:
            price = float(price_match.group(1))
            if not (5 < price < 100):
                return None
            chg_pct = None
            if chg_pct_match:
                s = chg_pct_match.group(1).replace('▲', '-').replace('+', '').strip()
                try:
                    chg_pct = float(s)
                    if abs_change_match:
                        abs_s = abs_change_match.group(1).replace('▲', '-').replace('+', '').strip()
                        try:
                            abs_change = float(abs_s)
                            if price - abs_change > 0:
                                expected_pct = abs_change / (price - abs_change) * 100
                                if abs(expected_pct - chg_pct) > 1.0:
                                    chg_pct = expected_pct
                        except ValueError:
                            pass
                except ValueError:
                    pass
            return {'price': round(price, 4), 'chgPct': round(chg_pct, 4) if chg_pct is not None else None}
        return None
    except Exception:
        return None

def fetch_nkvi_multisource():
    print(f"    [日経VI 取得開始]")
    r = fetch_yfinance_one('^N225VI')
    if r and r['price'] > 0:
        print(f"      ✓ yfinance: 成功")
        return r
    r = scrape_nkvi_nikkei_official()
    if r and r.get('price'):
        print(f"      ✓ Nikkei公式: 成功 (price={r['price']}, chg={r.get('chgPct')})")
        return r
    print(f"      ✗ 日経VI 取得失敗")
    return None

# ---------- 決算カレンダー (J-Quants V2) ----------
def is_business_day(d):
    """土日のみ非営業日とみなす(祝日は無視)"""
    return d.weekday() < 5

def business_days_until(target_date_str, base_date):
    """base_dateから target までの営業日数 (0以上)"""
    try:
        target = datetime.strptime(target_date_str, '%Y-%m-%d').date()
    except ValueError:
        return None
    if target < base_date:
        return None
    days = 0
    cur = base_date
    while cur < target:
        cur = cur + timedelta(days=1)
        if is_business_day(cur):
            days += 1
    return days

def fetch_earnings_calendar():
    """J-Quants V2 から決算発表予定を取得し、監視銘柄の3営業日以内警告をまとめる。

    ※ V2の /equities/earnings-calendar は「翌営業日」に決算発表予定の
       3月期・9月期銘柄のみを返す。よって実質「翌営業日(1営業日後)」の警告が中心。
       それでも仕掛け回避の役には立つため3営業日窓のロジックは温存する。
    """
    print()
    print("[決算カレンダー取得 (J-Quants V2)]")
    today = datetime.now(JST).date()

    data = get_earnings_calendar()
    if not data:
        print(f"  ✗ J-Quants 接続失敗 or データなし")
        return {}

    # V2 /equities/earnings-calendar のレスポンス: {"data": [{...}, ...]}
    announcements = data.get('data', [])
    print(f"  全発表予定: {len(announcements)} 件取得")

    warnings = {}
    for ann in announcements:
        code_raw = ann.get('Code', '')
        # J-Quantsは5桁コード(末尾0)を返すので、4桁にする
        code = code_raw[:4] if len(code_raw) >= 4 else code_raw
        if code not in TRACKED_STOCKS:
            continue
        date_str = ann.get('Date', '')
        bdays = business_days_until(date_str, today)
        if bdays is None or bdays > 3:
            continue
        # 既存より近い日があれば上書きしない
        existing = warnings.get(code)
        if existing and existing.get('businessDaysUntil', 99) <= bdays:
            continue
        warnings[code] = {
            'date': date_str,
            'businessDaysUntil': bdays,
            'fiscalYear': ann.get('FY', ''),
            'fiscalPeriod': ann.get('FQ', ''),
            'companyName': ann.get('CoName', ''),
        }
        print(f"  ⚠️ {code} ({ann.get('CoName','')}) — 決算予定 {date_str} ({bdays}営業日後)")

    if not warnings:
        print(f"  ✓ 監視銘柄すべて決算3営業日以内なし")
    return warnings

def main():
    print("=" * 50)
    print("  松村式Stocksurfing データ取得 (v3.4)")
    print(f"  実行時刻: {datetime.now(JST).isoformat()}")
    print("=" * 50)
    print()

    indicators = {}
    print("[指標取得]")
    for key, symbols in SYMBOL_OPTIONS.items():
        candidates = ', '.join(symbols)
        print(f"  {key:8s} 候補: {candidates}")
        result, used = fetch_with_options(symbols)
        if result:
            chg_s = f"{result['chgPct']:+.2f}%" if result['chgPct'] is not None else "N/A"
            print(f"    ✓ OK -> {used:12s} price={result['price']:>12.2f} {chg_s}")
            indicators[key] = result
        else:
            print(f"    ✗ FAIL")
            if key == 'NKVI':
                result = fetch_nkvi_multisource()
                if result:
                    indicators[key] = result

    reference = {}
    print()
    print("[参照データ]")
    for key, symbols in REFERENCE_OPTIONS.items():
        result, used = fetch_with_options(symbols)
        if result:
            reference[key] = result
            print(f"  {key:10s} OK -> {used:10s} price={result['price']:>12.2f}")
        else:
            print(f"  {key:10s} FAIL")

    # 決算カレンダー (J-Quants V2)
    earnings_warnings = fetch_earnings_calendar()

    output = {
        'fetchedAt': datetime.now(JST).isoformat(),
        'indicators': indicators,
        'referenceData': reference,
        'earningsWarnings': earnings_warnings,
        'success': len(indicators),
        'total': len(SYMBOL_OPTIONS),
    }

    out_path = os.path.join(SCRIPT_DIR, 'data.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print()
    print("=" * 50)
    print(f"  取得完了: {len(indicators)}/{len(SYMBOL_OPTIONS)} 指標 + 決算警告 {len(earnings_warnings)} 件")
    print("=" * 50)

if __name__ == '__main__':
    main()
