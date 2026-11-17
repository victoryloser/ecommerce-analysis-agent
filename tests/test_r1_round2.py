"""Fixed-planner conversation contracts on optional local public data."""
from decimal import Decimal
import json
import pytest

from app.executor import Executor
from app.models import AnalysisPlan, PlanUpdate
from app.orchestrator import AnalysisAgent
from app.session import Session
from test_round2_amounts import plan_for
from test_round2_reference import public_data, pytestmark, PACKAGE
from test_semifinal_state import Scripted


def change(manifest, changes=(), **kwargs):
    return AnalysisPlan(dataset_id=manifest['dataset_id'], metadata_version=manifest['metadata_version'],
        intent='update', updates=[PlanUpdate(**c) for c in changes], **kwargs)


@pytest.mark.parametrize('name', ['D1', 'D2', 'D3', 'D4'])
def test_official_continuous_a_b_contracts(public_data, name, monkeypatch, tmp_path):
    import app.config, app.evidence, app.trace, app.presentation
    for module in (app.config, app.evidence, app.trace, app.presentation):
        monkeypatch.setattr(module, 'RUNS_DIR', tmp_path / 'runs')
    questions = {line.split('|')[1]: line.split('|')[2] for line in (PACKAGE / '03_综合问题与连续追问.md').read_text(encoding='utf-8').splitlines() if line.startswith(('|A', '|B'))}
    directory, datasets = public_data
    manifest, reference, _ = datasets[name]
    executor = Executor(name, directory)
    comparison = plan_for(manifest, intent='compare', comparison_range={'start': '2027-03-01', 'end': '2027-04-01'})
    a2 = change(manifest, [dict(operation='set', field='intent', values=['contribution']),
        dict(operation='set', field='dimension', values=['category']),
        dict(operation='set', field='metric_ids', values=['sales']),
        dict(operation='set', field='limit_mode', values=['all'])], limitations=['causality'])
    a3 = change(manifest, [dict(operation='set', field='time_range', period={'start':'2027-03-01','end':'2027-04-01'}),
        dict(operation='set', field='intent', values=['aggregate']), dict(operation='set', field='dimension', values=['total']),
        dict(operation='set', field='category_id', values=['electronics']),
        dict(operation='set', field='metric_ids', values=['sales','freight','order_count'])])
    a4 = change(manifest, [dict(operation='remove', field='category_id'), dict(operation='set', field='top_n', values=[3])], output_mode='seller_audit')
    agent = AnalysisAgent(Scripted(comparison, a2, a3, a4), {name:executor})
    session = Session(name)
    first = agent.respond(session, questions['A1'])
    assert first.evidence['result']['status'] == 'ok'
    second = agent.respond(session, questions['A2'])
    assert len(second.evidence['result']['rows']) == 6 and '不能证明' in second.text
    third = agent.respond(session, questions['A3'])
    assert third.evidence['plan']['comparison_range'] is None
    assert third.evidence['result']['rows'][0][2] == reference['A3']['delivered_order_count']
    fourth = agent.respond(session, questions['A4'])
    from app.round2_judge import judge
    for case, turn in zip(('A1', 'A2', 'A3', 'A4'), (first, second, third, fourth)):
        verdict = judge(case, manifest, datasets[name][2], turn.evidence, turn.text, turn_id=turn.evidence['turn_id'])
        assert verdict['passed'], [c for c in verdict['checks'] if not c['passed']]
    assert all(t['status']=='completed' for t in session.workflow_tasks), fourth.text
    rank = fourth.evidence['steps'][0]['result']
    assert {r[0] for r in rank['rows']} == {r['seller_id'] for r in reference['A4']['ranking']}
    header = fourth.evidence['steps'][2]['result']
    h = dict(zip(header['columns'], header['rows'][0]))
    items = fourth.evidence['steps'][3]['result']
    rows = [dict(zip(items['columns'], r)) for r in items['rows']]
    assert sum(Decimal(r['price']) for r in rows) == Decimal(h['order_sales'])
    assert sum(Decimal(r['price']) for r in rows if r['belongs_to_selected_seller']) == Decimal(h['seller_sales'])
    assert fourth.evidence['calls_used'] == 5

    b2 = change(manifest, scope_source='analysis', output_mode='quality_check')
    b3 = change(manifest, [dict(operation='set', field='intent', values=['aggregate']),
        dict(operation='set', field='time_range', period={'start':'2027-04-01','end':'2027-05-01'}),
        dict(operation='set', field='dimension', values=['total']), dict(operation='set', field='category_id', values=['electronics']),
        dict(operation='set', field='metric_ids', values=['sales','freight','order_count'])], scope_source='analysis', limitations=['profit'])
    b4 = change(manifest, [dict(operation='reset'), dict(operation='set', field='time_range', period_mode='full_coverage')], output_mode='customer_summary')
    agent = AnalysisAgent(Scripted(comparison, b2, b3, b4), {name:executor})
    session = Session(name)
    first = agent.respond(session, questions['B1'])
    anchor = first.evidence['record_id']
    second = agent.respond(session, questions['B2'])
    assert all(t['status']=='completed' for t in session.workflow_tasks), second.text
    assert session.analysis_anchor == anchor
    detail = second.evidence['steps'][1]['result']
    rows = [dict(zip(detail['columns'], r)) for r in detail['rows']]
    if name == 'D4':
        assert {(r['issue_type'],r['order_id']) for r in rows} == {
            ('exact_duplicate','F003113'),('exact_duplicate','F007088'),
            ('price_conflict','F001738'),('price_conflict','F002589'),
            ('missing_payment','F004642'),('missing_payment','F009223')}
    else:
        assert rows == []
    third = agent.respond(session, questions['B3'])
    sales, freight, orders = third.evidence['result']['rows'][0]
    assert Decimal(sales)*100 == reference['B3']['goods_amount_cents']
    assert Decimal(freight)*100 == reference['B3']['freight_amount_cents']
    assert orders == reference['B3']['delivered_order_count']
    assert '无法计算促销' in third.text
    fourth = agent.respond(session, questions['B4'])
    for case, turn in zip(('B1', 'B2', 'B3', 'B4'), (first, second, third, fourth)):
        verdict = judge(case, manifest, datasets[name][2], turn.evidence, turn.text, turn_id=turn.evidence['turn_id'])
        assert verdict['passed'], [c for c in verdict['checks'] if not c['passed']]
    assert all(t['status']=='completed' for t in session.workflow_tasks), fourth.text
    assert fourth.evidence['steps'][0]['result']['rows'][0][0] == 10714
    assert fourth.evidence['steps'][1]['result']['rows'][0][0] == 2378
    assert session.working_plan.time_range.start.isoformat() == manifest['coverage']['start']
    assert all(not s.filters and s.item_where is None and s.selected_order_id is None for s in session.working_plan.steps)

    events = [json.loads(line) for path in (tmp_path / 'runs' / name / 'events').glob('*.jsonl') for line in path.read_text(encoding='utf-8').splitlines()]
    sizes = [e['context_bytes'] for e in events if e['event']=='planning_requested']
    assert len(sizes)==8 and max(sizes)<=48000
