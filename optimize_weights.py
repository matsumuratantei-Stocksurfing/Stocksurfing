#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
松村式Stocksurfing - 自己学習エンジン (weight optimizer) v1.0

verification_log.json に蓄積された「朝の各指標 chgPct(morningIndicators)」と
「当日の日経実勢(actualIndices.N225.chgPct)」のペアから、12指標の重みを最適化する。

【設計思想】奥様の判断の信頼性を最優先するため、賢いだけでなく "慎重" にする:
  1. 過学習を避け、汎化性能(LOOCV: 1日抜き交差検証)で評価する。
  2. 現行(baseline)の重みを汎化性能で MARGIN 以上明確に上回り、かつ
     十分なデータ数(MIN_DAYS)が溜まったときに限り weights.json を自動更新(promoted)。
  3. 上回らない/データ不足なら baseline を維持し、提案レポートだけ残す(誰も傷つかない)。
  4. 学習後の重みも現行へ正則化(ブレンド)し、極端な値にならないようクランプする。

依存ライブラリなし(標準ライブラリのみ)。GitHub Actions から週1で呼ぶ想定。
"""
import json
import os
import math
import datetime

JST = datetime.timezone(datetime.timedelta(hours=9))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(SCRIPT_DIR, 'verification_log.json')
WEIGHTS_PATH = os.path.join(SCRIPT_DIR, 'weights.json')
REPORT_PATH = os.path.join(SCRIPT_DIR, 'optimization_report.json')

KEYS = ['N225F', 'TOPX', 'NDX', 'SPX', 'SOX', 'DJI',
        'USDJPY', 'EURJPY', 'TNX', 'WTI', 'VIX', 'NKVI']
INVERSE = {'TNX': True, 'VIX': True, 'NKVI': True}
DEFAULT_WEIGHTS = {'N225F': 3.0, 'TOPX': 2.5, 'NDX': 2.0, 'SPX': 1.8, 'SOX': 2.0,
                   'DJI': 1.5, 'USDJPY': 1.5, 'EURJPY': 0.8, 'TNX': 0.8,
                   'WTI': 0.6, 'VIX': 0.8, 'NKVI': 1.0}

# --- 自動昇格ゲート(ここを緩めると危険。慎重に) ---
MIN_DAYS = 40          # これ未満のデータ数では絶対に自動更新しない
MARGIN = 0.05          # 汎化性能で baseline を +5pt 以上上回ること
BLEND = 0.5            # 最適化結果と現行重みのブレンド比(0.5=半々で正則化)
WEIGHT_GRID = [0.0, 0.3, 0.6, 0.8, 1.0, 1.3, 1.6, 2.0, 2.5, 3.0]


def _num(x):
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(xf) or math.isinf(xf)) else xf


def load_dataset():
    """[(date, {key:chgPct}, actual_n225_chgPct), ...] (平日・両データ有り) を返す。"""
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH, 'r', encoding='utf-8') as f:
        log = json.load(f)
    ds = []
    for l in log:
        date = l.get('date')
        mi = l.get('morningIndicators')
        n225 = (l.get('actualIndices') or {}).get('N225') or {}
        act = _num(n225.get('chgPct'))
        if not date or not mi or act is None:
            continue
        try:
            if datetime.date.fromisoformat(date).weekday() >= 5:
                continue
        except Exception:
            continue
        inds = {k: _num(mi.get(k)) for k in KEYS}
        if all(v is None for v in inds.values()):
            continue
        ds.append((date, inds, act))
    ds.sort(key=lambda x: x[0])
    return ds


def score(inds, w):
    s = tw = 0.0
    for k in KEYS:
        v = inds.get(k)
        if v is None:
            continue
        d = -1 if INVERSE.get(k) else 1
        cp = max(-5.0, min(5.0, v))
        s += cp * d * w[k]
        tw += w[k]
    return None if tw == 0 else s / tw


def hit_rate(data, w):
    ok = tot = 0
    for _, inds, act in data:
        sc = score(inds, w)
        if sc is None:
            continue
        tot += 1
        if (sc > 0) == (act > 0):
            ok += 1
    return (ok / tot, tot) if tot else (0.0, 0)


def optimize(train):
    """座標上昇法で in-sample 方向一致率を最大化する重みを探す。"""
    w = dict(DEFAULT_WEIGHTS)
    for _ in range(6):
        improved = False
        for k in KEYS:
            best, bh = w[k], hit_rate(train, w)[0]
            for g in WEIGHT_GRID:
                w2 = dict(w)
                w2[k] = g
                hh = hit_rate(train, w2)[0]
                if hh > bh:
                    bh, best = hh, g
            if best != w[k]:
                w[k] = best
                improved = True
        if not improved:
            break
    return w


def loocv(data):
    """1日抜き交差検証での最適化重みの汎化方向一致率。"""
    if len(data) < 5:
        return 0.0, 0
    ok = tot = 0
    for i in range(len(data)):
        train = data[:i] + data[i + 1:]
        w = optimize(train)
        _, inds, act = data[i]
        sc = score(inds, w)
        if sc is None:
            continue
        tot += 1
        if (sc > 0) == (act > 0):
            ok += 1
    return (ok / tot, tot) if tot else (0.0, 0)


def blend_and_clamp(w_opt):
    out = {}
    for k in KEYS:
        v = BLEND * w_opt[k] + (1 - BLEND) * DEFAULT_WEIGHTS[k]
        lo, hi = 0.0, DEFAULT_WEIGHTS[k] * 2.0  # 既定の2倍を上限に
        out[k] = round(max(lo, min(hi, v)), 3)
    return out


def current_weights():
    if os.path.exists(WEIGHTS_PATH):
        try:
            with open(WEIGHTS_PATH, 'r', encoding='utf-8') as f:
                wj = json.load(f)
            w = dict(DEFAULT_WEIGHTS)
            for k in KEYS:
                if k in wj.get('weights', {}):
                    v = _num(wj['weights'][k])
                    if v is not None:
                        w[k] = v
            return w, wj.get('version', 'unknown')
        except Exception:
            pass
    return dict(DEFAULT_WEIGHTS), 'default'


def main():
    print("=" * 56)
    print("  松村式Stocksurfing - 自己学習エンジン (weight optimizer)")
    print(f"  実行: {datetime.datetime.now(JST).isoformat()}")
    print("=" * 56)

    ds = load_dataset()
    n = len(ds)
    base_w, base_ver = current_weights()
    base_oos, _ = loocv(ds) if n >= 5 else (0.0, 0)
    base_is, _ = hit_rate(ds, base_w)

    print(f"\n学習データ(平日・朝指標×実勢): {n}日")
    print(f"現行重み({base_ver}) in-sample方向一致: {base_is*100:.1f}%")

    candidate = None
    cand_oos = 0.0
    cand_is = 0.0
    if n >= 5:
        w_opt_raw = optimize(ds)
        candidate = blend_and_clamp(w_opt_raw)
        cand_is, _ = hit_rate(ds, candidate)
        cand_oos, _ = loocv(ds)
        print(f"最適化(LOOCV汎化)方向一致: {cand_oos*100:.1f}%  / in-sample: {cand_is*100:.1f}%")
        print(f"現行(LOOCV汎化)方向一致 : {base_oos*100:.1f}%")

    # --- 自動昇格の判定 ---
    promote = (
        candidate is not None
        and n >= MIN_DAYS
        and cand_oos >= base_oos + MARGIN
    )

    if n < MIN_DAYS:
        reason = f"データ不足 (あと{MIN_DAYS - n}日でゲート判定開始)"
    elif candidate is None:
        reason = "候補生成不可"
    elif not promote:
        reason = f"汎化性能が現行を+{MARGIN*100:.0f}pt以上上回らない (据え置きが安全)"
    else:
        reason = "汎化性能で現行を明確に上回ったため自動昇格"

    report = {
        'evaluatedAt': datetime.datetime.now(JST).isoformat(),
        'days': n,
        'minDays': MIN_DAYS,
        'margin': MARGIN,
        'baselineVersion': base_ver,
        'baselineInSample': round(base_is, 4),
        'baselineOos': round(base_oos, 4),
        'candidateInSample': round(cand_is, 4),
        'candidateOos': round(cand_oos, 4),
        'candidateWeights': candidate,
        'promoted': promote,
        'reason': reason,
    }
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)

    print(f"\n判定: {'★昇格(weights.json更新)' if promote else '据え置き'} — {reason}")

    if promote:
        wj = {
            'version': f"learned-{datetime.datetime.now(JST).date().isoformat()}",
            'mode': 'promoted',
            'updatedAt': datetime.datetime.now(JST).isoformat(),
            'basedOnDays': n,
            'oosHitRate': round(cand_oos, 4),
            'baselineOosHitRate': round(base_oos, 4),
            'weights': candidate,
            'note': '自己学習エンジンが汎化性能で現行を上回ったため自動採用。',
        }
        with open(WEIGHTS_PATH, 'w', encoding='utf-8') as f:
            json.dump(wj, f, ensure_ascii=False, indent=2, allow_nan=False)
        print(f"  → weights.json を {wj['version']} に更新しました。")
    else:
        print("  → weights.json は変更しません。提案は optimization_report.json に保存。")


if __name__ == '__main__':
    main()
