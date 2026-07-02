#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
松村式Stocksurfing - 通知メール送信 (v3.4)
決算警告セクション・昨日の振り返りを含む HTML メール。
銘柄・指標・スコア計算は common.py を参照(重複排除)。
"""
import os
import sys
import json
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (
    DEFAULT_STOCKS, INDICATORS, NAME_MAP,
    calc_score, stock_score, stars_for, WEIGHTS_VERSION,
)

JST = timezone(timedelta(hours=9))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def calc_gap(indicators, reference):
    cme = indicators.get('N225F')
    cash = reference.get('N225_CASH')
    if not cme or not cash: return None
    if cme.get('price') is None or cash.get('price') is None: return None
    gap = cme['price'] - cash['price']
    return {'gap': gap, 'gapPct': gap / cash['price'] * 100}

def detect_twist(indicators):
    pos, neg = 0, 0
    for ind in INDICATORS:
        v = indicators.get(ind['key'])
        if not v or v.get('chgPct') is None: continue
        if abs(v['chgPct']) < 0.1: continue
        d = -1 if ind['inverse'] else 1
        if v['chgPct'] * d > 0: pos += 1
        else: neg += 1
    total = pos + neg
    if total < 4: return None
    dissent = neg if pos >= neg else pos
    return dissent if dissent >= 3 else None

def verdict_text(s):
    if s is None: return '不明'
    if s >= 40: return '🔥 強い追い風（積極的に仕掛け）'
    if s >= 20: return '📈 追い風（通常サイズで）'
    if s > -20: return '⚖️ 中立（様子見推奨）'
    if s > -40: return '📉 向かい風（控えめに）'
    return '🌀 強い逆風（本日は見送り）'

def get_yesterdays_recap(jst_now):
    """verification_log.json から昨日の振り返りを取得"""
    log_path = os.path.join(SCRIPT_DIR, 'verification_log.json')
    if not os.path.exists(log_path):
        return None
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            log = json.load(f)
    except Exception:
        return None
    if not log: return None
    # 最新の1件 (今朝より前のもの)
    today_str = jst_now.date().strftime('%Y-%m-%d')
    prior = [l for l in log if l.get('date') and l['date'] < today_str]
    if not prior:
        return None
    last = prior[-1]
    return last

def build_recap_html(recap):
    """昨日の振り返りHTMLブロック"""
    if not recap: return ''
    score = recap.get('morningScore')
    n225 = recap.get('actualIndices', {}).get('N225') or {}
    metrics = recap.get('metrics', {})
    direction_correct = metrics.get('n225_direction_correct')
    avg_pick = metrics.get('avg_pick_return')

    n225_chg = n225.get('chgPct')
    direction_icon = '✓' if direction_correct else '✗' if direction_correct is False else '—'
    parts = [f"昨日({recap.get('date','?')}) の予測: <b>{'+' if score and score > 0 else ''}{score:.0f}</b>"] if score is not None else []
    if n225_chg is not None:
        parts.append(f"日経実勢: <b>{n225_chg:+.2f}%</b>")
    parts.append(f"方向性: {direction_icon}")
    if avg_pick is not None:
        parts.append(f"★4候補平均: <b>{avg_pick:+.2f}%</b>")

    return f'<div style="background:#1a2a4d;color:#e6ecff;padding:10px;border-radius:8px;font-size:12px;margin:12px 0">📒 {" / ".join(parts)}</div>'

def build_earnings_warnings_html(earnings_warnings):
    """決算警告HTMLブロック"""
    if not earnings_warnings: return ''
    items = []
    for code, info in sorted(earnings_warnings.items(), key=lambda x: x[1].get('businessDaysUntil', 99)):
        st = next((s for s in DEFAULT_STOCKS if s['code'] == code), None)
        name = st['name'] if st else info.get('companyName', '?')
        bd = info.get('businessDaysUntil')
        date = info.get('date', '?')
        bd_s = '本日' if bd == 0 else f'{bd}営業日後'
        items.append(f'<li><b>{code} {name}</b> — 決算予定 {date} ({bd_s})</li>')
    return f'''
<div style="background:#3a2410;border:1px solid #f7b955;border-radius:8px;padding:12px;margin:12px 0">
  <div style="color:#f7b955;font-weight:bold;margin-bottom:6px">⚠️ 決算発表が3営業日以内の銘柄（仕掛け非推奨）</div>
  <ul style="margin:4px 0 0 16px;padding:0;color:#e6ecff;font-size:13px">{"".join(items)}</ul>
</div>'''

def build_morning_email(data, jst_now):
    indicators = data.get('indicators', {})
    reference = data.get('referenceData', {})
    earnings_warnings = data.get('earningsWarnings', {})
    score = calc_score(indicators)
    verdict = verdict_text(score)
    gap = calc_gap(indicators, reference)
    twist = detect_twist(indicators)

    picks = []
    for stock in DEFAULT_STOCKS:
        if stock['code'] in earnings_warnings: continue  # 決算警告対象は候補から外す
        star = stars_for(stock, score, indicators)
        ss = stock_score(stock, indicators)
        if star >= 4:
            picks.append({'stock': stock, 'star': star, 'ss': ss})
    picks.sort(key=lambda x: (-x['star'], -(x['ss'] or -99)))
    picks = picks[:5]

    score_str = f"{'+' if score and score > 0 else ''}{score:.0f}" if score is not None else "—"
    subject = f"📈 [{jst_now.strftime('%m/%d')}] 場の判定 {score_str} / {verdict.split('（')[0]}"

    gap_html = ''
    if gap:
        sign = '+' if gap['gap'] >= 0 else ''
        color = '#ff5766' if gap['gap'] >= 0 else '#2ecc71'
        gap_html = f'<p>🪟 <b>本日の窓開け予想</b>: <span style="color:{color};font-weight:bold">{sign}{gap["gap"]:.0f}円 ({sign}{gap["gapPct"]:.2f}%)</span></p>'

    twist_html = ''
    if twist:
        twist_html = f'<p style="background:#3a2410;padding:8px;border-radius:6px;color:#f7b955">⚠️ <b>指標がねじれています</b>（不一致{twist}件）。サイズを半分にするのが安全です。</p>'

    earnings_html = build_earnings_warnings_html(earnings_warnings)
    recap = get_yesterdays_recap(jst_now)
    recap_html = build_recap_html(recap)

    picks_html = ''
    if picks:
        picks_html = '<h3>🎯 仕掛け候補（★4以上、決算3営業日以内除外）</h3><ul style="list-style:none;padding-left:0">'
        for p in picks:
            ss_s = f' (連動 {"+" if p["ss"] and p["ss"]>=0 else ""}{p["ss"]:.2f}%)' if p['ss'] is not None else ''
            picks_html += f'<li style="padding:6px;margin:4px 0;background:#1a2540;color:#e6ecff;border-radius:6px">{"★"*p["star"]} <b>{p["stock"]["code"]} {p["stock"]["name"]}</b>{ss_s}</li>'
        picks_html += '</ul>'
    else:
        picks_html = '<p style="color:#9aa8c7">★4以上の候補なし。様子見推奨。</p>'

    indicators_table = '<h3>📊 指標一覧</h3><table style="border-collapse:collapse;width:100%"><tr style="background:#263353;color:#fff"><th style="padding:6px;text-align:left">指標</th><th style="padding:6px;text-align:right">前日終値</th><th style="padding:6px;text-align:right">前日比%</th></tr>'
    for ind in INDICATORS:
        v = indicators.get(ind['key'])
        if not v:
            indicators_table += f'<tr><td style="padding:6px;border-bottom:1px solid #ddd">{NAME_MAP.get(ind["key"],ind["key"])}</td><td style="padding:6px;text-align:right;color:#999">—</td><td style="padding:6px;text-align:right;color:#999">—</td></tr>'
            continue
        chg = v.get('chgPct')
        chg_s = f"{'+' if chg and chg >= 0 else ''}{chg:.2f}%" if chg is not None else '—'
        chg_color = '#ff5766' if chg and chg >= 0 else '#2ecc71'
        if ind['inverse']:
            chg_color = '#2ecc71' if chg and chg >= 0 else '#ff5766'
        indicators_table += f'<tr><td style="padding:6px;border-bottom:1px solid #ddd">{NAME_MAP.get(ind["key"],ind["key"])}</td><td style="padding:6px;text-align:right;border-bottom:1px solid #ddd">{v.get("price","—")}</td><td style="padding:6px;text-align:right;border-bottom:1px solid #ddd;color:{chg_color}">{chg_s}</td></tr>'
    indicators_table += '</table>'

    pages_url = os.environ.get('PAGES_URL', 'https://matsumuratantei-stocksurfing.github.io/Stocksurfing/')
    body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans','Yu Gothic',sans-serif;max-width:600px;margin:0 auto;padding:16px;color:#333">
<h1 style="color:#0b1220;border-bottom:3px solid #4da3ff;padding-bottom:8px">🏄 松村式Stocksurfing</h1>
<p style="color:#666">朝の場の判定 / {jst_now.strftime('%Y年%m月%d日 %H:%M')}</p>
{recap_html}
<div style="background:#0b1220;color:#fff;padding:20px;border-radius:12px;text-align:center;margin:16px 0">
  <div style="font-size:48px;font-weight:bold">{score_str}</div>
  <div style="font-size:18px;margin-top:8px">{verdict}</div>
</div>
{gap_html}
{twist_html}
{earnings_html}
{picks_html}
{indicators_table}
<p style="margin-top:24px;text-align:center"><a href="{pages_url}" style="background:#4da3ff;color:#fff;padding:12px 24px;text-decoration:none;border-radius:8px;font-weight:bold">📱 アプリで詳細を見る</a></p>
<p style="font-size:11px;color:#999;margin-top:24px;border-top:1px solid #ddd;padding-top:12px">v3.4 答え合わせエンジン搭載 / データ提供: yfinance / Nikkei公式 / J-Quants V2 Premium</p>
</body></html>"""
    return subject, body

