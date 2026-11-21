from copy import deepcopy
from decimal import Decimal

import pytest

from app.engine import evaluate
from app import billing, billing_service
from app.errors import BusinessError


def test_clean_each_root_vouched_verified(clean):
    assert clean['billing']['sealable'] is True
    for voucher in clean['billing']['vouchers']:
        assert voucher['status'] == 'VOUCHED'
        assert voucher['billing_basis'] == 'VERIFIED_GROSS'
        assert voucher['billing_weight_kg'] == voucher['verified_gross_kg']
        assert voucher['difference_kg'] == '0.00'
        assert voucher['sealable'] is True


def test_dirty_suspended_root_keeps_declared_gross(sample):
    vouchers = {v['root_id']: v for v in sample['billing']['vouchers']}
    m001 = vouchers['M001']
    # 逐层核验未闭合时不能封存，合同基准要求可核验重量则暂按申报毛重挂账。
    assert m001['status'] == 'SUSPENDED'
    assert m001['billing_basis'] == 'DECLARED_GROSS_PENDING'
    assert m001['billing_weight_kg'] == m001['declared_gross_kg'] == '441'
    assert m001['verified_gross_kg'] == '440.00'
    assert m001['difference_kg'] == '-1.00'
    assert m001['sealable'] is False
    codes = {b['code'] for b in m001['blocks']}
    assert {'WEIGHT_MISMATCH', 'MISSING_UN'} <= codes
    assert sample['billing']['sealable'] is False
    assert sample['billing']['blocked_roots'] == ['M001']


def test_declared_contract_basis_settles_on_declaration(clean):
    state = evaluate({'dataset': 'clean', 'billing_basis': 'declared'})
    for voucher in state['billing']['vouchers']:
        assert voucher['billing_basis'] == 'DECLARED_GROSS'
        assert voucher['billing_weight_kg'] == voucher['declared_gross_kg']


def test_declared_basis_with_open_difference_only_books(sample):
    state = evaluate({'billing_basis': 'declared'})
    m001 = next(v for v in state['billing']['vouchers'] if v['root_id'] == 'M001')
    # 申报口径优先：仍按申报毛重出账，但证据未闭合时一样不能封存。
    assert m001['billing_basis'] == 'DECLARED_GROSS'
    assert m001['billing_weight_kg'] == '441'
    assert m001['sealable'] is False
    assert '挂账' in m001['settlement']


def test_correction_recomputes_affected_root_and_rolls_version(sample):
    state = deepcopy(sample)
    # M001 主单申报 441，子层逐层合计 440；补正主单毛重到 440 后重量差异对平。
    state, info = billing_service.apply_corrections(
        state, [{'id': 'M001', 'gross_kg': '440'}], 'REQ-1')
    assert info['idempotent'] is False
    m001 = next(v for v in state['billing']['vouchers'] if v['root_id'] == 'M001')
    assert m001['verified_gross_kg'] == '440'
    # 原申报 441 与显式补正证据 440 尚未对平，重量差异仍作为阻断保留至证据闭合。
    block_codes = {b['code'] for b in m001['blocks']}
    assert 'WEIGHT_MISMATCH' in block_codes and 'MISSING_UN' in block_codes
    # 补正后核验表保留原申报 441 与可核验 440 的对照，原申报依据可追溯。
    trace = next(c for c in state['billing']['corrected_weight_checks']
                 if c['id'] == 'M001' and c['field'] == 'gross_kg')
    assert trace['declared'] == '441' and trace['children_sum'] == '440.00'
    assert m001['voucher_version'] == 2
    # 危险类 UN 缺失仍在，单张主单仍不能封存，故计费重量暂挂申报毛重。
    assert m001['sealable'] is False
    assert m001['billing_basis'] == 'DECLARED_GROSS_PENDING'
    assert Decimal(m001['billing_weight_kg']) == 441
    other = next(v for v in state['billing']['vouchers'] if v['root_id'] == 'M002')
    assert other['voucher_version'] == 1
    assert info['ledger_entry']['affected_roots'] == ['M001']


def test_correction_on_clean_root_switches_to_verified_weight(clean):
    state = deepcopy(clean)
    # 干净批无其它阻断，补正后可核验重量闭合，计费重量即切换并可封存。
    state, _ = billing_service.apply_corrections(
        state, [{'id': 'M002', 'gross_kg': '442'}], 'REQ-C')
    m002 = next(v for v in state['billing']['vouchers'] if v['root_id'] == 'M002')
    assert m002['billing_basis'] == 'VERIFIED_GROSS'
    assert m002['billing_weight_kg'] == '442'
    assert Decimal(m002['difference_kg']) == 2
    assert m002['sealable'] is True
    assert m002['voucher_version'] == 2
    assert '补差' in m002['settlement']
    # M6 报关总量按未改写的原申报汇总，原申报依据不受补正影响。
    assert state['balance']['gross_kg'] == '2640'


