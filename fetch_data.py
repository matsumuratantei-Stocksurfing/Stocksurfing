#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
松村式Stocksurfing - データ取得スクリプト (Phase 2 / v3.0)
GitHub Actions上で実行され、yfinance + 日経公式スクレイピングで全12指標を取得
data.json をリポジトリにコミットすることでHTMLから読み込まれる
"""
import json
import sys
import os
import re
from datetime import datetime, timezone, timedelta

import yfinance as yf
import requests
from bs4 import BeautifulSoup

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
    'NKVI':   ['^N225VI', '^JNIV'],   # yfinance失敗時はNikkei公式へフォールバック
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

# ---------- 日経VI(現物指数) ----------

def scrape_nkvi_nikkei_official():
    """日経公式 indexes.nikkei.co.jp/nkave/index?idx=nk225vi
    BeautifulSoupで構造化データを正確抽出"""
    url = 'https://indexes.nikkei.co.jp/nkave/index?idx=nk225vi'
    try:
        r = requests.get(url, headers={
            'User-Agent': UA_DESKTOP,
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'ja,en;q=0.9',
        }, timeout=15)
        r.encoding = r.apparent_encoding
        if r.status_code != 200:
            print(f"      [Nikkei公式] HTTP {r.status_code}")
            return None
        
        soup = BeautifulSoup(r.text, 'html.parser')
        
        # 1) ページのテキスト全体を取得して、構造的に最も信頼できるパターンで抽出
        text = soup.get_text('\n', strip=True)
        
        # パターンA: "現値" の直後の数値 + "前日比" 直後のパーセンテージ
        # 構造: 現値\n39.79\n...前日比\n+11.21（+39.22%）
        price_match = re.search(r'現値\s*[\n\s]*([0-9]+\.[0-9]+)', text)
        # 「前日比 ... (XX.XX%)」または「前日比\n...\n(+XX.XX%)」のパターン
        chg_pct_match = re.search(
            r'前日比[\s\S]{0,300}?[（(]\s*([+\-▲]?\s*[0-9]+\.[0-9]+)\s*%[)）]',
            text
        )
        
        # パターンB: 絶対変化値からchgPct計算 (バックアップ)
        abs_change_match = re.search(
            r'前日比[\s\S]{0,200}?([+\-▲]?\s*[0-9]+\.[0-9]+)\s*[（(]',
            text
        )
        
        if price_match:
            price = float(price_match.group(1))
            if not (5 < price < 100):
                print(f"      [Nikkei公式] 価格レンジ外: {price}")
                return None
            
            chg_pct = None
            if chg_pct_match:
                s = chg_pct_match.group(1).replace('▲', '-').replace('+', '').strip()
                try:
                    chg_pct = float(s)
                    # 整合チェック: 絶対変化値が分かれば、計算と一致するか確認
                    if abs_change_match:
                        abs_s = abs_change_match.group(1).replace('▲', '-').replace('+', '').strip()
                        try:
                            abs_change = float(abs_s)
                            if price - abs_change > 0:
                                expected_pct = abs_change / (price - abs_change) * 100
                                if abs(expected_pct - chg_pct) > 1.0:
                                    # 値が整合しない場合、計算値を信頼
                                    print(f"      [Nikkei公式] chgPct整合エラー (page={chg_pct}, calc={expected_pct:.2f})。計算値採用")
                                    chg_pct = expected_pct
                        except ValueError:
                            pass
                except ValueError:
                    pass
            
            return {
                'price': round(price, 4),
                'chgPct': round(chg_pct, 4) if chg_pct is not None else None,
                'source': 'Nikkei公式',
                'url': url,
            }
        else:
            print(f"      [Nikkei公式] 現値が見つからない")
            return None
    except Exception as e:
        print(f"      [Nikkei公式] {type(e).__name__}: {e}")
        return None

def fetch_nkvi_multisource():
    """日経VI(現物指数)を取得"""
    print(f"    [日経VI 取得開始]")
    # 1. yfinance (delistedの場合は失敗)
    r = fetch_yfinance_one('^N225VI')
    if r and r['price'] > 0:
        print(f"      ✓ yfinance: 成功 (price={r['price']}, chg={r['chgPct']:+.2f}%)")
        return r, 'yfinance'
    print(f"      ✗ yfinance: 失敗 (delisted等)")
    
    # 2. 日経公式 (BeautifulSoup)
    r = scrape_nkvi_nikkei_official()
    if r and r.get('price'):
        chg_s = f"{r['chgPct']:+.2f}%" if r.get('chgPct') is not None else "N/A"
        print(f"      ✓ Nikkei公式: 成功 (price={r['price']}, chg={chg_s})")
        return r, 'Nikkei公式'
    
    print(f"      ✗ 全ソース失敗")
    return None, None

def main():
    print("=" * 50)
    print("  松村式Stocksurfing データ取得 (Phase 2 / v3.0)")
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
            print(f"    ✗ FAIL -> 全候補失敗")
            if key == 'NKVI':
                result, source_name = fetch_nkvi_multisource()
                if result:
                    indicators[key] = {k: v for k, v in result.items() if k in ('price', 'chgPct')}
    
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
    
    output = {
        'fetchedAt': datetime.now(JST).isoformat(),
        'indicators': indicators,
        'referenceData': reference,
        'success': len(indicators),
        'total': len(SYMBOL_OPTIONS),
    }
    
    out_path = os.path.join(SCRIPT_DIR, 'data.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print()
    print("=" * 50)
    print(f"  取得完了: {len(indicators)}/{len(SYMBOL_OPTIONS)} 成功")
    print("=" * 50)

if __name__ == '__main__':
    main()
