#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
松村式Stocksurfing - 答え合わせスクリプト (v3.1)
夜23:00 JST に GitHub Actions から起動。
data.json (朝の予測) と当日の実勢終値を比較し、
verification_log.json に追記する。
"""
import json
import sys
import os
import math
from datetime import datetime, timezone, timedelta

import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jquants_client import get_daily_quote

JST = timezone(timedelta(hours=9))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _num(x):
    """NaN/Inf を None に正規化する (JSONの厳密規格は NaN を許さない)。"""
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

# 銘柄定義 (HTML/send_email と同期)
DEFAULT_STOCKS = [
    {'code':'8035', 'name':'東京エレクトロン',  'tags':['SOX','NQ']},
    {'code':'6920', 'name':'レーザーテック',    'tags':['SOX','NQ']},
    {'code':'6857', 'name':'アドバンテスト',    'tags':['SOX','NQ']},
    {'code':'6146', 'name':'ディスコ',          'tags':['SOX','NQ']},
    {'code':'6526', 'name':'ソシオネクスト',    'tags':['SOX','NQ']},
    {'code':'5803', 'name':'フジクラ',          'tags':['AIインフラ','NQ']},
    {'code':'4063', 'name':'信越化学',          'tags':['SOX','景気']},
    {'code':'9984', 'name':'ソフトバンクG',     'tags':['NQ','日経寄与']},
    {'code':'7011', 'name':'三菱重工業',        'tags':['防衛','重工']},
    {'code':'7012', 'name':'川崎重工業',        'tags':['防衛','造船']},
    {'code':'7013', 'name':'IHI',               'tags':['防衛','宇宙']},
    {'code':'6501', 'name':'日立製作所',        'tags':['NY','景気']},
    {'code':'6758', 'name':'ソニーG',           'tags':['NY','NQ','景気']},
    {'code':'7203', 'name':'トヨタ自動車',      'tags':['NY','USDJPY','景気']},
    {'code':'6506', 'name':'安川電機',          'tags':['フィジカルAI','NQ']},
    {'code':'8058', 'name':'三菱商事',          'tags':['商社','資源']},
    {'code':'8031', 'name':'三井物産',          'tags':['商社','資源']},
    {'code':'8306', 'name':'三菱UFJ FG',        'tags':['金融','USDJPY']},
    {'code':'8316', 'name':'三井住友FG',        'tags':['金融','USDJPY']},
    {'code':'9107', 'name':'川崎汽船',          'tags':['資源','景気']},
]

INDICATORS = [
    {'key':'N225F',  'weight':3.0, 'inverse':False},
    {'key':'TOPX',   'weight':2.5, 'inverse':False},
    {'key':'NDX',    'weight':2.0, 'inverse':False},
    {'key':'SPX',    'weight':1.8, 'inverse':False},
    {'key':'SOX',    'weight':2.0, 'inverse':False},
    {'key':'DJI',    'weight':1.5, 'inverse':False},
    {'key':'USDJPY', 'weight':1.5, 'inverse':False},
    {'key':'EURJPY', 'weight':0.8, 'inverse':False},
    {'key':'TNX',    'weight':0.8, 'inverse':True},
    {'key':'WTI',    'weight':0.6, 'inverse':False},
    {'key':'VIX',    'weight':0.8, 'inverse':True},
    {'key':'NKVI',   'weight':1.0, 'inverse':True},
]
TAG_MAP = {
    'SOX':'SOX','NQ':'NDX','NY':'DJI','USDJPY':'USDJPY',
    '景気':'SPX','内需':'TOPX','金融':'TNX','商社':'WTI','資源':'WTI','日経寄与':'N225F',
    '防衛':'N225F','造船':'N225F','重工':'SPX','宇宙':'NDX','AIインフラ':'NDX','フィジカルAI':'NDX',
}

def _load_weights():
    """weights.json があれば INDICATORS の重みを上書きする (自己学習エンジン連携)。
    存在しない/壊れている場合はベタ書きのデフォルト重みのまま動く。"""
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

WEIGHTS_VERSION = _load_weights()

def calc_score(indicators):
    s, w = 0, 0
    for ind in INDICATORS:
        v = indicators.get(ind['key'])
        if not v or v.get('chgPct') is None: continue
        d = -1 if ind['inverse'] else 1
        cp = max(-5, min(5, v['chgPct']))
        s += cp * 15 * d * ind['weight']
        w += ind['weight']
    return None if w == 0 else max(-100, min(100, s / (w * 0.75)))

def stock_score(stock, indicators):
    s, n = 0, 0
    for tag in stock['tags']:
        k = TAG_MAP.get(tag)
        if not k: continue
        v = indicators.get(k)
        if not v or v.get('chgPct') is None: continue
        s += v['chgPct']
        n += 1
    return s / n if n else None

def stars_for(stock, market_score, indicators):
    ss = stock_score(stock, indicators)
    if ss is None or market_score is None: return 0
    combined = ss * 10 + market_score * 0.3
    if combined >= 25: return 5
    if combined >= 12: return 4
    if combined >= 3:  return 3
    if combined >= -8: return 2
    return 1

def fetch_close_jquants(code, trade_date=None):
    """J-Quants で指定日の終値取得 (trade_date 未指定なら当日)"""
    if trade_date is None:
        trade_date = datetime.now(JST).date().strftime('%Y-%m-%d')
    data = get_daily_quote(code, trade_date)
    if not data:
        return None
    quotes = data.get('daily_quotes', [])
    if not quotes:
        return None
    q = quotes[0]
    close = _num(q.get('Close'))
    open_ = _num(q.get('Open'))
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

def fetch_index_close_yfinance(symbol):
    """日経・TOPIX等の指数終値"""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period='2d', interval='1d')
        if hist.empty:
            return None
        latest = hist.iloc[-1]
        close = _num(latest['Close'])
        open_p = _num(latest['Open'])
        if close is None or open_p is None:
            return None
        return {'open': open_p, 'close': close,
                'chgPct': ((close - open_p) / open_p) * 100 if open_p != 0 else 0}
    except Exception:
        return None

def main():
    print("=" * 50)
    print("  松村式Stocksurfing - 答え合わせ (v3.1)")
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
    print(f"  指数 (yfinance):")
    n225 = fetch_index_close_yfinance('^N225')
    topix = fetch_index_close_yfinance('^TPX') or fetch_index_close_yfinance('^TOPX')
    if n225:
        print(f"    日経225: {n225['close']:.2f} ({n225['chgPct']:+.2f}%)")
    if topix:
        print(f"    TOPIX  : {topix['close']:.2f} ({topix['chgPct']:+.2f}%)")
    
    print(f"  個別銘柄 (J-Quants → yfinanceフォールバック):")
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
    if morning_score is not None and n225:
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
        print(f"  全20銘柄 平均リターン   : {avg_all_return:+.2f}%")
    
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
