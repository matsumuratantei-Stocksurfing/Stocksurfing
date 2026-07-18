#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
松村式Stocksurfing - verification_log 修復スクリプト (一回きり・手動実行専用) v1.0

【背景 (2026-07-18)】旧 verify_predictions.py は yfinance ^N225 の末尾行を
日付検証なしで「当日実勢」として記録していた。^N225 は最新確定足の反映が
翌朝まで遅れることがあり、遅延日には『前日の値動き』が当日の正解ラベルとして
混入した(自己学習のリーク源)。TOPIX実勢も ^TPX/^TOPX 死亡で常に欠落していた。

【このスクリプトがやること】
- 全エントリの actualIndices.N225 / actualIndices.TOPX を、確定済みの過去日足
  (yfinance ^N225 の日付一致行 / J-Quants公式TOPIX)から再計算して上書きする。
- metrics.n225_direction_correct を再計算する(判定式は verify と同一)。
- 東証休場日(祝日等)のエントリはゴミデータなので削除する。
- 修復前の原本を verification_log_backup_prerepair.json に保存する
  (既に存在する場合は上書きしない=再実行しても原本は守られる)。

【上書きしないもの(後知恵バイアス防止)】
- morningIndicators / morningScore / predictedPicks: 朝7:30時点の情報と判断の記録。
- actualStocks と avg_pick_return 等: J-Quants由来で元々正確。
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import _num, _clean
from jquants_client import get_topix_daily, get_market_calendar

JST = timezone(timedelta(hours=9))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(SCRIPT_DIR, 'verification_log.json')
BACKUP_PATH = os.path.join(SCRIPT_DIR, 'verification_log_backup_prerepair.json')


def load_n225_history(period='1y'):
    """^N225 の確定済み日足を {日付文字列: {'open','close','chgPct'}} で返す。

    過去分の日足は完全なので、日付で引けば正しい当日実勢になる。
    chgPct は verify_predictions と同じ「寄り→引け(open→close)」定義。
    """
    hist = yf.Ticker('^N225').history(period=period, interval='1d')
    out = {}
    if hist is None or hist.empty:
        return out
    for ts, row in hist.iterrows():
        o = _num(row.get('Open'))
        c = _num(row.get('Close'))
        if o is None or c is None or o == 0:
            continue
        out[ts.strftime('%Y-%m-%d')] = {
            'open': o, 'close': c, 'chgPct': (c - o) / o * 100,
        }
    return out


def load_topix_history(from_date, to_date):
    """J-Quants公式TOPIXを {日付文字列: {'open','close','chgPct'}} で返す。"""
    data = get_topix_daily(from_date, to_date)
    out = {}
    for q in ((data or {}).get('data') or []):
        d = q.get('Date')
        o = _num(q.get('O'))
        c = _num(q.get('C'))
        if not d or c is None:
            continue
        out[d] = {'open': o, 'close': c,
                  'chgPct': (c - o) / o * 100 if o else None}
    return out


def load_trading_days(from_date, to_date):
    """東証営業日(HolDiv 1/2)の集合。取得失敗時は None (=休場判定を行わない)。"""
    cal = get_market_calendar(from_date, to_date)
    if not cal:
        return None
    rows = cal.get('data') or []
    if not rows:
        return None
    return {r.get('Date') for r in rows if r.get('HolDiv') in ('1', '2')}


def direction_correct(morning_score, chg):
    """verify_predictions.py と同一の方向的中判定。"""
    if morning_score is None or chg is None:
        return None
    predicted_up = morning_score > 0
    actual_up = chg > 0
    return (predicted_up == actual_up) or (abs(morning_score) < 20 and abs(chg) < 0.3)


