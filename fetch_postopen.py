"""寄り後値取得スクリプト (v3.4.5, 2026-07-18)

GitHub Actions から 9:10 / 9:20 / 9:30 JST に呼び出される。
日経平均 / 東京エレクトロン / ドル円の寄り値・現在値・前日終値を yfinance
から取得し、postopen.json に書き出す。

クライアント側 (index.html) は postopen.json を読むだけになるため、
CORS プロキシに依存しない堅牢な寄り後判定が実現できる。

v3.4.5: 前日終値の取り方を「日足の後ろから2本目」から
「当日より前の最後の日足」に変更。yfinance の日足反映が遅れている時に
2営業日前の終値を前日終値として掴んでしまう問題(前日比%のズレ)の対策。
"""

import json
import sys
from datetime import datetime, timezone, timedelta

import yfinance as yf


JST = timezone(timedelta(hours=9))


# index.html の referenceData キーと対応させる
SYMBOLS = {
    'TSE_OPEN':    {'symbol': '^N225',    'name': '日経平均'},
    'SOX_PROXY':   {'symbol': '8035.T',   'name': '東京エレクトロン'},
    'USDJPY_OPEN': {'symbol': 'USDJPY=X', 'name': 'ドル円'},
}


def fetch_open_price(symbol: str) -> dict | None:
    """寄り値 (当日 1m バーの最初の Open)、現在値、前日終値、前日比% を返す。"""
    try:
        ticker = yf.Ticker(symbol)

        # 当日 1m バー → 寄り値と現在値
        intraday = ticker.history(period='1d', interval='1m')
        if intraday is None or intraday.empty:
            print(f"[WARN] {symbol}: intraday history empty", file=sys.stderr)
            return None

        open_price = float(intraday['Open'].iloc[0])
        current_price = float(intraday['Close'].iloc[-1])

        # 前日終値 (v3.4.5: 「当日より前の最後の日足」を日付で選ぶ。
        # iloc[-2] 固定だと日足反映の遅延時に2営業日前を掴むため)
        prev_close = None
        try:
            daily = ticker.history(period='10d', interval='1d')
            if daily is not None and not daily.empty:
                today_jst = datetime.now(JST).date()
                mask = [d.date() < today_jst for d in daily.index]
                prior = daily[mask]
                if not prior.empty:
                    prev_close = float(prior['Close'].iloc[-1])
        except Exception as e:
            print(f"[WARN] {symbol}: prev_close fetch failed: {e}", file=sys.stderr)

        chg_pct = None
        if prev_close and prev_close != 0:
            chg_pct = (current_price - prev_close) / prev_close * 100

        return {
            'price': round(current_price, 4),
            'open': round(open_price, 4),
            'prev_close': round(prev_close, 4) if prev_close is not None else None,
            'chgPct': round(chg_pct, 4) if chg_pct is not None else None,
        }
    except Exception as e:
        print(f"[ERROR] {symbol}: {e}", file=sys.stderr)
        return None


def main() -> int:
    now = datetime.now(JST)
    out = {
        'updated_at': now.isoformat(),
        'updated_jst': now.strftime('%Y-%m-%d %H:%M:%S JST'),
        'data': {},
    }

    success = 0
    for key, info in SYMBOLS.items():
        result = fetch_open_price(info['symbol'])
        out['data'][key] = {
            'name': info['name'],
            'symbol': info['symbol'],
            'value': result,
        }
        status = 'OK' if result else 'FAIL'
        print(f"  {key:13s} {info['symbol']:10s} {status}: {result}")
        if result is not None:
            success += 1

    out['summary'] = {
        'success': success,
        'total': len(SYMBOLS),
    }

    with open('postopen.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n✅ postopen.json written (success {success}/{len(SYMBOLS)})")
    # 1つも取れていなければ失敗扱い (リトライさせる)
    return 0 if success > 0 else 1


if __name__ == '__main__':
    sys.exit(main())
