"""补正到达后的重算服务：凭证切换中断可回滚，同批重试沿用原凭证不重复出结论。

原申报依据（records / weight_checks / 输入快照）永不改写；
补正只登记在 state['billing']['corrections']，每次切换前保存上一版凭证快照。
"""
from copy import deepcopy

from app import billing
from app.errors import BusinessError


def _reconclude(state):
    """计费重量变化后，受影响的对账结论与封存状态重新出结论。"""
    if 'archive' in state:
        from app.modules.archive.rules import decide
        state['archive'] = decide(state)


def _find_root_for_id(state, node_id):
    for root in state['graph']['roots']:
        if node_id in billing.subtree_ids(root, state['graph']['children']):
            return root
    return None


def _validate_corrections(state, corrections):
    if not isinstance(corrections, list) or not corrections:
        raise BusinessError('BAD_CORRECTION', '补正须为非空数组')
    known = {r['id'] for r in state['records']}
    for item in corrections:
        if not isinstance(item, dict):
            raise BusinessError('BAD_CORRECTION', '补正项须为对象')
        node_id = str(item.get('id', '')).strip()
        if node_id not in known:
            raise BusinessError('UNKNOWN_REFERENCE', f'补正编号 {node_id} 不在本批舱单内')
        if not any(field in item and item[field] is not None
                   for field in ('gross_kg', 'net_kg')):
            raise BusinessError('BAD_CORRECTION', f'{node_id} 未提供净毛重补正')
        root = _find_root_for_id(state, node_id)
        if root is None:
            raise BusinessError('UNKNOWN_REFERENCE', f'{node_id} 无法归属任何主单')


def apply_corrections(state, corrections, request_id, basis=None):
    """登记补正并重算受影响主单；切换中断时调用方可凭 snapshot 回滚到上一版。"""
    _validate_corrections(state, corrections)
    current = state.setdefault('billing', {
        'corrections': [], 'ledger': [], 'voucher_versions': {}, 'snapshots': []})

    # 同一批重试（相同 request_id）沿用原凭证，直接返回原结论，不新增台账与版次。
    for entry in current.get('ledger', []):
        if entry.get('request_id') and entry['request_id'] == request_id:
            return state, {'idempotent': True, 'ledger_entry': entry}

    before = deepcopy(current)
    affected = sorted({_find_root_for_id(state, c['id']) for c in corrections})
    stamped = [dict(c, request_id=request_id) for c in corrections]
    current['corrections'] = list(current.get('corrections', [])) + stamped
    try:
        rebuilt = billing.build_billing(state, basis)
    except Exception:
        # 凭证切换中断：回滚到上一版，原申报依据与已封存凭证不受影响。
        state['billing'] = before
        raise
    rebuilt['ledger'] = list(current.get('ledger', []))
    rebuilt['snapshots'] = list(current.get('snapshots', []))
    rebuilt['voucher_versions'] = dict(current.get('voucher_versions', {}))

    old_weights = {v['root_id']: v['billing_weight_kg']
                   for v in current.get('vouchers', [])}
    changed_roots = []
    for voucher in rebuilt['vouchers']:
        root = voucher['root_id']
        if root in affected:
            prev = rebuilt['voucher_versions'].get(root, voucher['voucher_version'])
            voucher['voucher_version'] = prev + 1
            rebuilt['voucher_versions'][root] = voucher['voucher_version']
            changed_roots.append(root)
        elif old_weights.get(root) != voucher['billing_weight_kg']:
            # 同一张主单计费重量变化（叶子补正上卷）：受影响对账与封存重新出结论。
            prev = rebuilt['voucher_versions'].get(root, voucher['voucher_version'])
            voucher['voucher_version'] = prev + 1
            rebuilt['voucher_versions'][root] = voucher['voucher_version']
            changed_roots.append(root)

    entry = {
        'request_id': request_id,
        'applied_corrections': deepcopy(stamped),
        'affected_roots': sorted(set(affected) | set(changed_roots)),
        'previous_version': {r: rebuilt['voucher_versions'].get(r, 1) - 1
                             for r in changed_roots},
        'new_version': {r: rebuilt['voucher_versions'][r] for r in changed_roots},
        'rollback': False,
    }
    rebuilt['ledger'].append(entry)
    rebuilt['snapshots'].append(before)
    state['billing'] = rebuilt
    _reconclude(state)
    state['views']['billing'] = {
        'title': '计费重量交接凭证',
        'rows': rebuilt['vouchers'],
        'metrics': {
            '主单': len(rebuilt['vouchers']),
            '阻断主单': len(rebuilt['blocked_roots']),
            '批次封存': '可封存' if rebuilt['sealable'] else '待处理',
        },
    }
    return state, {'idempotent': False, 'ledger_entry': entry}


def rollback_last(state, request_id):
    """回滚最近一次凭证切换，恢复上一版凭证与版次；原申报依据不动。"""
    current = state.get('billing') or {}
    for entry in current.get('ledger', []):
        if entry.get('request_id') == request_id and entry.get('rollback'):
            return state, {'idempotent': True, 'ledger_entry': entry}
    snapshots = current.get('snapshots', [])
    if not snapshots:
        raise BusinessError('NO_SNAPSHOT', '没有可回滚的凭证版本')
    previous = snapshots[-1]
    target_request = current['ledger'][-1]['request_id'] if current.get('ledger') else None
    restored = deepcopy(previous)
    rebuilt = billing.build_billing({**state, 'billing': restored},
                                    state['context'].get('billing_basis'))
    rebuilt['ledger'] = list(restored.get('ledger', []))
    rebuilt['snapshots'] = list(restored.get('snapshots', []))
    rebuilt['voucher_versions'] = dict(restored.get('voucher_versions', {}))
    # 回滚到上一版：凭证版次以快照里的版次为准，缺失键回落到初始版次 1。
    for voucher in rebuilt['vouchers']:
        voucher['voucher_version'] = rebuilt['voucher_versions'].get(
            voucher['root_id'], voucher['voucher_version'])
    rollback_entry = {
        'request_id': request_id,
        'rollback': True,
        'rolled_back_request': target_request,
        'restored_versions': deepcopy(rebuilt['voucher_versions']),
    }
    rebuilt['ledger'].append(rollback_entry)
    state['billing'] = rebuilt
    _reconclude(state)
    state['views']['billing'] = {
        'title': '计费重量交接凭证',
        'rows': rebuilt['vouchers'],
        'metrics': {
            '主单': len(rebuilt['vouchers']),
            '阻断主单': len(rebuilt['blocked_roots']),
            '批次封存': '可封存' if rebuilt['sealable'] else '待处理',
        },
    }
    return state, {'idempotent': False, 'ledger_entry': rollback_entry}
