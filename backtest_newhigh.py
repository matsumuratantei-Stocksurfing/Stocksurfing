#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stocksurfing 計測 — 新高値(52週高値・取得期間内=≈上場来)戻り率バックテスト (計測専用)

本番ロジック(index.html / send_email.py / weights.json / 予測)には一切触れない。
発注・注文・証券APIモジュールは一切importしない(計測のみ)。
データ取得・損切りシミュは po_detector.py を単一ソースとして再利用する。

■ 検証する仮説
  「新高値(または新高値圏)で仕掛けると、その後20営業日の戻りにエッジがあるか」を、
  押し目バックテスト(backtest_perfectorder.py)と同じ枠組み・同じ地平線・同じ損切りで測る。

■ 定義(すべて検出時点までのデータのみ=前向き。未来参照なし)
  - prior52  : 当日を除く直近250営業日の最高値(=前日までの52週高値)
  - 52w新高値ブレイク(初日): 当日高値 >= prior52 かつ 前日は未更新(ブレイク初日だけ)
  - 52w新高値圏[0-3%]     : 終値が prior52 の 0〜3%下(近接)
  - 取得内ATHブレイク(初日): 当日高値 >= 取得開始来の最高値(前日まで) の初日
  - all_days(ベースライン): prior52 が定義できる全営業日(新高値条件なし=対照群)

■ 前向き成績(エントリー=当日終値)
  - fwdM     : M営業日後の終値リターン% (M=5/10/20、主指標は20)
  - maxdd    : M日間の最大ドローダウン%
  - ルールブック損切り: 損切-2% / 利確+5%(and +4%) のどちらに先に当たるか(松村式の実ルール)
  - 構造損切り: 押し安値(直近10日安値)×0.98 を損切、+5% を利確とした決着

■ 比較
  新高値イベント vs all_days(全日) vs TOPIX(1306) の positive%・fwd平均・ルールブック勝率。