def main():
    now = datetime.now(JST)
    print("=" * 56)
    print("  verification_log 修復 (正解ラベル再計算・一回きり)")
    print(f"  実行: {now.isoformat()}")
    print("=" * 56)

    if not os.path.exists(LOG_PATH):
        print("[ERROR] verification_log.json がありません")
        sys.exit(1)
    with open(LOG_PATH, 'r', encoding='utf-8') as f:
        raw = f.read()
    log = json.loads(raw)
    if not log:
        print("[SKIP] ログが空です")
        sys.exit(0)

    # 原本バックアップ(既存なら上書きしない)
    if os.path.exists(BACKUP_PATH):
        print(f"  バックアップ既存: {BACKUP_PATH} (上書きしません)")
    else:
        with open(BACKUP_PATH, 'w', encoding='utf-8') as f:
            f.write(raw)
        print(f"  ✓ 原本を {os.path.basename(BACKUP_PATH)} に保存")

    dates = sorted(l.get('date') for l in log if l.get('date'))
    frm, to = dates[0], dates[-1]
    print(f"  対象: {len(log)}件 ({frm} 〜 {to})")

    print("\n[過去データ取得]")
    n225_hist = load_n225_history()
    print(f"  ^N225 日足: {len(n225_hist)}日分")
    topix_hist = load_topix_history(frm, to)
    print(f"  J-Quants TOPIX: {len(topix_hist)}日分")
    trading_days = load_trading_days(frm, to)
    print(f"  取引カレンダー: {'取得失敗(休場判定なしで続行)' if trading_days is None else f'{len(trading_days)}営業日'}")
    if not n225_hist:
        print("[ERROR] ^N225 履歴が取得できないため中止(ログは変更しません)")
        sys.exit(1)

    print("\n[再計算]")
    new_log = []
    stats = {'kept': 0, 'dropped_holiday': 0, 'n225_fixed': 0, 'n225_missing': 0,
             'topix_fixed': 0, 'dir_flipped': 0}
    for e in log:
        d = e.get('date')
        if not d:
            continue
        if trading_days is not None and d not in trading_days:
            stats['dropped_holiday'] += 1
            print(f"  {d}: 休場日エントリのため削除")
            continue

        old_n225 = (e.get('actualIndices') or {}).get('N225') or {}
        old_chg = _num(old_n225.get('chgPct'))
        old_dir = (e.get('metrics') or {}).get('n225_direction_correct')

        n_new = n225_hist.get(d)
        e.setdefault('actualIndices', {})
        e.setdefault('metrics', {})
        if n_new:
            e['actualIndices']['N225'] = dict(n_new)
            new_chg = n_new['chgPct']
            stats['n225_fixed'] += 1
        else:
            e['actualIndices']['N225'] = None
            new_chg = None
            stats['n225_missing'] += 1

        t_new = topix_hist.get(d)
        if t_new:
            e['actualIndices']['TOPX'] = dict(t_new)
            stats['topix_fixed'] += 1

        new_dir = direction_correct(_num(e.get('morningScore')), new_chg)
        e['metrics']['n225_direction_correct'] = new_dir
        e['repairedAt'] = now.isoformat()

        flipped = (old_dir is not None or new_dir is not None) and (old_dir != new_dir)
        if flipped:
            stats['dir_flipped'] += 1
        diff = (abs(old_chg - new_chg) if (old_chg is not None and new_chg is not None) else None)
        mark = ' ★方向判定変化' if flipped else ''
        diff_s = f" Δchg={diff:.2f}pt" if diff is not None else ''
        print(f"  {d}: chg {old_chg if old_chg is not None else '—'} → "
              f"{f'{new_chg:.2f}' if new_chg is not None else '—'}{diff_s} "
              f"/ 的中 {old_dir} → {new_dir}{mark}")
        new_log.append(e)
        stats['kept'] += 1

    new_log.sort(key=lambda l: l.get('date', ''))
    new_log = _clean(new_log)
    with open(LOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(new_log, f, ensure_ascii=False, indent=2, allow_nan=False)

    print("\n[サマリー]")
    print(f"  保持: {stats['kept']}件 / 休場日削除: {stats['dropped_holiday']}件")
    print(f"  N225再計算: {stats['n225_fixed']}件 (履歴なし: {stats['n225_missing']}件)")
    print(f"  TOPIX補完: {stats['topix_fixed']}件")
    print(f"  方向的中の判定が変わった日: {stats['dir_flipped']}件")

    dirs = [l['metrics'].get('n225_direction_correct') for l in new_log
            if l.get('metrics', {}).get('n225_direction_correct') is not None]
    if dirs:
        rate = sum(dirs) / len(dirs) * 100
        print(f"  修復後の日経方向性的中率: {rate:.1f}% ({sum(dirs)}/{len(dirs)})")
    print("\n  ✓ 修復完了。次回の週次自己学習からクリーンなデータで再最適化されます。")


if __name__ == '__main__':
    main()