def build_postopen_email(data, jst_now):
    indicators = data.get('indicators', {})
    score = calc_score(indicators)
    verdict = verdict_text(score)
    score_str = f"{'+' if score and score > 0 else ''}{score:.0f}" if score is not None else "—"
    subject = f"⏰ [{jst_now.strftime('%m/%d')}] 寄り後判定 {score_str} / {verdict.split('（')[0]}"
    pages_url = os.environ.get('PAGES_URL', 'https://matsumuratantei-stocksurfing.github.io/Stocksurfing/')
    body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans','Yu Gothic',sans-serif;max-width:600px;margin:0 auto;padding:16px;color:#333">
<h1 style="color:#0b1220">⏰ 寄り後9:20判定</h1>
<p style="color:#666">{jst_now.strftime('%Y年%m月%d日 %H:%M')}</p>
<div style="background:#0b1220;color:#fff;padding:20px;border-radius:12px;text-align:center;margin:16px 0">
  <div style="font-size:42px;font-weight:bold">{score_str}</div>
  <div style="font-size:16px;margin-top:8px">{verdict}</div>
</div>
<p style="text-align:center;margin-top:24px">
  <a href="{pages_url}" style="background:#4da3ff;color:#fff;padding:14px 28px;text-decoration:none;border-radius:8px;font-weight:bold;font-size:16px">📱 寄り後の最終判定をアプリで見る</a>
