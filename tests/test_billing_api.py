from decimal import Decimal

from fastapi.testclient import TestClient
from app import main


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'billing.sqlite'))
    return TestClient(main.app)


def _run(c, dataset='clean'):
    return c.post('/api/runs', json={'dataset': dataset}).json()


def test_correction_endpoint_persists_and_reevaluates(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = _run(c)
    rid = run['manifest']['run_id']
    resp = c.post(f'/api/runs/{rid}/corrections', json={
        'request_id': 'REQ-1',
        'corrections': [{'id': 'M002', 'gross_kg': '442'}],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body['ledger_entry']['affected_roots'] == ['M002']
    # 重新读取同一 run：补正与凭证版次已持久化，不产生重复批次。
    stored = c.get(f'/api/runs/{rid}').json()
    m002 = next(v for v in stored['billing']['vouchers'] if v['root_id'] == 'M002')
    assert m002['billing_weight_kg'] == '442'
    assert m002['voucher_version'] == 2
    assert len(c.get('/api/runs').json()) == 1
    # 封存结论随计费重量变化重新出结论，封存哈希改变。
    assert stored['archive']['seal'] != run['archive']['seal']


def test_retry_same_batch_is_idempotent(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = _run(c)['manifest']['run_id']
    payload = {'request_id': 'REQ-1',
               'corrections': [{'id': 'M002', 'gross_kg': '442'}]}
    first = c.post(f'/api/runs/{rid}/corrections', json=payload).json()
    second = c.post(f'/api/runs/{rid}/corrections', json=payload).json()
    assert second['idempotent'] is True
    assert second['billing']['ledger'] == first['billing']['ledger']
    assert len(second['billing']['ledger']) == 1
    stored = c.get(f'/api/runs/{rid}').json()
    assert len(stored['billing']['ledger']) == 1


def test_rollback_endpoint_restores_previous_version(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = _run(c)['manifest']['run_id']
    c.post(f'/api/runs/{rid}/corrections', json={
        'request_id': 'REQ-1', 'corrections': [{'id': 'M002', 'gross_kg': '442'}]})
    rb = c.post(f'/api/runs/{rid}/billing/rollback', json={'request_id': 'REQ-RB'})
    assert rb.status_code == 200
    stored = c.get(f'/api/runs/{rid}').json()
    m002 = next(v for v in stored['billing']['vouchers'] if v['root_id'] == 'M002')
    assert Decimal(m002['billing_weight_kg']) == 440
    assert m002['voucher_version'] == 1
    assert stored['billing']['corrections'] == []
    # 回滚请求自身也按 request_id 幂等。
    again = c.post(f'/api/runs/{rid}/billing/rollback', json={'request_id': 'REQ-RB'})
    assert again.json()['idempotent'] is True


def test_correction_unknown_node_rejected(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = _run(c)['manifest']['run_id']
    resp = c.post(f'/api/runs/{rid}/corrections', json={
        'request_id': 'REQ-X', 'corrections': [{'id': 'GHOST', 'gross_kg': '1'}]})
    assert resp.status_code == 422
    assert resp.json()['code'] == 'UNKNOWN_REFERENCE'


def test_correction_requires_request_id(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = _run(c)['manifest']['run_id']
    resp = c.post(f'/api/runs/{rid}/corrections',
                  json={'corrections': [{'id': 'M002', 'gross_kg': '442'}]})
    assert resp.status_code == 422
    assert resp.json()['code'] == 'BAD_REQUEST_ID'


def test_billing_basis_override(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs',
                 json={'dataset': 'clean', 'billing_basis': 'declared'}).json()
    assert all(v['billing_basis'] == 'DECLARED_GROSS'
               for v in run['billing']['vouchers'])
    bad = c.post('/api/runs', json={'billing_basis': 'market'})
    assert bad.status_code == 422


def test_billing_page_and_archive_page_render(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = _run(c, dataset='dirty')['manifest']['run_id']
    billing_page = c.get(f'/billing?run_id={rid}')
    assert billing_page.status_code == 200
    assert '计费重量交接凭证' in billing_page.text
    archive_page = c.get(f'/archive?run_id={rid}')
    assert archive_page.status_code == 200
    assert '计费交接结论' in archive_page.text


def test_dirty_batch_cannot_seal_until_all_closed(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = _run(c, dataset='dirty')['manifest']['run_id']
    initial = c.get(f'/api/runs/{rid}').json()
    assert initial['billing']['sealable'] is False
    assert initial['billing']['blocked_roots'] == ['M001']
    # 只补正重量：MISSING_UN 与申报差异仍在，批次依旧不能封存。
    c.post(f'/api/runs/{rid}/corrections', json={
        'request_id': 'REQ-1', 'corrections': [{'id': 'M001', 'gross_kg': '440'}]})
    after = c.get(f'/api/runs/{rid}').json()
    assert after['billing']['blocked_roots'] == ['M001']
    assert after['archive']['status'] == '待处理'


def test_rerun_keeps_immutable_input_snapshot(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = _run(c)
    rid = run['manifest']['run_id']
    c.post(f'/api/runs/{rid}/corrections', json={
        'request_id': 'REQ-1', 'corrections': [{'id': 'M002', 'gross_kg': '442'}]})
    # rerun 基于不可变输入快照重算，得到不含补正的新/同批次结果，原 run 补正仍保留。
    rerun = c.post(f'/api/runs/{rid}/rerun').json()
    assert rerun['manifest']['run_id'] == rid
    stored = c.get(f'/api/runs/{rid}').json()
    m002 = next(v for v in stored['billing']['vouchers'] if v['root_id'] == 'M002')
    assert m002['billing_weight_kg'] == '442'


def test_export_bundle_contains_billing_evidence(tmp_path, monkeypatch):
    import io, json, zipfile
    c = _client(tmp_path, monkeypatch)
    rid = _run(c)['manifest']['run_id']
    c.post(f'/api/runs/{rid}/corrections', json={
        'request_id': 'REQ-1', 'corrections': [{'id': 'M002', 'gross_kg': '442'}]})
    z = zipfile.ZipFile(io.BytesIO(c.get(f'/api/export/{rid}').content))
    evidence = json.loads(z.read('evidence.json'))
    assert 'billing' in evidence['views']
    assert evidence['views']['billing']['rows'][0]['root_id'].startswith('M00')
