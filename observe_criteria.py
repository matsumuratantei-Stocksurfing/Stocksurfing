#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
observe_criteria.py — 格上げ基準 PROMOTION_CRITERIA_V1 の単一ソース + 判定

観測モードで「PO押し目エントリーにエッジがあるか」を前向き検証し、
事前固定した基準を全て満たしたときだけ“格上げ判定可能”と表示する
(格上げ=シグナル化の実行は自動では行わない。人間が相談して決める)。

★ PROMOTION_CRITERIA_V1 の数値は蓄積開始前に確定済み。開始後は変更しない。
  基準を変えたい場合は V2 として新設し、logic_version リセット規約と同様に
  「新基準は新バージョンの集計にのみ適用」する。

集計は logic_version でフィルタした「現行版のクローズ済みレコードのみ」を対象とする
(時計リセット規約)。
"""

PROMOTION_CRITERIA_VERSION = 'V1'

# 蓄積開始前に確定した固定基準(松村さん確定・開始後変更不可)
PROMOTION_CRITERIA_V1 = {
    'min_observation_days': 90,   # 1) 観測期間 >= 90暦日
    'min_closed': 30,             # 2) クローズ済み件数 >= 30
    'min_expectancy': 0.0,        # 3) 期待値 > 0 (クローズ済み全件)
    'baseline_excess_gt': 0.0,    # 4) baseline比超過リターン > 0 (dumb / TOPIX の両方)
    'min_r_multiple': 1.5,        # 5) R倍数(平均利益÷平均損失) >= 1.5
    'weak_regime_ev_floor': -1.0,  # 6) 弱い地合いサブグループでも期待値が大幅マイナスでない
                                   #    (「大幅マイナス」= -1.0% 未満を割らないこと、を閾値とする)
}


def evaluate_promotion(summary, observation_days, criteria=PROMOTION_CRITERIA_V1):
    """summary(観測集計) と経過日数から、各基準の達成状況を返す。
    summary 期待キー:
      closed, expectancy, avg_win, avg_loss, r_multiple,
      excess_vs_dumb, excess_vs_topix, weak_regime_expectancy
    返り値: {criteria, checks:[{no,label,target,actual,pass}], all_pass:bool}
    """
    s = summary or {}
    checks = []

    def chk(no, label, ok, target, actual):
        checks.append({'no': no, 'label': label, 'target': target,
                       'actual': actual, 'pass': bool(ok)})

    days = observation_days if observation_days is not None else 0
    closed = s.get('closed') or 0
    ev = s.get('expectancy')
    r_mult = s.get('r_multiple')
    ex_dumb = s.get('excess_vs_dumb')
    ex_topix = s.get('excess_vs_topix')
    weak_ev = s.get('weak_regime_expectancy')

    chk(1, '観測期間>=90暦日', days >= criteria['min_observation_days'],
        criteria['min_observation_days'], days)
    chk(2, 'クローズ済み>=30件', closed >= criteria['min_closed'],
        criteria['min_closed'], closed)
    chk(3, '期待値>0', (ev is not None and ev > criteria['min_expectancy']),
        f">{criteria['min_expectancy']}", ev)
    chk(4, 'baseline比超過>0(dumb/TOPIX両方)',
        (ex_dumb is not None and ex_topix is not None
         and ex_dumb > criteria['baseline_excess_gt'] and ex_topix > criteria['baseline_excess_gt']),
        f">{criteria['baseline_excess_gt']}(両方)", {'dumb': ex_dumb, 'topix': ex_topix})
    chk(5, 'R倍数>=1.5', (r_mult is not None and r_mult >= criteria['min_r_multiple']),
        criteria['min_r_multiple'], r_mult)
    chk(6, '弱地合でEVが大幅マイナスでない',
        (weak_ev is None or weak_ev >= criteria['weak_regime_ev_floor']),
        f">={criteria['weak_regime_ev_floor']}", weak_ev)

    return {
        'criteria_version': PROMOTION_CRITERIA_VERSION,
        'criteria': criteria,
        'checks': checks,
        'all_pass': all(c['pass'] for c in checks),
        'days_elapsed': days,
    }
