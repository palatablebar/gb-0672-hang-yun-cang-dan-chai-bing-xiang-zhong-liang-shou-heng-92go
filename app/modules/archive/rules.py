import hashlib, json

from app import billing


def decide(state):
    b = state['balance']
    billing_blocked = 0
    if 'billing' in state:
        billing_blocked = len(state['billing']['blocked_roots']) + (
            1 if state['billing']['declaration_open'] else 0)
    blocked = (b['graph_errors'] + b['weight_errors']
               + len(b['declaration_differences']) + len(state['hazard_issues'])
               + len(state['quarantine']) + billing_blocked)
    evidence = {
        'roots': state['graph']['roots'],
        'weights': state['weight_checks'],
        'hazards': state['hazard_issues'],
        'billing': state.get('billing', {}).get('vouchers', []),
    }
    sealable = b['row_balanced'] and not blocked
    return {
        'status': '可封存' if sealable else '待处理',
        'blocked': blocked,
        'billing_blocked': billing_blocked,
        'seal': hashlib.sha256(
            json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
    }


def apply(state):
    # 计费重量变化后，对账与封存结论必须重新出结论：先重建计费凭证再裁决封存。
    state['billing'] = billing.build_billing(state)
    state['archive'] = decide(state)
    state['views']['archive'] = {
        'title': '版本封存',
        'rows': [{'id': r['id'], 'revision': r['revision'], 'parent': r['parent']}
                 for r in state['records']],
        'metrics': {
            '主单': len(state['graph']['roots']),
            '状态': state['archive']['status'],
            '阻断': state['archive']['blocked'],
            '计费阻断': state['archive']['billing_blocked'],
        }}
