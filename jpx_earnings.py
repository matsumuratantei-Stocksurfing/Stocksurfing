#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
松村式Stocksurfing - JPX決算発表予定日 取得モジュール (v3.4.4)

JPX公式の無料「決算発表予定日」Excel(四半期末月ごと)を取得・パースし、
監視銘柄の決算発表予定日を返す。J-Quants V2 の earnings-calendar が
「翌営業日分のみ」しか返さない制約を補い、「3営業日前警告」を復活させる。

- 一覧ページ: https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html
- xlsxのファイル名は更新日入りで可変 (例 kessan05_0619.xlsx) のため、
  毎回一覧ページからリンクを発見する。
- 発表予定日は後から変更されることがある。J-Quants(毎営業日17時更新・翌営業日分)の
  方が確度が高いため、マージ時は fetch_data.py 側で「より近い日」を優先する。
- 失敗しても呼び出し側 (fetch_data.py) は J-Quants のみで継続する設計。

診断モード: `python jpx_earnings.py` で監視銘柄の今後の予定を一覧表示
(.github/workflows/jpx_test.yml の workflow_dispatch から実行可能)。
"""
import io
import os
import re
import sys
import json
from datetime import datetime, date, timezone, timedelta

import requests

JST = timezone(timedelta(hours=9))
INDEX_URL = 'https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html'
UA_DESKTOP = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'


def _business_days_until(target, base):
    """base(date)から target(date) までの営業日数 (0以上)。過去なら None。土日のみ非営業日。"""
    if target < base:
        return None
    days = 0
    cur = base
    while cur < target:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def find_xlsx_urls():
    """一覧ページから決算発表予定日 xlsx のURLを発見する。"""
    r = requests.get(INDEX_URL, headers={'User-Agent': UA_DESKTOP}, timeout=20)
    r.raise_for_status()
    hrefs = re.findall(r'href="([^"]*kessan[^"]*\.xlsx)"', r.text)
    urls = []
    for u in hrefs:
        if u.startswith('http'):
            pass
        elif u.startswith('/'):
            u = 'https://www.jpx.co.jp' + u
        else:
            u = 'https://www.jpx.co.jp/listing/event-schedules/financial-announcement/' + u
        if u not in urls:
            urls.append(u)
    return urls


def _norm_code(v):
    """セル値を4桁ローカルコードに正規化 (数値/文字列/5桁末尾0/英字入り 285A などに対応)。"""
    if v is None:
        return None
    s = str(v).strip().upper()
    if s.endswith('.0'):
        s = s[:-2]
    s = s.replace(' ', '').replace('　', '')
    if len(s) == 5 and s[-1] == '0':
        s = s[:4]
    if len(s) == 4 and re.fullmatch(r'[0-9][0-9A-Z]{3}', s):
        return s
    return None


def _parse_date(v):
    """セル値を date に。datetime/date/文字列(2026/8/5 等)に対応。未定等は None。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ('%Y/%m/%d', '%Y-%m-%d', '%Y年%m月%d日'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_xlsx(content):
    """xlsxバイト列から [{'code','date','name'}] を抽出する。

    列位置はヘッダー行(「コード」と「発表」を含む行)から動的に特定し、
    レイアウト変更にある程度耐える。
    """
    import openpyxl  # 遅延import (このモジュール自体はopenpyxl無しでも読み込める)
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    entries = []
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        idx = {}
        for row in rows:
            if not row:
                continue
            cells = ['' if c is None else str(c) for c in row]
            if any('コード' in c for c in cells) and any('発表' in c for c in cells):
                for i, c in enumerate(cells):
                    if 'コード' in c and 'code' not in idx:
                        idx['code'] = i
                    elif '発表' in c and 'date' not in idx:
                        idx['date'] = i
                    elif ('会社名' in c or '銘柄名' in c) and 'name' not in idx:
                        idx['name'] = i
                break
        if 'code' not in idx or 'date' not in idx:
            continue
        for row in rows:  # ヘッダー行の続きから
            if not row:
                continue
            code = _norm_code(row[idx['code']]) if idx['code'] < len(row) else None
            if not code:
                continue
            d = _parse_date(row[idx['date']]) if idx['date'] < len(row) else None
            if not d:
                continue
            name = ''
            if 'name' in idx and idx['name'] < len(row) and row[idx['name']] is not None:
                name = str(row[idx['name']]).strip()
            entries.append({'code': code, 'date': d, 'name': name})
    return entries


def get_jpx_earnings(tracked, today):
    """全xlsxを取得・パースし {code: {'date': date, 'name': str}} を返す。

    同一銘柄が複数回出る場合は「今日以降で最も近い日」を優先
    (将来日が無ければ直近の過去日)。tracked=None なら全銘柄。
    """
    urls = find_xlsx_urls()
    print(f"  [JPX] xlsxリンク {len(urls)} 件: {[u.rsplit('/', 1)[-1] for u in urls]}")

    def _key(d):
        return (0 if d >= today else 1, abs((d - today).days))

    result = {}
    for u in urls:
        try:
            r = requests.get(u, headers={'User-Agent': UA_DESKTOP}, timeout=30)
            r.raise_for_status()
            entries = parse_xlsx(r.content)
            print(f"  [JPX] {u.rsplit('/', 1)[-1]}: {len(entries)} 行パース")
        except Exception as e:
            print(f"  [JPX] {u.rsplit('/', 1)[-1]} 取得/パース失敗(継続): {e}")
            continue
        for e2 in entries:
            code = e2['code']
            if tracked is not None and code not in tracked:
                continue
            cur = result.get(code)
            if cur is None or _key(e2['date']) < _key(cur['date']):
                result[code] = {'date': e2['date'], 'name': e2['name']}
    return result


def get_jpx_warnings(tracked, today, max_bdays=3):
    """fetch_data.py の earningsWarnings 形式で max_bdays 営業日以内の警告を返す。"""
    ann = get_jpx_earnings(set(tracked), today)
    warnings = {}
    for code, info in ann.items():
        bd = _business_days_until(info['date'], today)
        if bd is None or bd > max_bdays:
            continue
        warnings[code] = {
            'date': info['date'].strftime('%Y-%m-%d'),
            'businessDaysUntil': bd,
            'fiscalYear': '',
            'fiscalPeriod': '',
            'companyName': info['name'],
            'source': 'JPX',
        }
    return warnings


if __name__ == '__main__':
    # 診断モード: 監視銘柄の今後の決算発表予定を一覧表示 (メール送信なし・data.json非変更)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from common import TRACKED_STOCKS, DEFAULT_STOCKS
    today = datetime.now(JST).date()
    print(f"=== JPX決算発表予定日 診断 (本日={today}) ===")
    ann = get_jpx_earnings(set(TRACKED_STOCKS), today)
    name_of = {s['code']: s['name'] for s in DEFAULT_STOCKS}
    print(f"監視銘柄でJPX予定が見つかった数: {len(ann)}")
    for code, info in sorted(ann.items(), key=lambda x: x[1]['date']):
        bd = _business_days_until(info['date'], today)
        bd_s = f"{bd}営業日後" if bd is not None else "過去"
        print(f"  {code} {name_of.get(code, info['name'])}: {info['date']} ({bd_s})")
    w = get_jpx_warnings(TRACKED_STOCKS, today)
    print(f"3営業日以内の警告: {len(w)} 件")
    print(json.dumps(w, ensure_ascii=False, indent=2))
