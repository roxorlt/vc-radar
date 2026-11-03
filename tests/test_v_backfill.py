"""V16l #39：全部样例手写，网络、密钥和真实模型一律禁止。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, time
import json
from pathlib import Path
import sqlite3

import pytest

from vcradar import backfill, cli, config, db, fx, http, llm, outbox, pipeline, render, scan, site, verify
from vcradar.config import BJ, Config
from vcradar.models import DigestData, Extracted, LabelDim, RawItem
from vcradar.sources.registry import P0_SOURCES

NOW = datetime(2026, 10, 31, 14, 0, tzinfo=BJ)
APPLY = NOW + timedelta(days=1)
PUSH = NOW.replace(day=6, month=11)
FIRST = NOW.date().isoformat()
TRANSITION_NAMES = ['Bleu Robotics', 'Machenta', 'Composal', 'RouterPlus', 'GrayPass', 'Simantic',
                    'BioGeometry', 'FuriosaAI', 'Solink', 'i23', 'Circuit Mind', 'Clinics on Cloud',
                    'CureBay', 'North Vector Dynamics', 'Agora Labs', 'Lette', 'Matiks', 'Querio', 'Volare', '22one']


def encode(value):
    return json.dumps(value, ensure_ascii=False)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('V 测试禁止网络、密钥和真实模型')
    monkeypatch.setattr(config, 'load_api_key', forbidden)
    monkeypatch.setattr(http.urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(llm, 'urlopen', forbidden)


@pytest.fixture
def env(tmp_path):
    links = tmp_path / 'links.json'
    links.write_text('[]')
    cfg = Config(state_dir=tmp_path / 'state', test_mode=True, links_file=links,
                 base_url='https://radar.test/private')
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, P0_SOURCES)
    conn.commit()
    yield conn, cfg
    conn.close()


def add_event(conn, name, *, amount='$5M', quote=None, old=None, country='London',
              pending=False, filtered=('总部非美国',), baseline=False, round_text='Seed',
              summary='公司为团队提供协作软件。产品用于管理工作。', day=NOW, batch=None):
    serial = conn.execute('SELECT COUNT(*) FROM raw_item').fetchone()[0]
    quote = quote if quote is not None else f'{name} raised {amount or "an undisclosed amount"} in funding.'
    content = quote + (f' The company is headquartered in {country}.' if country else '')
    item = RawItem('tc_venture', str(serial), f'https://news.test/{serial}', name + ' financing',
                   day, excerpt=content, structured={'yc_batch': batch} if batch else {})
    ext = Extracted(company_name=name, website=f'https://{scan.name_key(name).replace(" ", "-")}.test/',
                    hq_text=country, round_text=round_text, amount_text=amount,
                    summary=summary, ai=LabelDim('否'), social=LabelDim('否'),
                    quotes={'amount_text': quote})
    raw, _ = scan.upsert_raw_item(conn, item, NOW.replace(hour=0, minute=35), baseline)
    eid = scan.ingest(conn, raw, item, ext, now=NOW.replace(hour=0, minute=35),
                      is_baseline=baseline, is_backfill=False, latest_yc_batches=[], model=None)
    conn.execute('UPDATE funding_event SET filter_reasons_json=?,push_date=?,last_pushed_date=?,pending=0 WHERE id=?',
                 (encode(list(filtered)), None if baseline else FIRST, None if baseline else FIRST, eid))
    if old is not None:
        conn.execute("UPDATE funding_event SET amount_text=?,amount_currency='USD',amount_original=?,amount_usd=?,"
                     "amount_status='披露',fx_rate=1,fx_date='2026-10-29',marks_json='[]' WHERE id=?", (f'${old}', old, old, eid))
    if pending:
        conn.execute("UPDATE company SET status='总部待核' WHERE id=(SELECT company_id FROM funding_event WHERE id=?)", (eid,))
    return eid


def fixed_rates(cfg, eur=.5):
    cfg.rates_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.rates_file.write_text(encode({'base':'USD', 'source':'手写汇率', 'days':{
        '2026-10-31': {'fetched_at':'2026-10-31 08:40:00', 'rates':{'EUR':eur, 'GBP':.4}}}}))


def manifest(env, **kwargs):
    conn, cfg = env
    conn.commit()
    rates = fx.rates_for_run(cfg, cli.parse_args(['--dry-run']), now=NOW)
    return backfill.create_manifest(conn, cfg, now=NOW, real_now=NOW, rates=rates, **kwargs)


def apply(env, data, *, now=APPLY, file=None, **kwargs):
    conn, cfg = env
    conn.commit()
    path = file or cfg.outbox_dir / ('backfill-verify-' + data['generated_at'].replace(':','').replace(' ','-') + '.json')
    if not path.exists():
        outbox.write_atomic(path, data)
    return backfill.apply_manifest(conn, cfg, data, path, now=now, real_now=now, **kwargs), path


def seed_digest(conn, ids):
    scan.upsert_digest(conn, {'date':FIRST, 'push':True, 'status':'基线', 'event_ids':ids,
                             'generated_at':'2026-10-31 00:35:00', 'text':'首期旧文本'}, first_issue=True)
    conn.commit()


def test_39_1_two_scopes_sort_before_search_and_limits(env):
    conn, _ = env
    for i in range(57):
        eid = add_event(conn, f'Candidate {i:02}', amount=f'${i+1}M', baseline=i >= 22)
        conn.execute("UPDATE company SET ai_label='待核' WHERE id=(SELECT company_id FROM funding_event WHERE id=?)", (eid,))
        # 让重新合并后的标签仍需要确认。
        source = conn.execute('SELECT extraction_json FROM event_source WHERE event_id=?', (eid,)).fetchone()
        ext = json.loads(source[0]); ext['ai'] = {'verdict':None,'quote':''}
        conn.execute('UPDATE event_source SET extraction_json=? WHERE event_id=?', (encode(ext),eid))
    for i in range(58):
        add_event(conn, f'Old pending {i:02}', country=None, pending=True, filtered=(), baseline=i >= 4)
    calls = []
    def verifier(c, **kw):
        calls.append((c['search_group'], c['name']))
        return {'searches':[{'requested_at':f'2026-10-31 14:00:0{i}', 'query':f'{c["name"]} {i}',
                             'status':200, 'result_count':0, 'billed':False, 'cost':0, 'results':[]} for i in range(3)],
                'model_calls':[], 'fields':[]}
    data = manifest(env, verifier=verifier)
    assert sum(c['rank'] is not None for c in data['candidates']) == 57
    assert sum(c['in_supplement'] for c in data['candidates']) == 10
    assert all(c['rank'] is None for c in data['candidates'] if c['original_status'] == '总部待核')
    assert sum(group == 'A' for group, _ in calls) == 58
    assert sum(group == 'B' for group, _ in calls) == 10
    assert len(data['searches']) == 204
    changed = manifest(env, verifier=lambda *a, **k: {'searches':[], 'fields':[], 'model_calls':[]})
    assert [(c['name'],c['rank']) for c in changed['candidates']] == [(c['name'],c['rank']) for c in data['candidates']]


def test_39_2_apply_once_before_age_check_and_transaction_rollback(env, capsys):
    conn, cfg = env
    ids = [add_event(conn, n, old=1) for n in ('First', 'Second')]
    data = manifest(env)
    file = cfg.outbox_dir / 'backfill-verify-once.json'
    outbox.write_atomic(file, data)
    conn.execute(f"CREATE TRIGGER fail_v BEFORE UPDATE ON funding_event WHEN NEW.id={ids[1]} BEGIN SELECT RAISE(ABORT,'手写故障'); END")
    conn.commit(); before = list(conn.iterdump())
    with pytest.raises(sqlite3.Error):
        apply(env, data, file=file)
    assert list(conn.iterdump()) == before
    assert 'applied_at' not in json.loads(file.read_text())
    assert not list(cfg.outbox_dir.glob('*-backfill*.json'))
    conn.execute('DROP TRIGGER fail_v'); conn.commit()
    apply(env, data, file=file)
    before = list(conn.iterdump())
    capsys.readouterr()
    args = cli.parse_args(['--backfill-first-issue-gap','--apply','--dry-run'])
    assert backfill.run_backfill(cfg, args, pipeline.Clock((NOW+timedelta(days=30)).date(),time(14))) == 0
    assert capsys.readouterr().out.strip() == '该清单已执行，没有新的补充'
    assert list(conn.iterdump()) == before


def test_39_3_keep_round_and_batch_filters_but_include_country_filtered_baseline(env):
    conn, _ = env
    baseline = add_event(conn, 'Baseline', baseline=True)
    late = add_event(conn, 'Late', round_text='Series B', filtered=('总部非美国','轮次不在范围'))
    data = manifest(env)
    assert next(c for c in data['candidates'] if c['event_id']==baseline)['in_supplement']
    assert not next(c for c in data['candidates'] if c['event_id']==late)['in_supplement']
    apply(env, data)
    assert json.loads(conn.execute('SELECT filter_reasons_json FROM funding_event WHERE id=?',(late,)).fetchone()[0]) == ['轮次不在范围']
    assert conn.execute('SELECT baseline_unpushed,push_date FROM funding_event WHERE id=?',(baseline,)).fetchone()[:] == (0,None)


def test_39_4_sort_ties_and_separate_ten_slots(env):
    conn, cfg = env
    for name in ['Zulu','bravo','Alpha', *[f'Extra {i}' for i in range(10)]]:
        add_event(conn, name, amount='$10M', day=NOW - timedelta(days=name == 'Alpha'))
    data = manifest(env)
    assert [c['name'] for c in data['candidates']][-3:] == ['Extra 9','Zulu','Alpha']
    assert data['candidates'][0]['name']=='bravo'
    apply(env, data)
    sections, ids = backfill.collect_supplement(conn, cfg, PUSH.date())
    assert len(sections[0].cards) == len(ids) == 10
    assert sections[0].overflow_count == 3
    assert [c.name for c in sections[0].cards] == [c['name'] for c in data['candidates'][:10]]


def test_39_5_and_6_counts_and_window_exclude_supplement(env):
    conn,cfg=env
    for i in range(12): add_event(conn,f'Extra {i:02}',amount=f'${i+1}M')
    seed_digest(conn,[r[0] for r in conn.execute('SELECT id FROM funding_event')])
    window = conn.execute('SELECT generated_at FROM digest').fetchone()[0]
    apply(env,manifest(env))
    sections,_=backfill.collect_supplement(conn,cfg,PUSH.date())
    text=render.render_push(DigestData(PUSH.date(),NOW,PUSH,'没有新增',extra_sections=sections),links_file=cfg.links_file)
    assert text.splitlines()[1]=='本周没有发现新的天使轮、种子轮、A 轮融资。'
    assert text.splitlines()[2] == '之前漏掉的公司（一次性补充）'
    assert '还有一些没有列出' in text and '另补充此前漏掉的' not in text
    assert conn.execute('SELECT generated_at FROM digest').fetchone()[0]==window
    assert pipeline._listing_for_issue(conn,PUSH.date())['event_ids']==[]


def test_39_7_push_once_and_repeat_same_day(env):
    conn,cfg=env
    add_event(conn,'Extra')
    apply(env,manifest(env))
    _,ids=backfill.collect_supplement(conn,cfg,PUSH.date())
    backfill.mark_supplement(conn,ids,PUSH.date()); conn.commit()
    assert backfill.collect_supplement(conn,cfg,PUSH.date()+timedelta(days=7))==([],[])
    assert backfill.collect_supplement(conn,cfg,PUSH.date(),event_ids=ids)[1]==ids
    assert conn.execute('SELECT backfill,backfill_push_date FROM company').fetchone()[:]==('首期漏掉',str(PUSH.date()))


def test_39_8_zero_candidates_no_section(env):
    conn,cfg=env
    data=manifest(env)
    path,_=apply(env,data)
    assert path is None and backfill.collect_supplement(conn,cfg,PUSH.date())==([],[])
    text=render.render_push(DigestData(PUSH.date(),NOW,PUSH,'没有新增'),links_file=cfg.links_file)
    assert '另补充' not in text and '一次性补充' not in text


def test_39_9_apply_failure_does_not_block_normal_push(env,tmp_path,monkeypatch):
    conn,cfg=env
    add_event(conn,'Extra')
    data=manifest(env)
    writer=outbox.write_atomic
    def fail_record(path,payload):
        if payload.get('kind')=='backfill': raise OSError('手写写入失败')
        writer(path,payload)
    monkeypatch.setattr(outbox,'write_atomic',fail_record)
    with pytest.raises(OSError): apply(env,data)
    conn.execute('UPDATE source SET enabled=0');conn.commit()
    fixture=tmp_path/'fixture';fixture.mkdir();(fixture/'manifest.json').write_text('{}')
    args=cli.parse_args(['--fixture',str(fixture),'--date',str(PUSH.date()),'--no-deep'])
    assert pipeline.run(cfg,args,pipeline.Clock(PUSH.date(),time(8,40)))==0
    output=json.loads(next(cfg.outbox_dir.glob('2026-11-06-??????.json')).read_text())
    assert output['push'] and '一次性补充' not in output['text']


def test_39_11_skip_changed_top_and_k_without_replacement(env,capsys):
    conn,cfg=env
    for i in range(12):add_event(conn,f'Extra {i:02}',amount=f'${20-i}M',old=1)
    data=manifest(env)
    changed=[data['candidates'][i] for i in (0,1,10)]
    conn.execute('UPDATE funding_event SET push_date=? WHERE id IN (?,?)',('2026-11-01',changed[0]['event_id'],changed[2]['event_id']))
    conn.execute("INSERT INTO funding_event(company_id,round_norm,event_date,first_seen,first_ingested_at) VALUES (?,'A 轮','2026-10-31',?,?)",
                 (changed[1]['company_id'],'2026-10-31 14:00:01','2026-10-31 14:00:01'))
    conn.commit()
    before={c['company_id']:dict(conn.execute('SELECT * FROM company WHERE id=?',(c['company_id'],)).fetchone()) for c in changed}
    record,_=apply(env,data)
    assert json.loads(record.read_text())['count']==8
    assert [c['rank'] for c in json.loads(record.read_text())['companies']]==[3,4,5,6,7,8,9,10,12]
    assert '共跳过 3 家' in capsys.readouterr().out
    assert '共跳过 3 家' in next(cfg.log_dir.glob('*-backfill.log')).read_text()
    for cid,row in before.items():
        assert dict(conn.execute('SELECT * FROM company WHERE id=?',(cid,)).fetchone())==row
        assert conn.execute('SELECT COUNT(*) FROM verify_log WHERE company_id=?',(cid,)).fetchone()[0]==0
    sections,_=backfill.collect_supplement(conn,cfg,PUSH.date())
    assert len(sections[0].cards)==8 and sections[0].overflow_count==1


def test_39_12_amounts_are_per_event_frozen_and_null_when_unresolved(env,monkeypatch,tmp_path):
    conn,cfg=env
    fixed_rates(cfg)
    bio=add_event(conn,'BioGeometry',amount='$100',quote='BioGeometry raised 1 亿美元 in Series A.',old=100,round_text='Series A')
    agent=add_event(conn,'Agent37',amount='$0.37',quote='Agent37 raised $0.37 million in seed funding.',old=.37)
    missing=add_event(conn,'Missing',amount='$1',quote='The financing amount was not disclosed.',old=1)
    euro=add_event(conn,'Euro',amount='€11M',quote='Euro raised €11 million in Series A.',old=11,round_text='Series A')
    large=add_event(conn,'Large',amount='$50',quote='Large raised $50 million in seed funding.',old=50)
    sava=add_event(conn,'Sava',amount='$2',quote='Sava raised $2 million in seed funding.',old=2,country=None,pending=True,filtered=())
    sava2=add_event(conn,'Sava',amount='$8',quote='Sava raised $8 million in Series A.',old=8,country=None,pending=True,filtered=(),round_text='Series A')
    outside=add_event(conn,'Outside',amount='$5',quote='Outside raised $5 million.',old=5,filtered=())
    data=manifest(env)
    fixes=data['amount_fix_candidate']+data['amount_fix_noncandidate']
    assert len(fixes)==7 and len({f['event_id'] for f in fixes})==7
    assert {f['event_id'] for f in data['amount_fix_noncandidate']}=={sava,sava2}
    by_id={f['event_id']:f for f in fixes}
    assert by_id[bio]['new']==dict(currency='USD',orig_amount=100e6,usd_amount=100e6,rate_date=None,amount_status='披露',large=True)
    assert by_id[euro]['new']['usd_amount']==22e6 and by_id[euro]['new']['rate_date']=='2026-10-31'
    assert by_id[large]['new']['large'] is True
    assert by_id[missing]['new']==dict(currency=None,orig_amount=None,usd_amount=None,rate_date=None,amount_status='金额待确认',large=False)
    ranked=[c for c in data['candidates'] if c['rank']]
    assert [c['event_id'] for c in ranked]==[bio,large,euro,agent,missing]
    assert ranked[-1]['rank']>10 and not ranked[-1]['in_supplement']
    assert data['out_of_scope_amount_suspects'][0]['event_id']==outside
    csv,md=backfill.write_reports(data,tmp_path/'reports')
    for title in ['金额更正（候选）','金额更正（非候选）','范围外疑似金额错误']:
        assert title in csv.read_text() and title in md.read_text()
    fixed_rates(cfg,.9)
    def forbidden(*a,**k): raise AssertionError('apply 不应重新解析或换算')
    monkeypatch.setattr(fx,'convert_amount',forbidden)
    monkeypatch.setattr(fx,'parse_amount',forbidden)
    apply(env,data)
    for fix in fixes:
        row=conn.execute('SELECT * FROM funding_event WHERE id=?',(fix['event_id'],)).fetchone()
        assert backfill._amount_values(row)==fix['new']
        log=conn.execute("SELECT old_new_json FROM verify_log WHERE field='金额' AND event_id=?",(row['id'],)).fetchone()
        assert json.loads(log[0])=={'old':fix['old'],'new':fix['new']}
    assert conn.execute('SELECT push_date FROM funding_event WHERE id=?',(sava,)).fetchone()[0]==FIRST
    assert conn.execute('SELECT backfill FROM company WHERE name=?',('Sava',)).fetchone()[0]==''
    assert conn.execute('SELECT amount_usd FROM funding_event WHERE id=?',(outside,)).fetchone()[0]==5
    assert conn.execute('SELECT amount_text FROM funding_event WHERE id=?',(missing,)).fetchone()[0] is None
    snap=site._snapshot(conn,cfg,APPLY)
    assert next(e for e in snap['events'] if e['id']==missing)['amount']=='金额待确认'
    assert next(e for e in snap['events'] if e['id']==sava)['amount']=='200 万美元'


def test_39_12_conflicting_quotes_remain_amount_pending(env):
    conn,_=env
    eid=add_event(conn,'Conflict',quote='Conflict raised $10 million.',old=1)
    add_event(conn,'Conflict',quote='Conflict raised $12 million.',old=1)
    data=manifest(env)
    assert data['amount_fix_candidate'][0]['event_id']==eid
    assert data['amount_fix_candidate'][0]['new']['amount_status']=='金额待确认'
    assert not data['candidates'][0]['in_supplement']


def test_39_12_multiple_candidate_events_stop_before_search_or_output(env):
    conn,cfg=env
    add_event(conn,'Duplicate',round_text='Seed')
    add_event(conn,'Duplicate',round_text='Series A')
    with pytest.raises(ValueError,match='一家公司有多条补充候选事件：Duplicate'):
        manifest(env,verifier=lambda *a,**k:pytest.fail('不应搜索'))
    assert not cfg.outbox_dir.exists()


def test_39_13_summary_retry_fallback_and_apply_without_model(env,monkeypatch):
    conn,cfg=env
    good=add_event(conn,'Good',amount='$90M',summary='')
    bad=add_event(conn,'Bad',amount='$80M',summary='')
    for i in range(10):add_event(conn,f'Other {i:02}',amount=f'${i+1}M',summary='' if i==0 else '公司提供协作软件。')
    summary='公司为企业团队提供项目协作软件，帮助员工集中整理工作资料和沟通记录。产品通过共享工作台管理任务进展，让不同岗位的成员查阅信息。客户可以按照自己的工作流程安排协作事项，并在团队内部共享最新结果。'
    client=llm.MockLLMClient([{'summary':summary},{'error':'timeout'},{'error':'timeout'}])
    data=manifest(env,summary_client=client)
    assert client.calls==3
    top={c['event_id']:c for c in data['candidates'] if c['in_supplement']}
    assert top[good]['summary']==summary and 80<=extract_len(summary)<=140
    assert top[bad]['summary'] is None
    assert all('summary' not in c for c in data['candidates'] if not c['in_supplement'])
    monkeypatch.setattr(client,'complete_json',lambda *a,**k:pytest.fail('apply 不能调模型'))
    apply(env,data)
    sections,_=backfill.collect_supplement(conn,cfg,PUSH.date())
    text=render.render_push(DigestData(PUSH.date(),NOW,PUSH,'没有新增',extra_sections=sections),links_file=cfg.links_file)
    bad_line=next(line for line in text.splitlines() if '. Bad' in line)
    assert bad_line.count('｜')==2 and '｜｜' not in bad_line
    assert conn.execute('SELECT summary FROM company WHERE name=?',('Good',)).fetchone()[0]==summary


def extract_len(text):
    return len(''.join(text.split()))


def test_manifest_generated_at_uses_real_time_not_simulated_now(env):
    conn,cfg=env
    add_event(conn,'Real stamp');conn.commit()
    rates=fx.rates_for_run(cfg,cli.parse_args(['--dry-run']),now=NOW)
    before=datetime.now(BJ).replace(microsecond=0)
    data=backfill.create_manifest(conn,cfg,now=NOW-timedelta(days=100),rates=rates)
    assert before<=backfill._time(data['generated_at'])<=datetime.now(BJ)


def test_v11_migration_keeps_old_verification_rows_and_adds_old_new_column(tmp_path):
    path=tmp_path/'old.db';conn=db.connect(path)
    conn.execute("INSERT INTO company(id,name,name_key,first_seen) VALUES ('old','Old','old','2026-10-01')")
    conn.execute("INSERT INTO verify_log(company_id,run_date,run_started_at,field,result,verified_at) VALUES ('old','2026-10-01','2026-10-01 08:40:00','金额','已确认','2026-10-01 08:40:00')")
    conn.execute('ALTER TABLE verify_log DROP COLUMN old_new_json')
    conn.execute('ALTER TABLE verify_log DROP COLUMN search_date')
    conn.execute('ALTER TABLE verify_search DROP COLUMN search_date')
    conn.execute('ALTER TABLE verify_search DROP COLUMN run_started_at')
    conn.execute('ALTER TABLE company DROP COLUMN backfill_rank')
    conn.commit();conn.close()
    for _ in range(2):
        conn=db.connect(path)
        row=conn.execute('SELECT * FROM verify_log').fetchone()
        assert row['field']=='金额' and row['old_new_json']=='null' and row['search_date']=='2026-10-01'
        assert conn.execute('SELECT backfill_rank FROM company').fetchone()[0] is None
        assert conn.execute('SELECT value FROM meta WHERE key=?',('schema_version',)).fetchone()[0]==str(db.SCHEMA_VERSION)
        conn.close()


def test_39_2_applied_file_staging_failure_rolls_back(env,monkeypatch):
    conn,cfg=env
    add_event(conn,'File failure')
    data=manifest(env)
    writer=outbox.write_atomic
    def fail_applied(path,payload):
        if payload.get('applied_at'):raise OSError('手写 applied 文件失败')
        return writer(path,payload)
    monkeypatch.setattr(outbox,'write_atomic',fail_applied)
    before=list(conn.iterdump())
    with pytest.raises(OSError):apply(env,data)
    assert list(conn.iterdump())==before
    assert not list(cfg.outbox_dir.glob('*-backfill*.json'))
    assert 'applied_at' not in json.loads(next(cfg.outbox_dir.glob('backfill-verify-*.json')).read_text())


def test_39_1_production_spend_log_appends_even_when_manifest_is_overwritten(env, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(backfill, 'datetime', FixedDatetime)
    conn,test_cfg=env
    add_event(conn,'Spend',country=None)
    conn.commit()
    cfg=replace(test_cfg,test_mode=False)
    db.backup(conn,cfg.db_path)
    def verifier(c,**kw):
        return {'searches':[{'requested_at':'2026-10-31 14:00:01','query':'Spend funding','status':200,
            'results':[],'result_count':1,'billed':True,'cost':.005}],
            'model_calls':[{'cost':.0035}], 'fields':[{'field':'国家','result':'本次未查','reason':'搜索失败','verified_at':'2026-10-31 14:00:02'}]}
    args=cli.parse_args(['--backfill-first-issue-gap'])
    clock=pipeline.Clock(NOW.date(),NOW.time())
    for _ in range(2):assert backfill.run_backfill(cfg,args,clock,verifier=verifier)==0
    log=(cfg.outbox_dir/'backfill-spend.log').read_text().splitlines()
    assert len(log)==2 and all(line.endswith('\t1\t0.0085') for line in log)
    assert len(list(cfg.outbox_dir.glob('backfill-verify-*.json')))==1
    prod=db.connect(cfg.db_path)
    try:
        stats=site._verification(prod,NOW,cfg)[0]
        file,data=backfill.latest_manifest(cfg,now=APPLY)
        balance=lambda:{'is_available':True,'balance_infos':[{'currency':'CNY','total_balance':'100'}]}
        backfill.apply_manifest(prod,cfg,data,file,now=APPLY,real_now=APPLY,balance_client=balance)
        assert site._verification(prod,NOW,cfg)[0]==stats
        assert stats['backfill_cost']==pytest.approx(.017)
        assert prod.execute('SELECT run_date,search_date,run_started_at FROM verify_search').fetchone()[:]==(str(APPLY.date()),str(NOW.date()),backfill._stamp(APPLY))
        assert prod.execute("SELECT verified_at FROM verify_log WHERE field='国家'").fetchone()[0]=='2026-10-31 14:00:02'
    finally:prod.close()


@pytest.mark.parametrize("short_quote", [None, "$1"])
def test_39_12_saved_source_without_amount_does_not_reuse_old_wrong_number(env,short_quote):
    conn,_=env
    eid=add_event(conn,'No numeric source',old=1)
    source=conn.execute('SELECT raw_item_id,extraction_json FROM event_source WHERE event_id=?',(eid,)).fetchone()
    ext=json.loads(source['extraction_json']);ext['quotes']={'amount_text':short_quote} if short_quote else {}
    conn.execute('UPDATE event_source SET extraction_json=? WHERE event_id=?',(encode(ext),eid))
    conn.execute("UPDATE raw_item SET title='A company report',excerpt='The business provides workflow software.' WHERE id=?",(source['raw_item_id'],))
    data=manifest(env)
    assert data['amount_fix_candidate'][0]['new']['amount_status']=='金额待确认'
    assert data['amount_fix_candidate'][0]['new']['orig_amount'] is None


def test_39_12_missing_all_sources_can_fall_back_to_legacy_amount_text(env):
    conn,_=env
    eid=add_event(conn,'No saved source',amount='$2 million',old=2)
    conn.execute('DELETE FROM event_source WHERE event_id=?',(eid,))
    conn.execute('UPDATE funding_event SET amount_text=? WHERE id=?',('$2 million',eid))
    data=manifest(env)
    assert data['amount_fix_candidate'][0]['new']['usd_amount']==2e6


def test_39_2_committed_apply_can_repair_final_manifest_rename(env,monkeypatch):
    conn,cfg=env
    add_event(conn,'Rename recovery')
    data=manifest(env)
    rename=Path.replace
    def refuse(src,dst):
        if src.name.endswith('.applied.tmp'):raise OSError('手写最后改名失败')
        return rename(src,dst)
    monkeypatch.setattr(Path,'replace',refuse)
    with pytest.raises(OSError):apply(env,data)
    before=list(conn.iterdump())
    assert conn.execute('SELECT backfill FROM company').fetchone()[0]=='补充未推送'
    monkeypatch.setattr(Path,'replace',rename)
    apply(env,data)
    assert list(conn.iterdump())==before
    assert json.loads(next(cfg.outbox_dir.glob('backfill-verify-*.json')).read_text())['applied_at']
