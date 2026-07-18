#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
松村式Stocksurfing - データ取得スクリプト (v3.4.5)
yfinance + Nikkei公式 + J-Quants V2(決算カレンダー・TOPIX指数・取引カレンダー) を統合

v3.4.3 (2026-07-04):
- 各指標に asOf (データの日付) を記録し、日付ズレ/staleデータ混入を検出 (dataQuality)
- 日経VI: Nikkei公式のURL構造変更(?idx= が一覧ページ化し「現値」が消失)に対応。
  個別指数プロフィールページ (/nkave/index/profile?idx=nk225vi) から取得する。
v3.4.4 (2026-07-04):
- 決算警告に JPX公式Excel (jpx_earnings.py) を統合し「3営業日前警告」を復活。
  J-Quants V2 (翌営業日分のみ・確度高) を優先し、JPXで先の予定を補完する。
v3.4.5 (2026-07-18):
- 【重要】N225_CASH(窓開け計算の基準となる日経平均前日終値)の1営業日遅れを修正。
  yfinance ^N225 は前日の確定日足が翌朝になっても反映されないことがある(2026-07に
  7/14,7/17,7/18朝の3回連続で確認。窓開け予想が最大3倍過大/方向逆転していた)。
  対策: 日付フィルタ + J-Quants取引カレンダーで期待営業日を検証し、
  ズレていれば日経公式プロフィールページ(idx=nk225)で補正する。
- 【重要】TOPX を J-Quants公式TOPIX指数(/indices/bars/daily/topix)を一次ソースに変更。
  Yahoo の ^TPX/^TOPX が取得不能になり、フォールバックの 1306.T が2026-04-01の
  1:10分割で「TOPIX=407.9」のような桁違い表示になっていた。
- dataQuality に referenceData の日付検証(refDates/jpExpectedDate)を追加。
  N225_CASH が期待営業日とズレたら staleKeys 入りし、メール側で窓予想を非表示にする。
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
from common import TRACKED_STOCKS, tse_last_trading_day, scrape_nikkei_index_snapshot
from jquants_client import get_earnings_calendar, get_topix_daily
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

def fetch_yf_prev_close(symbol, today, retries=2):
    """yfinanceで『today より前の最後の確定日足』を取得する (v3.4.5)。

    ^N225 は当日途中足が混ざったり、最新確定足が翌朝まで欠けることがあるため、
    末尾行を鵜呑みにせず日付でフィルタして「前日終値」の意味を保証する。
    """
    for attempt in range(retries + 1):
        try:
            hist = yf.Ticker(symbol).history(period='10d', interval='1d')
            if hist is None or hist.empty:
                raise ValueError('empty history')
            mask = [d.date() < today for d in hist.index]
            prior = hist[mask]
            if prior.empty:
                return None
            close = float(prior['Close'].iloc[-1])
            chg = None
            if len(prior) >= 2:
                pc = float(prior['Close'].iloc[-2])
                if pc:
                    chg = (close - pc) / pc * 100
            return {'price': round(close, 4),
                    'chgPct': round(chg, 4) if chg is not None else None,
                    'asOf': prior.index[-1].strftime('%Y-%m-%d')}
        except Exception:
            if attempt < retries:
                time.sleep(1.5)
                continue
            return None
    return None

# ---------- TOPIX (J-Quants公式指数) ----------
def fetch_topix_jquants():
    """J-Quants V2 /indices/bars/daily/topix から直近のTOPIX公式終値と前日比を返す。

    v3.4.5: Yahoo ^TPX/^TOPX 取得不能・1306.T分割問題の恒久対策。
    朝7:30時点では前営業日の確定値(夕方更新)が返る。
    """
    today = datetime.now(JST).date()
    frm = (today - timedelta(days=14)).strftime('%Y-%m-%d')
    data = get_topix_daily(frm, today.strftime('%Y-%m-%d'))
    rows = [r for r in ((data or {}).get('data') or []) if r.get('C') is not None]
    if not rows:
        return None
    rows.sort(key=lambda r: r.get('Date', ''))
    last = rows[-1]
    try:
        close = float(last['C'])
    except (TypeError, ValueError):
        return None
    if close <= 0:
        return None
    chg = None
    if len(rows) >= 2:
        try:
            prev = float(rows[-2]['C'])
            if prev:
                chg = (close - prev) / prev * 100
        except (TypeError, ValueError):
            pass
    return {'price': round(close, 4),
            'chgPct': round(chg, 4) if chg is not None else None,
            'asOf': last.get('Date')}