def test_original_declaration_remains_traceable(sample):
    state = deepcopy(sample)
    billing_service.apply_corrections(
        state, [{'id': 'M001', 'gross_kg': '440'}], 'REQ-1')
    node = next(r for r in state['records'] if r['id'] == 'M001')
    assert node['gross_kg'] == '441'
    check = next(c for c in state['weight_checks']
                 if c['id'] == 'M001' and c['field'] == 'gross_kg')
    assert check['declared'] == '441'
    corrected = next(c for c in state['billing']['corrected_weight_checks']
                     if c['id'] == 'M001' and c['field'] == 'gross_kg')
    assert corrected['children_sum'] == '440.00'


def test_retry_same_request_is_idempotent(sample):
    state = deepcopy(sample)
    first, info1 = billing_service.apply_corrections(
        state, [{'id': 'M001', 'gross_kg': '440'}], 'REQ-1')
    versions1 = deepcopy(first['billing']['voucher_versions'])
    second, info2 = billing_service.apply_corrections(
        first, [{'id': 'M001', 'gross_kg': '440'}], 'REQ-1')
    assert info2['idempotent'] is True
    assert len(second['billing']['ledger']) == 1
    assert second['billing']['voucher_versions'] == versions1
    assert second['billing']['vouchers'] == first['billing']['vouchers']


def test_rollback_restores_previous_voucher(sample):
    state = deepcopy(sample)
    billing_service.apply_corrections(
        state, [{'id': 'M001', 'gross_kg': '440'}], 'REQ-1')
    assert state['billing']['voucher_versions']['M001'] == 2
    state, info = billing_service.rollback_last(state, 'REQ-ROLLBACK')
    assert info['idempotent'] is False
    m001 = next(v for v in state['billing']['vouchers'] if v['root_id'] == 'M001')
    assert m001['billing_weight_kg'] == '441'
    assert m001['voucher_version'] == 1
    # 回滚后版本台账回到切换前（初始无升版记录），原申报依据仍在，补正清空。
    assert 'M001' not in state['billing']['voucher_versions']
    assert state['billing']['corrections'] == []


def test_rollback_request_id_is_idempotent(sample):
    state = deepcopy(sample)
    billing_service.apply_corrections(
        state, [{'id': 'M001', 'gross_kg': '440'}], 'REQ-1')
    billing_service.rollback_last(state, 'REQ-RB')
    ledger_size = len(state['billing']['ledger'])
    _, info = billing_service.rollback_last(state, 'REQ-RB')
    assert info['idempotent'] is True
    assert len(state['billing']['ledger']) == ledger_size


def test_rollback_without_snapshot_rejected(clean):
    state = deepcopy(clean)
    with pytest.raises(BusinessError) as exc:
        billing_service.rollback_last(state, 'REQ-X')
    assert exc.value.code == 'NO_SNAPSHOT'


def test_unknown_correction_reference_rejected(clean):
    state = deepcopy(clean)
    with pytest.raises(BusinessError) as exc:
        billing_service.apply_corrections(
            state, [{'id': 'GHOST', 'gross_kg': '1'}], 'REQ-X')
    assert exc.value.code == 'UNKNOWN_REFERENCE'
    assert state['billing'].get('corrections', []) == []


def test_bad_correction_payload_rolls_back(sample):
    state = deepcopy(sample)
    with pytest.raises(BusinessError):
        billing_service.apply_corrections(
            state, [{'id': 'M001', 'gross_kg': 'not-a-number'}], 'REQ-BAD')
    # 切换中断：凭证保持上一版，台账不新增。
    assert state['billing']['vouchers'] == sample['billing']['vouchers']
    assert state['billing']['ledger'] == []


def test_leaf_correction_propagates_bottom_up(clean):
    state = deepcopy(clean)
    # 叶子补正 1kg，父级与主单的可核验毛重逐层重算。
    state, _ = billing_service.apply_corrections(
        state, [{'id': 'M002-P1-1', 'gross_kg': '111'}], 'REQ-LEAF')
    voucher = next(v for v in state['billing']['vouchers']
                   if v['root_id'] == 'M002')
    assert voucher['verified_gross_kg'] == '441.00'
    p1 = next(c for c in state['billing']['corrected_weight_checks']
              if c['id'] == 'M002-P1' and c['field'] == 'gross_kg')
    assert p1['children_sum'] == '221.00'
    assert voucher['voucher_version'] == 2


def test_seal_reconcludes_when_billing_weight_changes(sample):
    state = deepcopy(sample)
    before = state['archive']['seal']
    billing_service.apply_corrections(
        state, [{'id': 'M001', 'gross_kg': '440'}], 'REQ-1')
    state['archive'] = __import__('app.modules.archive.rules', fromlist=['decide']).decide(state)
    # 封存哈希随计费凭证变化而变化；MISSING_UN 未处理所以仍是待处理。
    assert state['archive']['seal'] != before
    assert state['archive']['status'] == '待处理'


def test_billing_view_present(sample):
    view = sample['views']['billing']
    assert view['title'] == '计费重量交接凭证'
    assert len(view['rows']) == 6
