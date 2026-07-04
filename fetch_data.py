#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
松村式Stocksurfing - データ取得スクリプト (v3.4.3)
yfinance + Nikkei公式 + J-Quants V2(決算カレンダー) を統合

v3.4.3 (2026-07-04):
- 各指標に asOf (データの日付) を記録し、日付ズレ/staleデータ混入を検出 (dataQuality)
- 日経VI: Nikkei公式のURL構造変更(?idx= が一覧ページ化し「現値」が消失)に対応。
  個別指数プロフィールページ (/nkave/index/profile?idx=nk225vi) から取得する。
v3.4.4 (2026-07-04):
- 決算警告に JPX公式Excel (jpx_earnings.py) を統合し「3営業日前警告」を復活。
  J-Quants V2 (翌営業日分のみ・確度高) を優先し、JPXで先の予定を補完する。
"""
import json
import sys
import os
import re
import time
from datetime import datetime, timezone, timedelta

import yfinance as yf
import requests
from bs4 import BeautifulSoup

# 同一ディレクトリの共通モジュール / J-Quants V2 クライアントをimport
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import TRACKED_STOCKS
from jquants_client import get_earnings_calendar
from jpx_earnings import get_jpx_warnings

JST = timezone(timedelta(hours=9))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UA_DESKTOP = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'

SYMBOL_OPTIONS = {
    'N225F':  ['NKD=F'],
    'TOPX':   ['^TPX', '^TOPX', '1306.T'],
    'NDX':    ['^NDX', 'QQQ'],
    'SPX':    ['^GSPC', 'SPY', '^SPX'],
    'SOX':    ['^SOX', 'SOXX'],
    'DJI':    ['^DJI', 'DIA'],
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

def fetch_yfinance_one(symbol, retries=2):
    """yfinanceで1シンボル取得。yfinance/Yahooは一時的失敗(レート制限・空応答)が
    あるため、空/例外なら短い間隔でリトライする。"""
    for attempt in range(retries + 1):
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period='5d', interval='1d')
            if hist.empty or len(hist) < 1:
                if attempt < retries:
                    time.sleep(1.5)
                    continue
                return None
            latest = hist.iloc[-1]
            close = float(latest['Close'])
            if len(hist) >= 2:
                prev_close = float(hist.iloc[-2]['Close'])
                chg_pct = ((close - prev_close) / prev_close) * 100 if prev_close != 0 else None
            else:
                open_p = float(latest['Open'])
                chg_pct = ((close - open_p) / open_p) * 100 if open_p != 0 else None
            try:
                as_of = hist.index[-1].strftime('%Y-%m-%d')
            except Exception:
                as_of = None
            return {'price': round(close, 4),
                    'chgPct': round(chg_pct, 4) if chg_pct is not None else None,
                    'asOf': as_of}
        except Exception:
            if attempt < retries:
                time.sleep(1.5)
                continue
            return None
    return None

def fetch_with_options(symbols):
    for sym in symbols:
        r = fetch_yfinance_one(sym)
        if r and r['price'] > 0:
            return r, sym
    return None, None

# ---------- 日経VI(現物) ----------
def scrape_nkvi_profile():
    """日経公式の個別指数プロフィールページから日経VIを取得。

    2026-07確認: 従来の ?idx=nk225vi は指数値一覧ページになり「現値」の文言が消えたため、
    プロフィールページ(サーバーサイドレンダリング)を一次ソースにする。
    ページ冒頭に「日経平均ボラティリティー・インデックス / 34.11 / +37.87% +9.37 2026.07.03(15:50)」
    の形で現値・前日比%・前日比・データ日付が載っている。
    """
    url = 'https://indexes.nikkei.co.jp/nkave/index/profile?idx=nk225vi'
    try:
        r = requests.get(url, headers={
            'User-Agent': UA_DESKTOP,
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'ja,en;q=0.9',
        }, timeout=15)
        if r.status_code != 200:
            return None
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, 'html.parser')
        text = soup.get_text('\n', strip=True)
        m = re.search(
            r'ボラティリティー・インデックス[\s\S]{0,160}?'
            r'(?<![0-9,.])([0-9]{1,2}\.[0-9]{2})(?![0-9])[\n\s]+'
            r'([+\-−▲]?\s*[0-9]+\.[0-9]+)\s*%[\n\s]*'
            r'([+\-−▲]?\s*[0-9]+\.[0-9]+)[\n\s]*'
            r'(20[0-9]{2})\.([0-1][0-9])\.([0-3][0-9])',
            text)
        if not m:
            return None
        price = float(m.group(1))
        if not (5 < price < 100):
            return None

        def _signed(s):
            return float(s.replace('▲', '-').replace('−', '-').replace('+', '').replace(' ', ''))

        chg_pct = None
        try:
            chg_pct = _signed(m.group(2))
        except ValueError:
            pass
        # 前日比(絶対値)との整合チェック: ズレが大きければ絶対値から再計算
        try:
            abs_change = _signed(m.group(3))
            if chg_pct is not None and price - abs_change > 0:
                expected = abs_change / (price - abs_change) * 100
                if abs(expected - chg_pct) > 1.0:
                    chg_pct = expected
        except ValueError:
            pass
        as_of = f"{m.group(4)}-{m.group(5)}-{m.group(6)}"
        return {'price': round(price, 4),
                'chgPct': round(chg_pct, 4) if chg_pct is not None else None,
                'asOf': as_of}
    except Exception:
        return None

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
    r = scrape_nkvi_profile()
    if r and r.get('price'):
        print(f"      ✓ Nikkei公式(プロフィール): 成功 (price={r['price']}, chg={r.get('chgPct')}, asOf={r.get('asOf')})")
        return r
    r = scrape_nkvi_nikkei_official()
    if r and r.get('price'):
        print(f"      ✓ Nikkei公式(旧一覧): 成功 (price={r['price']}, chg={r.get('chgPct')})")
        return r
    print(f"      ✗ 日経VI 取得失敗")
    return None

# ---------- データ品質 (日付整合) チェック ----------
# 米国市場グループ: 同じ営業日のデータで揃っているべき指標
US_GROUP = ['NDX', 'SPX', 'SOX', 'DJI', 'TNX', 'VIX']

def assess_data_quality(indicators, now_jst):
    """各指標の asOf (データ日付) を集約し、staleデータ混入を検出する。

    判定ルール(参考情報。スコア計算には一切影響しない):
    - 米国指数グループ内で最新日付より古い指標 → stale
    - fetchedAt から5暦日超過去のデータ → stale (連休を考慮した緩めの閾値)
    ※ 市場が違えば日付が異なるのは正常(例: 米休場日は米指数のみ1日古い)。
      その場合 usRefDate と dates を見れば「どの日付のデータで判定したか」が分かる。
    """
    dates = {k: (v.get('asOf') if isinstance(v, dict) else None) for k, v in indicators.items()}
    stale = set()
    us_dates = [d for k, d in dates.items() if k in US_GROUP and d]
    us_ref = max(us_dates) if us_dates else None
    for k, d in dates.items():
        if not d:
            continue
        try:
            dt = datetime.strptime(d, '%Y-%m-%d').date()
        except ValueError:
            continue
        if (now_jst.date() - dt).days > 5:
            stale.add(k)
            continue
        if k in US_GROUP and us_ref and d < us_ref:
            stale.add(k)
    return {'dates': dates, 'staleKeys': sorted(stale), 'usRefDate': us_ref}


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
        # v3.4.4: J-Quantsが落ちてもJPXのみで継続できるよう、早期returnしない
        print(f"  ✗ J-Quants 接続失敗 or データなし (JPXのみで継続)")
        announcements = []
    else:
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

    # v3.4.4: JPX公式Excel(数週間先までの予定)で「3営業日前警告」を補完。
    # J-Quantsは翌営業日分のみだが確度が高いので、同一銘柄は「より近い日」を優先。
    # JPX取得に失敗しても J-Quants のみで継続する(フォールバック)。
    try:
        jpx_warnings = get_jpx_warnings(TRACKED_STOCKS, today, max_bdays=3)
    except Exception as e:
        print(f"  ⚠ JPX決算カレンダー取得失敗(J-Quantsのみで継続): {e}")
        jpx_warnings = {}
    for code, info in jpx_warnings.items():
        existing = warnings.get(code)
        if existing and existing.get('businessDaysUntil', 99) <= info['businessDaysUntil']:
            continue
        warnings[code] = info
        print(f"  ⚠️ {code} ({info.get('companyName', '')}) — 決算予定 {info['date']} ({info['businessDaysUntil']}営業日後) [JPX]")

    if not warnings:
        print(f"  ✓ 監視銘柄すべて決算3営業日以内なし")
    return warnings

def main():
    print("=" * 50)
    print("  松村式Stocksurfing データ取得 (v3.4.4)")
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

    # データ品質 (日付整合) チェック
    now_jst = datetime.now(JST)
    data_quality = assess_data_quality(indicators, now_jst)
    if data_quality['staleKeys']:
        print()
        print(f"  ⏳ 日付ズレ/staleの疑い: {', '.join(data_quality['staleKeys'])} (基準日 {data_quality['usRefDate']})")

    output = {
        'fetchedAt': now_jst.isoformat(),
        'indicators': indicators,
        'referenceData': reference,
        'earningsWarnings': earnings_warnings,
        'dataQuality': data_quality,
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