</p>
<p style="margin-top:16px;font-size:13px;color:#666">寄り値（日経・SOX代理・ドル円）に基づく最終判定はアプリ画面で確認してください。</p>
</body></html>"""
    return subject, body

def postopen_send_ok(jst_now):
    """寄り後メールを送ってよいかの安全ガード。False=送らない(スキップ)。

    (1) JST 9:05 未満は寄り値未確定 → 送らない。
    (2) postopen.json が無い/当日更新でない → 送らない。
    (3) 日経の寄り値(open)が欠損、または前日終値と完全一致(寄り未確定の疑い) → 送らない。
    寄り値が確定している時のみ True を返し、その時だけ「寄り後判定」を名乗る。
    """
    # (1) 時刻ガード
    if (jst_now.hour, jst_now.minute) < (9, 5):
        print(f"[SKIP] 寄り前(9:05 JST未満 / 現在 {jst_now.strftime('%H:%M')})のため寄り後メールを送信しません")
        return False

    # (2) postopen.json 存在・当日更新チェック
    pj_path = os.path.join(SCRIPT_DIR, 'postopen.json')
    if not os.path.exists(pj_path):
        print("[SKIP] postopen.json が無い(寄り値未取得)ため送信しません")
        return False
    try:
        with open(pj_path, 'r', encoding='utf-8') as f:
            pj = json.load(f)
    except Exception:
        print("[SKIP] postopen.json 読込失敗のため送信しません")
        return False
    try:
        upd_date = datetime.fromisoformat(pj.get('updated_at', '')).astimezone(JST).date()
    except Exception:
        upd_date = None
    if upd_date != jst_now.date():
        print(f"[SKIP] postopen.json が当日更新でない(更新日 {upd_date})ため送信しません")
        return False

    # (3) 日経の寄り値確定チェック
    nk = ((pj.get('data') or {}).get('TSE_OPEN') or {}).get('value')
    if not nk or nk.get('open') is None:
        print("[SKIP] 日経の寄り値(open)が未取得/欠損(寄り未確定)のため送信しません")
        return False
    op, pc = nk.get('open'), nk.get('prev_close')
    if pc is not None and op == pc:
        print("[SKIP] 日経の寄り値が前日終値と完全一致(寄り未確定の疑い)のため送信しません")
        return False

    print(f"[OK] 寄り値確定を確認({jst_now.strftime('%H:%M')} JST / 日経寄り {op})。寄り後メールを送信します")
    return True


def send_mail(subject, html_body, recipients, gmail_user, gmail_password):
    msg = MIMEMultipart('alternative')
    msg['From'] = gmail_user
    msg['To'] = ', '.join(recipients)
    msg['Subject'] = subject
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=context) as server:
        server.login(gmail_user, gmail_password)
        server.send_message(msg)

def main():
    gmail_user = os.environ.get('GMAIL_USER')
    gmail_password = os.environ.get('GMAIL_APP_PASSWORD')
    recipients_str = os.environ.get('RECIPIENTS', gmail_user or '')
    notification_type = os.environ.get('NOTIFICATION_TYPE', 'morning')
    if not gmail_user or not gmail_password:
        print("[ERROR] GMAIL_USER / GMAIL_APP_PASSWORD が未設定")
        sys.exit(1)
    recipients = [r.strip() for r in recipients_str.split(',') if r.strip()]
    if not recipients:
        print("[ERROR] 宛先が未設定")
        sys.exit(1)
    data_path = os.path.join(SCRIPT_DIR, 'data.json')
    if not os.path.exists(data_path):
        print(f"[ERROR] data.json が見つかりません")
        sys.exit(1)
    with open(data_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    jst_now = datetime.now(JST)
    if notification_type == 'postopen':
        # 二重の安全ガード: 寄り前 or 寄り値未確定なら「寄り後判定」を送らない。
        # cron/判定が万一ズレても、寄り付き前に寄り後メールが飛ぶことを構造的に防ぐ。
        if not postopen_send_ok(jst_now):
            print("[OK] 寄り後メールはスキップしました(誤配信防止ガード)")
            sys.exit(0)
        subject, body = build_postopen_email(data, jst_now)
    else:
        subject, body = build_morning_email(data, jst_now)
    print(f"[INFO] 送信中: {subject}")
    print(f"[INFO] 宛先: {recipients}")
    send_mail(subject, body, recipients, gmail_user, gmail_password)
    print(f"[OK] 送信完了")

if __name__ == '__main__':
    main()