【限界】取得期間は上昇相場に偏り。取引コスト/スリッページ未考慮。価格は調整済み(AdjC)。
"""
import os
import sys
import json
import time
import statistics
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_STOCKS
from po_detector import JST, fetch_days, sim_stop

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SLEEP_BETWEEN_CALLS = 0.3

# できるだけ長い履歴を取り、52週高値の助走(250営業日)を確保しつつ検証標本を厚くする
NEWHIGH_FETCH_FROM = '2022-01-01'
W52 = 250              # 52週 ≒ 250営業日
MIN_TRAIL = 200        # 52週高値を評価するのに必要な最低助走本数
M_LIST = [5, 10, 20]
M_PRIMARY = 20         # 主地平線(構造/スイングと同じ20営業日)
STOP_HORIZON = 20
TP_MAIN = 5.0          # 利確+5%(松村式・利確②)
TP_ALT = 4.0           # 利確+4%(利確①寄り)
SL_PCT = -2.0          # 損切-2%(松村式・厳守)

# セクターETF(レバ/インバースは対象外)+ TOPIXベースライン。押し目BTと同一。
ETF_UNIVERSE = [
    {'code': '213A', 'name': '半導体ETF-A'}, {'code': '221A', 'name': '半導体ETF-B'},
    {'code': '200A', 'name': '半導体ETF-C'}, {'code': '282A', 'name': '半導体ETF-D'},
    {'code': '2243', 'name': '半導体ETF-E'}, {'code': '513A', 'name': '防衛テックETF'},
    {'code': '1619', 'name': '建設・資材ETF'},
]
BASELINE_UNIVERSE = [{'code': '1306', 'name': 'TOPIX ETF(ベースライン)'}]


# ---------- 集計ヘルパ ----------
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def _rate(flags):
    flags = [f for f in flags if f is not None]
    return round(sum(flags) / len(flags) * 100, 1) if flags else None


# ---------- 1銘柄の新高値イベント走査(前向き) ----------
def scan_newhigh(days, group):
    n = len(days)
    closes = [d['c'] for d in days]
    highs = [d['h'] for d in days]
    lows = [d['l'] for d in days]

    events = []
    run_ath_prior = None   # 前日までの取得期間内最高値
    prev_new52 = False
    prev_ath = False

    for i in range(n):
        c = closes[i]
        hi = highs[i]
        # 前日までの52週高値(当日を除く)
        prior_seg = [h for h in highs[max(0, i - W52):i] if h is not None]
        prior52 = max(prior_seg) if len(prior_seg) >= MIN_TRAIL else None

        is_new52 = bool(prior52 is not None and hi is not None and hi >= prior52)
        is_ath = bool(run_ath_prior is not None and hi is not None and hi >= run_ath_prior and i >= W52)

        # 権利落ち/欠損日はイベント化しない(が状態は更新して次に進む)
        valid = (c is not None and days[i]['adjFactor'] == 1.0 and c > 0)

        if valid and prior52 is not None:
            # 近接バンド(終値 vs 前日までの52週高値)
            pct = (prior52 - c) / prior52 * 100
            if c >= prior52:
                band = '新高値引け(<=0%)'
            elif pct < 1:
                band = '[0,1)'
            elif pct < 3:
                band = '[1,3)'
            elif pct < 5:
                band = '[3,5)'
            else:
                band = '[5,∞)'

            # 前方メトリクス
            fwd = {}
            for M in M_LIST:
                if i + M < n and closes[i + M] is not None:
                    fwd[f'fwd{M}'] = round((closes[i + M] - c) / c * 100, 3)
                    dd = 0.0
                    for k in range(1, M + 1):
                        lk = lows[i + k]
                        if lk is not None:
                            dd = min(dd, (lk - c) / c * 100)
                    fwd[f'dd{M}'] = round(dd, 3)
                else:
                    fwd[f'fwd{M}'] = None
                    fwd[f'dd{M}'] = None

            # 損切りシミュ(20営業日以内にどちらに先に当たるか)
            seg_l = [l for l in lows[max(0, i - 9):i + 1] if l is not None]
            swing_low = min(seg_l) if seg_l else None
            rb5 = sim_stop(c, c * (1 + TP_MAIN / 100), c * (1 + SL_PCT / 100), highs, lows, i, STOP_HORIZON)
            rb4 = sim_stop(c, c * (1 + TP_ALT / 100), c * (1 + SL_PCT / 100), highs, lows, i, STOP_HORIZON)
            strc = sim_stop(c, c * (1 + TP_MAIN / 100), (swing_low * 0.98 if swing_low else None),
                            highs, lows, i, STOP_HORIZON)
            base = {'group': group, 'band': band, 'entry': c,
                    'rb5': rb5, 'rb4': rb4, 'strc': strc, **fwd}

            # このバーが該当するイベント種別を全て記録
            types = ['all_days']
            if is_new52 and not prev_new52:
                types.append('52w_breakout')
            if band in ('新高値引け(<=0%)', '[0,1)', '[1,3)'):
                types.append('52w_near3')
            if is_ath and not prev_ath:
                types.append('ath_breakout')
            for t in types:
                events.append({'type': t, **base})

        # 状態更新(欠損日でも可能な範囲で)
        if hi is not None:
            run_ath_prior = hi if run_ath_prior is None else max(run_ath_prior, hi)
        prev_new52 = is_new52
        prev_ath = is_ath

    return events


# ---------- セル集計 ----------
def _wl(rows, key):
    w = sum(1 for r in rows if r[key] == 'WIN')
    l = sum(1 for r in rows if r[key] == 'LOSE')
    e = sum(1 for r in rows if r[key] == 'EVEN')
    dec = w + l
    return {'win': w, 'lose': l, 'even': e,
            'winPct_decided': round(w / dec * 100, 1) if dec else None}


def cell(rows, M=M_PRIMARY):
    pos = [(1 if (r[f'fwd{M}'] > 0) else 0) if r[f'fwd{M}'] is not None else None for r in rows]
    return {
        'n': len(rows),
        'positive_pct': _rate(pos),
        'fwd_mean': _mean([r[f'fwd{M}'] for r in rows]),
        'fwd_median': _median([r[f'fwd{M}'] for r in rows]),
        'maxdd_mean': _mean([r[f'dd{M}'] for r in rows]),
        'maxdd_median': _median([r[f'dd{M}'] for r in rows]),
        'rulebook_2_5': _wl(rows, 'rb5'),   # 損切-2%/利確+5%
        'rulebook_2_4': _wl(rows, 'rb4'),   # 損切-2%/利確+4%
        'structural': _wl(rows, 'strc'),    # 押し安値×0.98損切/利確+5%
    }


def main():
    today = datetime.now(JST).date().strftime('%Y-%m-%d')
    print("=" * 64)
    print("  Stocksurfing 新高値(52週/取得内ATH)戻り率バックテスト (計測専用)")
    print(f"  実行: {datetime.now(JST).isoformat()}  取得: {NEWHIGH_FETCH_FROM}〜{today}")
    print("=" * 64)

    universe = (
        [('個別', s['code'], s['name']) for s in DEFAULT_STOCKS]
        + [('ETF', e['code'], e['name']) for e in ETF_UNIVERSE]
        + [('ベースライン', b['code'], b['name']) for b in BASELINE_UNIVERSE]
    )

    all_events = []
    data_status = []
    for group, code, name in universe:
        days = fetch_days(code, frm=NEWHIGH_FETCH_FROM, to=today)
        time.sleep(SLEEP_BETWEEN_CALLS)
        usable = [d for d in days if d['c'] is not None]
        first_date = usable[0]['date'] if usable else None
        if len(usable) < (MIN_TRAIL + 30):
            data_status.append({'group': group, 'code': code, 'name': name,
                                'days': len(usable), 'earliest': first_date, 'status': 'データ不足/なし'})
            print(f"  [{group}] {code} {name}: データ不足({len(usable)}日) → スキップ")
            continue
        evs = scan_newhigh(days, group)
        all_events.extend(evs)
        n52 = sum(1 for e in evs if e['type'] == '52w_breakout')
        data_status.append({'group': group, 'code': code, 'name': name, 'days': len(usable),
                            'earliest': first_date, 'events_all': sum(1 for e in evs if e['type'] == 'all_days'),
                            'events_52w_breakout': n52, 'status': 'OK'})
        print(f"  [{group}] {code} {name}: {len(usable)}日(最古 {first_date}) → 52wブレイク{n52}件")

    def pool(types, groups):
        rows = [e for e in all_events if e['type'] in types and e['group'] in groups]
        return cell(rows) if rows else {'n': 0}

    ind_etf = ['個別', 'ETF']
    pooled = {
        '52w_breakout(個別+ETF)': pool(['52w_breakout'], ind_etf),
        '52w_near3(個別+ETF)': pool(['52w_near3'], ind_etf),
        'ath_breakout(個別+ETF)': pool(['ath_breakout'], ind_etf),
        'all_days(個別+ETF)': pool(['all_days'], ind_etf),
        '52w_breakout(個別)': pool(['52w_breakout'], ['個別']),
        '52w_breakout(ETF)': pool(['52w_breakout'], ['ETF']),
        'all_days(TOPIX)': pool(['all_days'], ['ベースライン']),
    }

    # 近接バンド × 成績(個別+ETF、all_days基準で「高値に近いほど良いか」)
    band_tab = []
    for bl in ['新高値引け(<=0%)', '[0,1)', '[1,3)', '[3,5)', '[5,∞)']:
        rows = [e for e in all_events if e['type'] == 'all_days' and e['group'] in ind_etf and e['band'] == bl]
        if rows:
            band_tab.append({'band': bl, **cell(rows)})

    # 地平線感度(個別+ETF、52wブレイク vs all_days の positive%)
    def posrate(types, groups, M):
        rows = [e for e in all_events if e['type'] in types and e['group'] in groups]
        return _rate([(1 if (r[f'fwd{M}'] > 0) else 0) if r[f'fwd{M}'] is not None else None for r in rows])
    horizon_sens = {
        '52w_breakout': {f'positive_M{M}': posrate(['52w_breakout'], ind_etf, M) for M in M_LIST},
        'all_days': {f'positive_M{M}': posrate(['all_days'], ind_etf, M) for M in M_LIST},
    }

    # ---- 所見(平易な結論) ----
    bo = pooled['52w_breakout(個別+ETF)']
    ad = pooled['all_days(個別+ETF)']
    tx = pooled['all_days(TOPIX)']
    near = pooled['52w_near3(個別+ETF)']
    ath = pooled['ath_breakout(個別+ETF)']
    verdict = []

    def d(a, b):
        return round(a - b, 1) if (a is not None and b is not None) else None

    if bo.get('n') and ad.get('n'):
        verdict.append(
            f"(1) 新高値ブレイクの優位性(20営業日): ブレイク positive={bo['positive_pct']}% / fwd平均={bo['fwd_mean']}% "
            f"(n={bo['n']}) vs 全日 positive={ad['positive_pct']}% / fwd平均={ad['fwd_mean']}% "
            f"→ positive差={d(bo['positive_pct'], ad['positive_pct'])}pt, fwd差={d(bo['fwd_mean'], ad['fwd_mean'])}pt"
        )
    else:
        verdict.append("(1) 新高値ブレイク: サンプル不足で判定不能")

    if tx.get('n'):
        verdict.append(
            f"(2) 市場ベースライン比: TOPIX(全日) positive={tx['positive_pct']}% / fwd平均={tx['fwd_mean']}% "
            f"→ 新高値ブレイクの対TOPIX超過 fwd={d(bo.get('fwd_mean'), tx.get('fwd_mean'))}pt"
        )

    if bo.get('rulebook_2_5', {}).get('winPct_decided') is not None:
        rb = bo['rulebook_2_5']; rba = ad.get('rulebook_2_5', {})
        verdict.append(
            f"(3) 松村式ルール(損切-2%/利確+5%)の決着勝率: 新高値ブレイク={rb['winPct_decided']}% "
            f"(勝{rb['win']}/負{rb['lose']}/未決着{rb['even']}) vs 全日={rba.get('winPct_decided')}% "
            f"→ {'ブレイクが有利' if (rb['winPct_decided'] or 0) > (rba.get('winPct_decided') or 0) else '全日と大差なし/劣位'}"
        )

    if near.get('n'):
        verdict.append(
            f"(4) 新高値圏[0-3%以内]近接: positive={near['positive_pct']}% / fwd平均={near['fwd_mean']}% "
            f"/ ルール勝率={near.get('rulebook_2_5', {}).get('winPct_decided')}% (n={near['n']}) "
            f"→ ブレイク瞬間でなく“高値近接”でも取れるか"
        )

    if band_tab:
        seq = " / ".join(f"{b['band']}:pos{b['positive_pct']}%(fwd{b['fwd_mean']},n{b['n']})" for b in band_tab)
        verdict.append(f"(5) 高値からの距離×成績(全日): {seq} → 高値に近い帯ほど良ければ“新高値目線”は有効")

    if ath.get('n'):
        verdict.append(
            f"(6) 取得内ATH(≈上場来)ブレイク: positive={ath['positive_pct']}% / fwd平均={ath['fwd_mean']}% "
            f"/ ルール勝率={ath.get('rulebook_2_5', {}).get('winPct_decided')}% (n={ath['n']})"
        )

    edge_ok = (bo.get('fwd_mean') is not None and ad.get('fwd_mean') is not None
               and (bo['fwd_mean'] - ad['fwd_mean']) >= 0.5
               and (bo.get('positive_pct') or 0) >= (ad.get('positive_pct') or 0))
    verdict.append(
        f"(7) 実装可否(暫定): {'新高値目線にエッジの芽あり→観測モードで前向き検証に進める価値あり' if edge_ok else 'この標本ではエッジ薄〜不明。別期間(OOS)確認と観測での実測が必須'}。"
        f" 本標本は上昇相場に偏り・コスト未考慮のため楽観バイアス前提で解釈。"
    )

    report = {
        'generatedAt': datetime.now(JST).isoformat(),
        'note': '計測専用・本番非変更。価格は調整済み(AdjC)、権利落ち日除外。取引コスト/スリッページ未考慮。',
        'hypothesis': '新高値(52週/取得内ATH)または新高値圏での仕掛けに20営業日の戻りエッジがあるか',
        'params': {'fetchFrom': NEWHIGH_FETCH_FROM, 'fetchTo': today, 'W52': W52,
                   'M_list': M_LIST, 'M_primary': M_PRIMARY, 'stopHorizon': STOP_HORIZON,
                   'takeProfitMain': TP_MAIN, 'takeProfitAlt': TP_ALT, 'stopLossPct': SL_PCT},
        'universeStatus': data_status,
        'totalEvents_all_days': sum(1 for e in all_events if e['type'] == 'all_days'),
        'pooled': pooled,
        'proximityBands_allDays': band_tab,
        'horizonSensitivity_positive': horizon_sens,
        'verdict': verdict,
    }
    with open(os.path.join(SCRIPT_DIR, 'newhigh_backtest_report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)

    print("\n" + "=" * 64)
    print("[プール(20営業日・個別+ETF)]")
    for k, v in pooled.items():
        if v.get('n'):
            print(f"  {k:<26} n={v['n']:>5} pos={v['positive_pct']} fwd平均={v['fwd_mean']} "
                  f"ルール(-2/+5)勝率={v['rulebook_2_5']['winPct_decided']}")
    print("\n  所見(計測者):")
    for line in verdict:
        print("  " + line)
    print("\n  ✓ newhigh_backtest_report.json 書き出し完了")


if __name__ == '__main__':
    main()
