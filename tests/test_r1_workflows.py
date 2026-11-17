from copy import deepcopy
from decimal import Decimal
import json

import pytest

from app.calculation import ROUND2_RULE
from app.executor import Executor
from app.import_data import import_tables
from app.models import AnalysisPlan, PlanUpdate
from app.orchestrator import AnalysisAgent
from app.plan_update import apply_updates
from app.session import Session
from app.session_store import SessionStore
from app.task_shapes import expand_output
from test_round2_amounts import source_tables, plan_for, direct
from test_semifinal_state import Scripted


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import app.config, app.evidence, app.trace, app.presentation
    for module in (app.config, app.evidence, app.trace, app.presentation):
        monkeypatch.setattr(module, "RUNS_DIR", tmp_path / "runs")
    tables = source_tables(False)
    tables["order_items"].append(dict(tables["order_items"][0]))
    manifest = import_tables("r1", tables, [], "R1 synthetic", tmp_path, quality_rule_version=ROUND2_RULE)
    executor = Executor("r1", tmp_path)
    return tmp_path, manifest, executor


def test_batch_is_final_and_atomic(setup):
    directory, manifest, executor = setup
    base = plan_for(manifest, intent="compare", comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    changes = [PlanUpdate(operation="set", field="intent", values=["rank"]),
               PlanUpdate(operation="set", field="dimension", values=["seller"]),
               PlanUpdate(operation="set", field="top_n", values=[3])]
    agent = AnalysisAgent(None, {"r1": executor})
    session = Session("r1", working_plan=base)
    turn = agent.update_conditions(session, changes)
    assert session.working_plan.intent == "rank" and session.working_plan.top_n == 3
    assert session.working_plan.comparison_range is None
    old = deepcopy(session.working_plan)
    with pytest.raises(ValueError):
        agent.update_conditions(session, [PlanUpdate(operation="set", field="top_n", values=[0])])
    assert session.working_plan == old and turn.evidence["result"]["status"] == "ok"


def test_quality_comparison_union_and_zero_rows(setup):
    directory, manifest, executor = setup
    plan, result = direct(directory, manifest, intent="diagnose", metric_ids=[], diagnosis="quality_rows",
                          comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    rows = [dict(zip(result.columns, r)) for r in result.rows]
    assert {(r['order_id'], r['issue_type']) for r in rows} == {("M", "exact_duplicate"), ("O2", "missing_payment")}
    _, summary = direct(directory, manifest, intent="diagnose", metric_ids=[], diagnosis="quality_summary",
                         comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    counts = {r[0]: r for r in summary.rows}
    assert counts["all"][2] == 2 and counts["price_conflict"][2] == 0
    assert counts["exact_duplicate"][-1] == 1


def test_order_audit_bundle_has_all_items_and_exact_amounts(setup):
    directory, manifest, executor = setup
    plan = plan_for(manifest, output_mode="seller_audit", top_n=3)
    agent = AnalysisAgent(Scripted(plan), {"r1": executor})
    session = Session("r1")
    turn = agent.respond(session, "rank sellers and inspect an order")
    assert [t['status'] for t in session.workflow_tasks] == ["completed"] * 4, turn.text
    assert turn.evidence["calls_used"] == 5
    header = turn.evidence["steps"][2]["result"]
    row = dict(zip(header["columns"], header["rows"][0]))
    assert row["seller_sales"] == "100.00" and row["order_sales"] == "150.00"
    assert row["payment_amount"] == "165.00"
    items = turn.evidence["steps"][3]["result"]
    assert len(items["rows"]) == 2
    assert sum(r[items['columns'].index('belongs_to_selected_seller')] for r in items['rows']) == 1


def test_workflow_drill_back_restore_keeps_tasks(setup):
    directory, manifest, executor = setup
    plan = expand_output(plan_for(manifest, output_mode="seller_audit", top_n=3))
    store = SessionStore(directory / "sessions.sqlite3")
    agent = AnalysisAgent(Scripted(plan), {"r1": executor}, store)
    session = Session("r1")
    agent.respond(session, "audit")
    original = deepcopy(session.working_plan)
    tasks = deepcopy(session.workflow_tasks)
    agent.drill(session, 0, "orders")
    assert session.working_plan == original and session.workflow_tasks == tasks
    restored = store.load(session.session_id, manifest)
    agent.back(restored)
    assert restored.working_plan == original and restored.workflow_tasks == tasks
    calls = agent.provider.calls
    agent.respond(restored, "/continue")
    assert agent.provider.calls == calls and restored.workflow_tasks == tasks


def test_source_and_downstream_batch_rebinds_new_group(setup):
    directory, manifest, executor = setup
    source = plan_for(manifest, intent='rank', metric_ids=['sales'], group_by=['category','seller'], top_n=1)
    dependent = plan_for(manifest, intent='details', metric_ids=[], source_step=1, bind_mode='group')
    workflow = plan_for(manifest, intent='workflow', metric_ids=[], steps=[source,dependent])
    agent = AnalysisAgent(Scripted(workflow), {'r1':executor})
    session = Session('r1')
    agent.respond(session, 'top combination and details')
    old = session.workflow_tasks[1]['evidence']['plan']
    assert {f['field']:f['values'] for f in old['filters']} == {'category_id':['A'],'seller_id':['A']}
    changes = [PlanUpdate(operation='set', field='time_range', target_step=1, period={'start':'2027-03-01','end':'2027-04-01'}),
               PlanUpdate(operation='set', field='seller_id', target_step=2, values=['B'])]
    turn = agent.update_conditions(session, changes)
    assert all(t['status']=='completed' for t in session.workflow_tasks), turn.text
    effective = session.workflow_tasks[1]['evidence']['plan']
    assert effective['filters'] == [{'field':'category_id','values':['B']}]
    assert effective['item_where']['values'] == ['B']
    assert effective['time_range']['start'] == '2027-03-01'
    assert session.working_plan.steps[1].source_step == 1
    assert session.working_plan.steps[1].binding_updates
    before_plan = session.working_plan.model_copy(deep=True)
    before_tasks = deepcopy(session.workflow_tasks)
    with pytest.raises(ValueError):
        agent.update_conditions(session, [changes[0],
            PlanUpdate(operation='set', field='top_n', target_step=2, values=[0])])
    assert session.working_plan == before_plan and session.workflow_tasks == before_tasks


@pytest.mark.parametrize('scope,orders', [('current',{'O2'}),('previous',{'M'}),('comparison_union',{'M','O2'})])
def test_quality_period_selector_and_button_preserve_analysis(setup, scope, orders):
    directory, manifest, executor = setup
    comparison = plan_for(manifest, intent='compare', comparison_range={'start':'2027-03-01','end':'2027-04-01'}, quality_scope=scope)
    agent = AnalysisAgent(Scripted(comparison), {'r1':executor})
    session = Session('r1')
    first = agent.respond(session, 'compare')
    before = session.working_plan.model_dump()
    turn = agent.quality_details(session, specified=True)
    result = turn.evidence['result']
    assert {r[result['columns'].index('order_id')] for r in result['rows']} == orders
    assert session.working_plan.model_dump() == before and session.last_evidence == first.evidence
    export = agent.quality_export(session)
    assert export['row_count'] == len(orders)


def test_quality_overlapping_and_disjoint_periods(setup):
    directory, manifest, _ = setup
    _, overlap = direct(directory, manifest, intent='diagnose', metric_ids=[], diagnosis='quality_summary',
        time_range={'start':'2027-03-01','end':'2027-05-01'}, comparison_range={'start':'2027-03-01','end':'2027-04-01'})
    assert next(r for r in overlap.rows if r[0]=='all')[2] == 2
    _, gap = direct(directory, manifest, intent='diagnose', metric_ids=[], diagnosis='quality_rows',
        time_range={'start':'2027-03-01','end':'2027-04-01'}, comparison_range={'start':'2027-05-01','end':'2027-06-01'})
    assert {r[gap.columns.index('order_id')] for r in gap.rows} == {'M'}


def test_quality_mixed_duplicate_conflicts_and_unknown_scope(tmp_path):
    tables = source_tables()
    tables['order_items'].append(dict(tables['order_items'][1]))
    tables['order_items'][-2]['freight_value'] = 12
    manifest = import_tables('mixed', tables, [], 'Mixed', tmp_path, quality_rule_version=ROUND2_RULE)
    _, result = direct(tmp_path, manifest, intent='diagnose', metric_ids=[], diagnosis='quality_summary')
    rows = {r[0]:r for r in result.rows}
    assert rows['exact_duplicate'][2] == rows['price_conflict'][2] == 1
    assert rows['exact_duplicate'][-1] == 1 and rows['all'][2] == 2
    _, uncertain = direct(tmp_path, manifest, intent='diagnose', metric_ids=[], diagnosis='quality_summary',
        order_where={'field':'order_sales','operator':'lt','values':[160]})
    rows = {r[0]:r for r in uncertain.rows}
    assert rows['price_conflict'][2] == 0 and rows['price_conflict'][3] == 1


def test_manual_audit_keeps_original_workflow_and_selected_order(setup):
    directory, manifest, executor = setup
    plan = plan_for(manifest, output_mode='seller_audit', top_n=3)
    agent = AnalysisAgent(Scripted(plan), {'r1':executor})
    session = Session('r1')
    agent.respond(session, 'audit sellers')
    original = deepcopy(session.working_plan)
    tasks = deepcopy(session.workflow_tasks)
    turn = agent.drill(session, 0, 'audit_order')
    assert session.working_plan == original and session.workflow_tasks == tasks
    assert session.view_focus['kind'] == 'inspection'
    assert all(t['status']=='completed' for t in turn.evidence['tasks']), turn.text
    resumed = agent.resume_inspection(session)
    assert resumed.evidence['calls_used'] == 0
    agent.back(session)
    assert session.working_plan == original and session.workflow_tasks == tasks


def test_quality_sources_preserve_duplicate_records(tmp_path, monkeypatch):
    import csv
    import app.config, app.evidence, app.trace
    from app.data_upload import discover, suggest_mapping, import_upload
    from app.source import SCHEMA
    for module in (app.config, app.evidence, app.trace):
        monkeypatch.setattr(module, 'RUNS_DIR', tmp_path / 'runs')
    tables = source_tables()
    tables['order_items'].append(dict(tables['order_items'][1]))
    files = []
    for table, records in tables.items():
        path = tmp_path / (table + '.csv')
        with path.open('w',encoding='utf-8-sig',newline='') as handle:
            writer = csv.DictWriter(handle,fieldnames=list(SCHEMA[table]))
            writer.writeheader(); writer.writerows(records)
        files.append(str(path))
    manifest = import_upload(files, suggest_mapping(discover(files)), 'source check', tmp_path / 'data')
    executor = Executor(manifest['dataset_id'], tmp_path / 'data')
    agent = AnalysisAgent(Scripted(plan_for(manifest)), {manifest['dataset_id']:executor})
    session = Session(manifest['dataset_id'])
    agent.respond(session, 'amounts')
    agent.quality_details(session, specified=True)
    evidence = agent.quality_sources(session, 'O1')
    items = [r for r in evidence['records'] if r['table']=='order_items']
    assert len(items) == 4 and len({r['record_number'] for r in items}) == 4
    assert sorted(Decimal(r['values']['price']) for r in items) == [50,100,100,120]
    assert all(r['locator_kind']=='logical_csv_record' for r in items)
    assert agent.quality_sources(session,'O2')['payment_missing']
    with pytest.raises(ValueError, match='当前问题'):
        agent.quality_sources(session,'unselected')


def test_invalid_natural_batch_and_failed_query_keep_correct_intent(setup, monkeypatch):
    from app.models import QueryResult
    from app.sql_compiler import CompiledQuery
    directory, manifest, executor = setup
    base = plan_for(manifest)
    invalid = plan_for(manifest, intent='update', updates=[{'operation':'set','field':'top_n','values':[0]}])
    changed = plan_for(manifest, intent='update', updates=[{'operation':'set','field':'time_range','period':{'start':'2027-03-01','end':'2027-04-01'}}])
    agent = AnalysisAgent(Scripted(base,invalid,changed),{'r1':executor})
    session = Session('r1')
    first = agent.respond(session,'April')
    rejected = agent.respond(session,'invalid batch')
    assert rejected.evidence['status']=='invalid_plan' and session.working_plan == base
    execute = executor.execute_plan
    def fail(s,pid,trace=None,**kwargs):
        s.registered_plans.pop(pid)
        return QueryResult(query_id='failed',status='timeout',error='injected'), CompiledQuery('SELECT 1',())
    monkeypatch.setattr(executor,'execute_plan',fail)
    failed = agent.respond(session,'March')
    assert failed.evidence['result']['status']=='timeout'
    assert session.working_plan.time_range.start.isoformat()=='2027-03-01'
    assert session.analysis_evidence['record_id']==first.evidence['record_id']
    monkeypatch.setattr(executor,'execute_plan',execute)
    retried = agent.respond(session,'/continue')
    assert retried.evidence['result']['status']=='ok' and agent.provider.calls==3


def test_audit_failure_resumes_only_unfinished_steps(setup, monkeypatch):
    from app.models import QueryResult
    from app.sql_compiler import CompiledQuery
    directory, manifest, executor = setup
    calls = []
    execute = executor.execute_plan
    def fail_once(s,pid,trace=None,**kwargs):
        p = s.registered_plans[pid]
        calls.append(p.diagnosis if p.intent=='diagnose' else p.intent)
        if len(calls)==3:
            s.registered_plans.pop(pid)
            return QueryResult(query_id='failed',status='timeout',error='injected'),CompiledQuery('SELECT 1',())
        return execute(s,pid,trace,**kwargs)
    monkeypatch.setattr(executor,'execute_plan',fail_once)
    agent = AnalysisAgent(Scripted(plan_for(manifest,output_mode='seller_audit',top_n=3)),{'r1':executor})
    session = Session('r1')
    first = agent.respond(session,'audit')
    assert [t['status'] for t in session.workflow_tasks]==['completed','completed','failed','completed']
    assert '尚未完成' in first.text
    ids = [t['evidence']['record_id'] for t in session.workflow_tasks]
    second = agent.respond(session,'/continue')
    assert calls == ['rank','order_candidate','order_audit','details','order_audit']
    assert all(t['status']=='completed' for t in session.workflow_tasks)
    assert [session.workflow_tasks[i]['evidence']['record_id'] for i in [0,1,3]] == [ids[i] for i in [0,1,3]]


def test_all_contribution_groups_do_not_use_top_n(setup):
    directory, manifest, _ = setup
    _, result = direct(directory,manifest,intent='contribution',metric_ids=['sales'],dimension='category',limit_mode='all',top_n=1,
        comparison_range={'start':'2027-03-01','end':'2027-04-01'})
    assert len(result.rows)==2


def test_prior_period_issue_followup_uses_selected_order(setup):
    directory, manifest, executor = setup
    comparison = plan_for(manifest, intent='compare', comparison_range={'start':'2027-03-01','end':'2027-04-01'})
    followup = plan_for(manifest, intent='update', scope_source='view', updates=[
        {'operation':'set','field':'intent','values':['aggregate']},
        {'operation':'set','field':'metric_ids','values':['sales']}])
    store = SessionStore(directory / 'sessions.sqlite3')
    agent = AnalysisAgent(Scripted(comparison, followup), {'r1':executor}, store)
    session = Session('r1')
    original = agent.respond(session, 'compare March and April')
    agent.quality_details(session, specified=True)
    sources = agent.quality_sources(session, 'M')
    restored = store.load(session.session_id, manifest)
    assert restored.analysis_anchor == original.evidence['record_id']
    assert restored.working_plan == comparison
    assert sources['type'] == 'source_records'
    turn = agent.respond(restored, 'show sales for this order')
    assert turn.evidence['plan']['selected_order_id'] == 'M'
    assert turn.evidence['result']['status'] == 'ok', turn.text
    assert Decimal(turn.evidence['result']['rows'][0][0]) == Decimal('40.00')


def test_interrupted_quality_workflow_checkpoints_keep_comparison_anchor(setup, monkeypatch):
    directory, manifest, executor = setup
    comparison = plan_for(manifest, intent='compare', comparison_range={'start':'2027-03-01','end':'2027-04-01'})
    quality = plan_for(manifest, intent='update', scope_source='analysis', output_mode='quality_check')
    store = SessionStore(directory / 'sessions.sqlite3')
    agent = AnalysisAgent(Scripted(comparison, quality), {'r1':executor}, store)
    session = Session('r1')
    first = agent.respond(session, 'compare')
    execute = executor.execute_plan
    def interrupt(s, pid, trace=None, **kwargs):
        if s.registered_plans[pid].diagnosis == 'quality_rows':
            raise RuntimeError('simulated interruption')
        return execute(s, pid, trace, **kwargs)
    monkeypatch.setattr(executor, 'execute_plan', interrupt)
    with pytest.raises(RuntimeError, match='simulated interruption'):
        agent.respond(session, 'check quality in both periods')
    restored = store.load(session.session_id, manifest)
    assert restored.analysis_anchor == first.evidence['record_id']
    assert restored.analysis_plan() == comparison
    summary_id = restored.workflow_tasks[0]['evidence']['record_id']
    monkeypatch.setattr(executor, 'execute_plan', execute)
    agent.respond(restored, '/continue')
    assert restored.workflow_tasks[0]['evidence']['record_id'] == summary_id
    assert all(t['status']=='completed' for t in restored.workflow_tasks)
    assert restored.analysis_anchor == first.evidence['record_id']


def test_quality_full_summary_and_export_exceed_preview(tmp_path):
    tables = source_tables(False)
    for i in range(505):
        order = {**tables['orders'][1],'order_id':f'Q{i:04}'}
        item = {**tables['order_items'][1],'order_id':order['order_id']}
        tables['orders'].append(order)
        tables['order_items'].append(item)
    manifest = import_tables('many',tables,[],'Many issues',tmp_path,quality_rule_version=ROUND2_RULE)
    executor = Executor('many',tmp_path)
    session = Session('many')
    details = plan_for(manifest,intent='diagnose',metric_ids=[],diagnosis='quality_rows')
    result, compiled = executor.execute_plan(session,executor.register(session,details))
    assert result.truncated and len(result.rows)==500
    from app.query_worker import run_query
    exported = run_query(executor.path,compiled,export_path=tmp_path/'all.csv')
    assert exported.row_count==506
    _, summary = direct(tmp_path,manifest,intent='diagnose',metric_ids=[],diagnosis='quality_summary')
    assert next(r for r in summary.rows if r[0]=='missing_payment')[2]==506
