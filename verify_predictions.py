#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
松村式Stocksurfing - 答え合わせスクリプト (v3.4.5)
夜23:00 JST に GitHub Actions から起動。
data.json (朝の予測) と当日の実勢終値を比較し、
verification_log.json に追記する。

個別株終値は J-Quants V2 → yfinance フォールバックの二段構え。

v3.4.5 (2026-07-18):
- 【重要】指数実勢の「日付検証」を追加。yfinance ^N225 は当日の確定足が
  夜になっても反映されないことがあり(2026-07確認)、末尾行をそのまま使うと
  『前日の値動き』を当日実勢として記録してしまっていた(的中率の汚染)。
  対策: trade_date と一致する行だけを採用し、無ければ日経公式ページで補完。
- TOPIX実勢を J-Quants公式指数(/indices/bars/daily/topix)から取得。
  (^TPX/^TOPX は取得不能になり、これまで常に欠落していた)
- 祝日スキップを追加: J-Quants取引カレンダーで trade_date が休場日なら
  答え合わせをスキップ(海の日等の祝日にゴミデータが混入するのを防ぐ)。
"""
import json
import sys
import os
from datetime import datetime, timezone, timedelta

import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    DEFAULT_STOCKS, INDICATORS,
    _num, _clean, calc_score, stock_score, stars_for, WEIGHTS_VERSION,
    tse_is_trading_day, scrape_nikkei_index_snapshot,
)
from jquants_client import get_daily_quote, get_topix_daily

JST = timezone(timedelta(hours=9))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def fetch_close_jquants(code, trade_date=None):
    """J-Quants V2 で指定日の四本値取得 (trade_date 未指定なら当日)。

    V2 /equities/bars/daily のレスポンスは {"data":[{...}]}、
    始値=O / 終値=C(いずれも調整前)。
    """
    if trade_date is None:
        trade_date = datetime.now(JST).date().strftime('%Y-%m-%d')
    data = get_daily_quote(code, trade_date)
    if not data:
        return None
    quotes = data.get('data', [])
    if not quotes:
        return None
    q = quotes[0]
    close = _num(q.get('C'))
    open_ = _num(q.get('O'))
    if close is None or open_ is None:
        return None
    return {
        'open': open_,
        'close': close,
        'chgPct': ((close - open_) / open_) * 100 if open_ != 0 else 0,
    }


def fetch_close_yfinance(code, suffix='.T'):
    """yfinanceでフォールバック"""
    try:
        ticker = yf.Ticker(code + suffix)
        hist = ticker.history(period='2d', interval='1d')
        if hist.empty:
            return None
        latest = hist.iloc[-1]
        close = _num(latest['Close'])
        open_p = _num(latest['Open'])
        if close is None or open_p is None:
            return None
        if len(hist) >= 2:
            prev_close = _num(hist.iloc[-2]['Close'])
            chg_pct = ((close - prev_close) / prev_close) * 100 if prev_close else 0
        else:
            chg_pct = ((close - open_p) / open_p) * 100 if open_p != 0 else 0
        return {'open': open_p, 'close': close, 'chgPct': chg_pct}
    except Exception:
        return None


def fetch_index_close_yfinance(symbol, trade_date_str):
    """日経・TOPIX等の指数終値 (v3.4.5: trade_date と一致する日足のみ採用)。

    yfinance の指数は最新確定足の反映が遅れることがあるため、
    末尾行を鵜呑みにせず、該当日の行が存在する時だけ返す。
    """
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period='10d', interval='1d')
        if hist is None or hist.empty:
            return None
        for ts in reversed(hist.index):
            if ts.strftime('%Y-%m-%d') == trade_date_str:
                row = hist.loc[ts]
                close = _num(row['Close'])
                open_p = _num(row['Open'])
                if close is None or open_p is None:
                    return None
                return {'open': open_p, 'close': close,
                        'chgPct': ((close - open_p) / open_p) * 100 if open_p != 0 else 0}
        return None
    except Exception:
        return None


def fetch_n225_actual(trade_date_str):
    """当日の日経平均実勢。yfinance(日付検証つき) → 日経公式ページの二段構え。

    フォールバック時の chgPct は前日終値比(close-to-close)になる点に注意
    (方向性判定にはむしろ適した定義)。
    """
    r = fetch_index_close_yfinance('^N225', trade_date_str)
    if r:
        return r, 'yfinance'
    snap = scrape_nikkei_index_snapshot('nk225', '日経平均株価', 20000, 150000)
    if snap and snap.get('asOf') == trade_date_str:
        return ({'open': None, 'close': snap['price'],
                 'chgPct': snap.get('chgPct')}, 'Nikkei公式(前日終値比)')
    return None, None


def fetch_topix_actual(trade_date_str):
    """当日のTOPIX実勢。J-Quants公式指数 → yfinance(日付検証つき)の二段構え。"""
    data = get_topix_daily(trade_date_str, trade_date_str)
    rows = (data or {}).get('data') or []
    if rows:
        q = rows[0]
        o = _num(q.get('O'))
        c = _num(q.get('C'))
        if c is not None:
            return {'open': o, 'close': c,
                    'chgPct': ((c - o) / o) * 100 if o else None}
    return (fetch_index_close_yfinance('^TPX', trade_date_str)
            or fetch_index_close_yfinance('^TOPX', trade_date_str))


def main():
    print("=" * 50)
    print("  松村式Stocksurfing - 答え合わせ (v3.4.5)")
    print(f"  実行時刻: {datetime.now(JST).isoformat()}")
    print("=" * 50)

    # 1. 朝のdata.jsonを読込
    data_path = os.path.join(SCRIPT_DIR, 'data.json')
    if not os.path.exists(data_path):
        print(f"[ERROR] data.json なし。朝のワークフローが走っていない可能性")
        sys.exit(1)
    with open(data_path, 'r', encoding='utf-8') as f:
        morning = json.load(f)

    morning_indicators = morning.get('indicators', {})
    morning_score = calc_score(morning_indicators)
    fetched_at = morning.get('fetchedAt', '')

    # ② 取引日は「実行時刻」ではなく「朝 data.json の日付」を採用する。
    #    GitHub Actions の遅延で実行が JST 翌日(土曜)へずれても、日付が化けない。
    trade_date = None
    if fetched_at:
        try:
            trade_date = datetime.fromisoformat(fetched_at).astimezone(JST).date()
        except Exception:
            trade_date = None
    if trade_date is None:
        trade_date = datetime.now(JST).date()
    today = trade_date.strftime('%Y-%m-%d')

    # ② 土日(市場休場)は答え合わせをスキップ。休場データはNaN/無意味で母数を汚す。
    if trade_date.weekday() >= 5:
        print(f"\n[SKIP] {today} は土日(市場休場)のため答え合わせを行いません。")
        sys.exit(0)
    # v3.4.5: 祝日(海の日等)もスキップ。J-Quants取引カレンダーで判定し、
    # カレンダー取得失敗時は平日=営業日として従来通り続行する。
    if not tse_is_trading_day(trade_date):
        print(f"\n[SKIP] {today} は東証休場日(祝日等)のため答え合わせを行いません。")
        sys.exit(0)

    print(f"\n[朝の予測] (取引日 {today} / weights={WEIGHTS_VERSION or 'default'})")
    print(f"  日時: {fetched_at}")
    print(f"  スコア: {morning_score:.1f}" if morning_score is not None else "  スコア: N/A")

    # ★4以上の予測候補
    morning_picks = []
    for st in DEFAULT_STOCKS:
        star = stars_for(st, morning_score, morning_indicators)
        if star >= 4:
            morning_picks.append({**st, 'predictedStar': star, 'predictedSS': stock_score(st, morning_indicators)})
    print(f"  ★4以上候補: {len(morning_picks)}銘柄")

    # 2. 当日の実勢取得
    print(f"\n[実勢データ取得]")
    print(f"  指数 (日付検証つき):")
    n225, n225_src = fetch_n225_actual(today)
    topix = fetch_topix_actual(today)
    if n225:
        chg_s = f"{n225['chgPct']:+.2f}%" if n225.get('chgPct') is not None else "N/A"
        print(f"    日経225: {n225['close']:.2f} ({chg_s}) [{n225_src}]")
    else:
        print(f"    日経225: {today} の確定値が取得できず (実勢は記録しない)")
    if topix:
        chg_s = f"{topix['chgPct']:+.2f}%" if topix.get('chgPct') is not None else "N/A"
        print(f"    TOPIX  : {topix['close']:.2f} ({chg_s})")

    print(f"  個別銘柄 (J-Quants V2 → yfinanceフォールバック):")
    stock_results = {}
    for st in DEFAULT_STOCKS:
        r = fetch_close_jquants(st['code'], today) or fetch_close_yfinance(st['code'])
        if r:
            stock_results[st['code']] = r
            print(f"    {st['code']} {st['name']:15s}: close={r['close']:>9.2f} chgPct={r['chgPct']:+6.2f}%")
        else:
            print(f"    {st['code']} {st['name']:15s}: 取得失敗")

    # 3. 答え合わせ計算
    print(f"\n[答え合わせ集計]")

    # 指数の方向性チェック
    n225_dir_correct = None
    if morning_score is not None and n225 and n225.get('chgPct') is not None:
        predicted_up = morning_score > 0
        actual_up = n225['chgPct'] > 0
        n225_dir_correct = (predicted_up == actual_up) or (abs(morning_score) < 20 and abs(n225['chgPct']) < 0.3)
        print(f"  日経方向性: 予測={'+' if predicted_up else '-'} 実勢={'+' if actual_up else '-'} → {'✓' if n225_dir_correct else '✗'}")

    # 候補銘柄の平均パフォーマンス
    pick_returns = []
    for p in morning_picks:
        r = stock_results.get(p['code'])
        if r:
            pick_returns.append(r['chgPct'])
    avg_pick_return = sum(pick_returns) / len(pick_returns) if pick_returns else None
    if avg_pick_return is not None:
        print(f"  ★4以上候補 平均リターン: {avg_pick_return:+.2f}% ({len(pick_returns)}銘柄)")

    # 全銘柄平均(参考: ベンチマーク)
    all_returns = [r['chgPct'] for r in stock_results.values() if r]
    avg_all_return = sum(all_returns) / len(all_returns) if all_returns else None
    if avg_all_return is not None:
        print(f"  全{len(DEFAULT_STOCKS)}銘柄 平均リターン   : {avg_all_return:+.2f}%")

    # ★4が全銘柄平均をアウトパフォームしたか
    pick_outperform = None
    if avg_pick_return is not None and avg_all_return is not None:
        pick_outperform = avg_pick_return > avg_all_return
        print(f"  候補のアウトパフォーム: {'✓' if pick_outperform else '✗'}")

    # 4. ログに追記
    log_path = os.path.join(SCRIPT_DIR, 'verification_log.json')
    log = []
    if os.path.exists(log_path):
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                log = json.load(f)
        except Exception:
            log = []

    entry = {
        'date': today,
        'verifiedAt': datetime.now(JST).isoformat(),
        'weightsVersion': WEIGHTS_VERSION,
        'morningScore': round(morning_score, 1) if morning_score is not None else None,
        'morningFetchedAt': fetched_at,
        # ③ 自己学習エンジン用: 朝の各指標 chgPct スナップショット。
        #    これが日々溜まることで後日の重み最適化(optimize_weights.py)が可能になる。
        'morningIndicators': {
            ind['key']: _num((morning_indicators.get(ind['key']) or {}).get('chgPct'))
            for ind in INDICATORS
        },
        'predictedPicks': [{'code':p['code'],'name':p['name'],'star':p['predictedStar']} for p in morning_picks],
        'actualIndices': {
            'N225': n225,
            'TOPX': topix,
        },
        'actualStocks': stock_results,
        'metrics': {
            'n225_direction_correct': n225_dir_correct,
            'avg_pick_return': round(avg_pick_return, 4) if avg_pick_return is not None else None,
            'avg_all_return': round(avg_all_return, 4) if avg_all_return is not None else None,
            'picks_outperformed': pick_outperform,
        },
    }

    # 同日のログがあれば上書き
    log = [l for l in log if l.get('date') != today]
    log.append(entry)
    # 日付順に整列(遅延実行で前後しても綺麗に並ぶ)
    log.sort(key=lambda l: l.get('date', ''))
    # 古いログは120件で打ち切り
    if len(log) > 120:
        log = log[-120:]

    # ① NaN/Inf を除去し、厳密JSONとして書き出す。
    #    これを怠るとブラウザの JSON.parse が例外を投げ、答え合わせタブが空表示になる。
    log = _clean(log)
    with open(log_path, 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2, allow_nan=False)

    print(f"\n  ✓ verification_log.json に追記 ({len(log)}件)")

    # 累計サマリー
    print(f"\n[累計サマリー]")
    direction_correct = [l['metrics'].get('n225_direction_correct') for l in log
                          if l['metrics'].get('n225_direction_correct') is not None]
    if direction_correct:
        rate = sum(direction_correct) / len(direction_correct) * 100
        print(f"  日経方向性的中率: {rate:.1f}% ({sum(direction_correct)}/{len(direction_correct)})")

    pick_outperform_log = [l['metrics'].get('picks_outperformed') for l in log
                            if l['metrics'].get('picks_outperformed') is not None]
    if pick_outperform_log:
        op_rate = sum(pick_outperform_log) / len(pick_outperform_log) * 100
        print(f"  ★4候補のアウトパフォーム率: {op_rate:.1f}% ({sum(pick_outperform_log)}/{len(pick_outperform_log)})")

if __name__ == '__main__':
    main()