# ---------- 日経平均 前日終値 (多段ソース) ----------
def resolve_n225_cash(now_jst, expected):
    """『直近の東証営業日(expected)の日経平均終値』をなるべく確実に取る (v3.4.5)。

    1) yfinance ^N225 の確定足(当日を除いた最後の足)
    2) 日付が expected と合わなければ日経公式プロフィールページで補正
       - ページが前営業日の確定値なら、そのまま採用
       - 場中(当日値)なら『現値 - 前日比』で前日終値を逆算
    3) それでもダメなら stale な yfinance 値を返す(dataQuality で警告される)
    戻り値: (result_dict or None, source_str or None)
    """
    today = now_jst.date()
    exp_s = expected.strftime('%Y-%m-%d')
    today_s = today.strftime('%Y-%m-%d')

    yf_prev = fetch_yf_prev_close('^N225', today)
    if yf_prev and yf_prev.get('asOf') == exp_s:
        return yf_prev, 'yfinance'

    snap = scrape_nikkei_index_snapshot('nk225', '日経平均株価', 20000, 150000)
    if snap:
        if snap.get('asOf') == exp_s:
            return ({'price': snap['price'],
                     'chgPct': snap.get('chgPct'),
                     'asOf': snap['asOf']}, 'Nikkei公式')
        if snap.get('asOf') == today_s and snap.get('chgAbs') is not None:
            prev = snap['price'] - snap['chgAbs']
            if prev > 0:
                return ({'price': round(prev, 4),
                         'chgPct': None,
                         'asOf': exp_s}, 'Nikkei公式(前日比から逆算)')

    if yf_prev:
        return yf_prev, 'yfinance(stale)'
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
    print("  松村式Stocksurfing データ取得 (v3.4.5)")
    print(f"  実行時刻: {datetime.now(JST).isoformat()}")
    print("=" * 50)
    print()

    now_jst = datetime.now(JST)

    indicators = {}
    print("[指標取得]")
    for key, symbols in SYMBOL_OPTIONS.items():
        # v3.4.5: TOPX は J-Quants公式指数を一次ソースにする
        # (Yahoo ^TPX/^TOPX は取得不能になり、1306.T は2026-04の1:10分割で
        #  ETF価格がTOPIX実数と桁違いになるため)
        if key == 'TOPX':
            r = fetch_topix_jquants()
            if r:
                chg_s = f"{r['chgPct']:+.2f}%" if r['chgPct'] is not None else "N/A"
                print(f"  {key:8s} ✓ OK -> J-Quants公式TOPIX price={r['price']:>12.2f} {chg_s} asOf={r.get('asOf')}")
                indicators[key] = r
                continue
            print(f"  {key:8s} ⚠ J-Quants TOPIX失敗 → yfinanceフォールバック")
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
    expected_jp_day = tse_last_trading_day(now_jst.date())
    exp_s = expected_jp_day.strftime('%Y-%m-%d')
    print(f"  直近の東証営業日(期待日付): {exp_s}")

    # N225_CASH: 窓開け計算の基準になる前日終値。日付検証 + 多段ソース (v3.4.5)
    n225_cash, n225_src = resolve_n225_cash(now_jst, expected_jp_day)
    if n225_cash:
        reference['N225_CASH'] = n225_cash
        flag = '' if n225_cash.get('asOf') == exp_s else ' ⚠stale'
        print(f"  N225_CASH  OK -> {n225_src:20s} price={n225_cash['price']:>12.2f} asOf={n225_cash.get('asOf')}{flag}")
    else:
        print(f"  N225_CASH  FAIL")

    for key, symbols in REFERENCE_OPTIONS.items():
        if key == 'N225_CASH':
            continue
        result, used = fetch_with_options(symbols)
        if result:
            reference[key] = result
            print(f"  {key:10s} OK -> {used:10s} price={result['price']:>12.2f}")
        else:
            print(f"  {key:10s} FAIL")

    # 決算カレンダー (J-Quants V2)
    earnings_warnings = fetch_earnings_calendar()

    # データ品質 (日付整合) チェック
    data_quality = assess_data_quality(indicators, now_jst)
    # v3.4.5: 参照データ(窓開け計算の基準)の日付も検証・記録する
    ref_dates = {k: (v.get('asOf') if isinstance(v, dict) else None) for k, v in reference.items()}
    data_quality['refDates'] = ref_dates
    data_quality['jpExpectedDate'] = exp_s
    if 'N225_CASH' not in reference or ref_dates.get('N225_CASH') != exp_s:
        data_quality['staleKeys'] = sorted(set(data_quality['staleKeys']) | {'N225_CASH'})
    if data_quality['staleKeys']:
        print()
        print(f"  ⏳ 日付ズレ/staleの疑い: {', '.join(data_quality['staleKeys'])} (米基準日 {data_quality['usRefDate']} / 東証期待日 {exp_s})")

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
