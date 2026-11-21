"""计费重量交接凭证的纯函数服务。

船公司按主单申报毛重结算运费，港口以包装换算后逐层可核验的子层重量为核验依据。
本模块不修改原始申报记录：补正只进入 state['billing']['corrections']，
原申报依据始终保留在 records / weight_checks 中可追溯。
"""
from decimal import Decimal
from copy import deepcopy

from app.errors import BusinessError

GROSS = 'gross_kg'
NET = 'net_kg'
WEIGHT_FIELDS = (GROSS, NET)


def _D(value):
    return Decimal(str(value))


def _sum(values):
    return sum((_D(v) for v in values), Decimal(0))


def subtree_ids(root, children):
    """返回主单完整子树节点（含主单自身），环上节点只出现一次。"""
    seen = {root}
    stack = [root]
    while stack:
        node = stack.pop()
        for child in children.get(node, []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def _correction_map(corrections):
    by_id = {}
    for item in corrections:
        node_id = str(item.get('id', '')).strip()
        if not node_id:
            raise BusinessError('BAD_CORRECTION', '补正编号不能为空')
        patch = {}
        for field in WEIGHT_FIELDS:
            if field in item and item[field] is not None:
                try:
                    value = _D(item[field])
                except Exception:
                    raise BusinessError('BAD_CORRECTION', f'{node_id} 补正重量格式错误')
                if value < 0:
                    raise BusinessError('BAD_CORRECTION', f'{node_id} 补正重量不能为负')
                patch[field] = str(value)
        if not patch:
            raise BusinessError('BAD_CORRECTION', f'{node_id} 未提供净毛重补正')
        by_id[node_id] = {**by_id.get(node_id, {}), **patch}
    return by_id


def verified_weights(records, children, corrections):
    """自底向上按子层求和得到可核验净毛重；补正叶子覆盖原值，环上节点回退原申报。"""
    by_id = _correction_map(corrections)
    nodes = {r['id']: r for r in records}
    verified = {}

    def walk(node, active):
        if node in verified or node in active:
            return
        active.add(node)
        kids = [c for c in children.get(node, []) if c in nodes]
        for child in kids:
            walk(child, active)
        for field in WEIGHT_FIELDS:
            if node in by_id and field in by_id[node]:
                # 补正是显式证据，叶子与非叶子节点均以补正值为准，再向上逐层汇总。
                value = _D(by_id[node][field])
            elif kids and all(child in verified for child in kids):
                value = _sum(verified[child][field] for child in kids)
            else:
                value = _D(nodes[node][field])
            verified.setdefault(node, {})[field] = str(value)
        active.discard(node)

    for node in nodes:
        walk(node, set())
    return verified, by_id


def corrected_checks(records, children, corrections):
    """补正到达后逐层重新核验，结构与 weight 模块一致，便于同屏对照原结论。"""
    verified, _ = verified_weights(records, children, corrections)
    nodes = {r['id']: r for r in records}
    results = []
    for parent, kids in sorted(children.items()):
        if not kids or parent not in nodes:
            continue
        for field in WEIGHT_FIELDS:
            total = _sum(verified[child][field] for child in kids if child in verified)
            declared = _D(nodes[parent][field])
            difference = declared - total
            results.append({
                'id': parent, 'field': field, 'declared': str(declared),
                'children_sum': str(total), 'difference': str(difference),
                'code': 'BALANCED' if not difference else 'WEIGHT_MISMATCH',
            })
    return results


def _subtree_blocks(root, state, sub, corrected):
    blocks = []
    for error in state['graph']['errors']:
        if error['id'] in sub:
            blocks.append({'id': error['id'], 'code': error['code'], 'detail': '拆并关系未闭合'})
    for check in corrected:
        if check['id'] in sub and check['code'] != 'BALANCED':
            blocks.append({'id': check['id'], 'code': check['code'],
                           'detail': f"{check['field']} 逐层差异 {check['difference']}kg"})
    for row in state['quarantine']:
        if row.get('id') in sub:
            blocks.append({'id': row['id'], 'code': row['code'], 'detail': '采信隔离未处理'})
    for issue in state['hazard_issues']:
        if issue['id'] in sub:
            blocks.append({'id': issue['id'], 'code': issue['code'], 'detail': '危险品核验未闭合'})
    return blocks


def _voucher(root, state, sub, verified, corrected, basis, version_by_root):
    nodes = {r['id']: r for r in state['records']}
    declared_gross = _D(nodes[root][GROSS])
    declared_net = _D(nodes[root][NET])
    evidence_gross = _D(verified[root][GROSS])
    evidence_net = _D(verified[root][NET])
    difference = evidence_gross - declared_gross
    blocks = _subtree_blocks(root, state, sub, corrected)
    evidence_closed = not blocks

    if basis == 'declared':
        basis_used = 'DECLARED_GROSS'
        billing_weight = declared_gross
        basis_note = '合同基准：按主单申报毛重结算，差异挂账不调整计费重量'
    elif evidence_closed:
        basis_used = 'VERIFIED_GROSS'
        billing_weight = evidence_gross
        basis_note = '合同基准：可核验重量，证据已逐层对平'
    else:
        basis_used = 'DECLARED_GROSS_PENDING'
        billing_weight = declared_gross
        basis_note = '合同基准要求可核验重量，但证据未闭合，暂按申报毛重挂账'

    if evidence_closed and not difference and basis_used != 'DECLARED_GROSS_PENDING':
        settlement = '按计费重量出账，无差异挂账'
    elif basis_used == 'VERIFIED_GROSS':
        settlement = f'按可核验毛重出账，与申报差异 {difference}kg 随运费补差'
    elif basis_used == 'DECLARED_GROSS':
        settlement = f'按申报毛重出账，可核验差异 {difference}kg 仅挂账'
    else:
        settlement = '证据未对平，暂挂账不得结算补差'

    if not evidence_closed:
        status = 'SUSPENDED'
    elif basis_used == 'DECLARED_GROSS' and difference:
        status = 'VOUCHED_DIFF'
    else:
        status = 'VOUCHED'

    return {
        'root_id': root,
        'revision': nodes[root]['revision'],
        'declared_gross_kg': str(declared_gross),
        'declared_net_kg': str(declared_net),
        'verified_gross_kg': str(evidence_gross),
        'verified_net_kg': str(evidence_net),
        'difference_kg': str(difference),
        'billing_basis': basis_used,
        'billing_basis_note': basis_note,
        'billing_weight_kg': str(billing_weight),
        'settlement': settlement,
        'block_count': len(blocks),
        'blocks': blocks,
        'status': status,
        'sealable': evidence_closed,
        'voucher_version': version_by_root.get(root, 1),
    }


def build_billing(state, basis=None):
    """依据当前 state（含已登记补正）重算全部主单凭证与批次结论。"""
    basis = basis or state['context'].get('billing_basis', 'verified')
    if basis not in {'verified', 'declared'}:
        raise BusinessError('BAD_BILLING_BASIS', "计费基准须为 verified 或 declared")
    corrections = state.get('billing', {}).get('corrections', [])
    verified, _ = verified_weights(state['records'], state['graph']['children'], corrections)
    corrected = corrected_checks(state['records'], state['graph']['children'], corrections)
    previous = state.get('billing', {})
    vouchers = []
    for root in state['graph']['roots']:
        sub = subtree_ids(root, state['graph']['children'])
        vouchers.append(_voucher(root, state, sub, verified, corrected, basis,
                                 previous.get('voucher_versions', {})))
    blocked_roots = [v['root_id'] for v in vouchers if not v['sealable']]
    declaration_open = bool(state['balance']['declaration_differences'])
    return {
        'basis_contract': basis,
        'vouchers': vouchers,
        'corrected_weight_checks': corrected,
        'corrections': deepcopy(corrections),
        'voucher_versions': deepcopy(previous.get('voucher_versions', {})),
        'ledger': deepcopy(previous.get('ledger', [])),
        'snapshots': deepcopy(previous.get('snapshots', [])),
        'blocked_roots': blocked_roots,
        'declaration_open': declaration_open,
        'sealable': not blocked_roots and not declaration_open,
    }


def apply(state):
    """模块入口：支持通过 overrides.corrections / billing_basis 演示补正链路。"""
    overrides = state.get('billing_overrides') or {}
    billing = state.get('billing') or {'corrections': [], 'ledger': [], 'voucher_versions': {}}
    if overrides.get('corrections'):
        billing = deepcopy(billing)
        billing['corrections'] = list(billing.get('corrections', [])) + list(
            overrides['corrections'])
    state['billing'] = billing
    state['billing'] = build_billing(state, overrides.get('billing_basis'))
    state['views']['billing'] = {
        'title': '计费重量交接凭证',
        'rows': state['billing']['vouchers'],
        'metrics': {
            '主单': len(state['billing']['vouchers']),
            '阻断主单': len(state['billing']['blocked_roots']),
            '批次封存': '可封存' if state['billing']['sealable'] else '待处理',
        },
    }
