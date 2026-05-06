#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
松村式Stocksurfing - 通知メール送信 (v3.1)
v3で追加: 決算警告セクション・昨日の振り返り
"""
import os
import sys
import json
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

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
NAME_MAP = {'N225F':'日経先物','TOPX':'TOPIX','NDX':'ナスダック100','SPX':'S&P500',
            'SOX':'SOX半導体','DJI':'NYダウ','USDJPY':'ドル円','EURJPY':'ユーロ円',
            'TNX':'米10年金利','WTI':'WTI原油','VIX':'VIX恐怖指数','NKVI':'日経VI'}

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
    if combined >= 3: return 3
    if combined >= -8: return 2
    return 1

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
<p style="font-size:11px;color:#999;margin-top:24px;border-top:1px solid #ddd;padding-top:12px">v3.1 答え合わせエンジン搭載 / データ提供: yfinance / Nikkei公式 / J-Quants Premium</p>
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
        subject, body = build_postopen_email(data, jst_now)
    else:
        subject, body = build_morning_email(data, jst_now)
    print(f"[INFO] 送信中: {subject}")
    print(f"[INFO] 宛先: {recipients}")
    send_mail(subject, body, recipients, gmail_user, gmail_password)
    print(f"[OK] 送信完了")

if __name__ == '__main__':
    main()
