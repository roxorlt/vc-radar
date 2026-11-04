"""V16l #36～#38：城市、测试标志、运行配置和四种汇率组合。"""
from dataclasses import replace
from datetime import datetime, time, timedelta
import json
from pathlib import Path

import pytest

from test_v_backfill import env, offline, add_event, manifest, apply, NOW, fixed_rates, PUSH
from vcradar import backfill, cli, config, db, fx, llm, outbox, pipeline, render, scan, site
from vcradar.config import BJ, Config
from vcradar.models import Card, CardSource, DigestData, Extracted, RawItem


@pytest.mark.parametrize('location,expected',[
    ('London','英国'),('London, Ontario, Canada','加拿大'),('London, Ontario','英国'),
    ('Paris','法国'),('Paris, Texas','美国'),('Vancouver','加拿大'),('Vancouver, WA','美国'),
    ('Vancouver, Washington','美国'),('Vancouver, WA, Canada','加拿大'),
    ('Portland, Oregon','美国'),('Portland, OR','美国'),('Portland, Maine','美国'),('Portland, ME','美国'),('Portland','国家待确认'),
    ('Birmingham, England','英国'),('Birmingham, UK','英国'),('Birmingham, Alabama','美国'),('Birmingham, AL','美国'),('Birmingham','国家待确认'),
    ('Georgia','国家待确认'),('Tbilisi, Georgia','格鲁吉亚'),('country of Georgia','格鲁吉亚'),
    ('Tbilisi','格鲁吉亚'),('Tbilisi, Canada','加拿大'),('country of Georgia, GA','格鲁吉亚'),
    ('Atlanta, Georgia','美国'),('Atlanta, GA','美国'),('Georgia, GA','美国'),
])
def test_36_fixed_city_positive_and_ambiguity(location,expected):
    assert scan.country_from_hq(location,'')==expected
    item=RawItem('tc_venture','city','https://news.test/city','City news',NOW,
                 excerpt='The company is headquartered in '+location+'.')
    assert scan.judge_country(item,Extracted(company_name='City',hq_text=location))[0]==expected


@pytest.mark.parametrize('city,context,expected',[
    ('Vancouver','Vancouver-based company operates in Washington.','美国'),
    ('Portland','Portland-based company operates in Oregon.','美国'),
    ('Birmingham','Birmingham-based company operates in England.','英国'),
    ('Georgia','Georgia-based company is in Tbilisi.','格鲁吉亚'),
])
def test_36_conditional_terms_must_be_in_same_sentence(city,context,expected):
    item=RawItem('tc_venture','context','https://news.test/context','Company news',NOW,excerpt=context)
    assert scan.judge_country(item,Extracted(company_name='Company',hq_text=city))[0]==expected
    unrelated=RawItem('tc_venture','other','https://news.test/other','Company news',NOW,
                      excerpt=city+'-based company. '+context.split(' in ')[-1])
    assert scan.judge_country(unrelated,Extracted(company_name='Company',hq_text=city))[0] == ('加拿大' if city=='Vancouver' else '国家待确认')


@pytest.mark.parametrize('flags',[
    ['--fixture','unused'],['--dry-run'],['--search-fixtures','unused'],['--rates-file','unused'],
    ['--rates-fail'],['--now','10:30'],['--links-file','unused'],['--dry-run','--assume-review-site-passed'],
    ['--replay','unused'],['--to','test'],['--no-verify'],['--prod-rules'],['--reset-test-db'],['--test'],
])
def test_37_all_a_flags_use_one_isolation_rule(flags):
    args=cli.parse_args(flags)
    assert args.test_flags and config.is_test_run(args)
    assert config.is_test_run(args,Config(test_mode=False))


@pytest.mark.parametrize('flags',[
    ['--date','2026-10-31'],['--resend'],['--sync-sent'],['--first-issue'],['--deep-only'],
    ['--deep-only','--company','aster'],['--no-deep'],['--deep-deadline','10:00'],['--deep-final','11:00'],
    ['--total-timeout','15'],['--live-model'],['--backfill-first-issue-gap'],['--backfill-first-issue-gap','--apply'],
])
def test_37_b_flags_keep_production_location(flags):
    args=cli.parse_args(flags)
    assert not args.test_flags and not config.is_test_run(args)
    assert config.is_test_run(cli.parse_args(flags+['--no-verify']))


def test_38_runtime_json_and_assume_override_never_modify_file(tmp_path,monkeypatch):
    runtime=tmp_path/'runtime.json'
    toml=tmp_path/'config.toml'
    toml.write_text(f'state_dir={json.dumps(str(tmp_path/"state"))}\nruntime_file={json.dumps(str(runtime))}\nbase_url="https://radar.test/private"\nreview_site_passed=true\n')
    assert config.load_config(toml).review_site_passed is False
    runtime.write_text('{"review_site_passed":true}')
    assert config.load_config(toml).review_site_passed is True
    runtime.write_text('{"review_site_passed":false}')
    before=runtime.read_bytes();seen=[]
    monkeypatch.setattr(pipeline,'run',lambda cfg,*rest:seen.append(cfg) or 0)
    assert cli.main(['--config',str(toml),'--assume-review-site-passed','--no-verify'])==0
    assert seen[0].review_site_passed and seen[0].test_mode
    assert runtime.read_bytes()==before
    with pytest.raises(SystemExit) as exc:cli.main(['--config',str(toml),'--assume-review-site-passed'])
    assert exc.value.code==2 and not (tmp_path/'state').exists()


@pytest.mark.parametrize('cache_present,file_present,fail,expected',[
    (False,False,False,None),(True,False,False,20e6),(True,False,True,20e6),(False,False,True,None),
    (False,True,False,40e6),(True,True,False,40e6),(False,True,True,40e6),(True,True,True,40e6),
])
def test_37_four_test_rate_combinations_are_read_only(env,tmp_path,cache_present,file_present,fail,expected):
    _,cfg=env
    # 用固定返回覆盖所有路径，真实汇率请求应为零，包括注入 fetcher 的调用。
    if cache_present:fixed_rates(cfg,.5)
    prod=cfg.state_dir/'rates.json';prod.write_text('production sentinel')
    fixed=tmp_path/'fixed.json'
    rates={'base':'USD','source':'固定文件','days':{'2026-10-30':{'fetched_at':'2026-10-30 08:41:00','rates':{'EUR':.25}}}}
    fixed.write_text(json.dumps(rates))
    args=cli.parse_args((['--rates-file',str(fixed)] if file_present else ['--fixture','unused'])+(['--rates-fail'] if fail else []))
    before={p:p.read_bytes() for p in [prod,fixed,cfg.rates_file] if p.exists()}
    def forbidden(*a,**k):pytest.fail('测试不能请求汇率')
    session=fx.rates_for_run(cfg,args,now=NOW,fetcher=forbidden)
    snapshot=session.snapshot(NOW.date(),('EUR',))
    result=fx.convert_amount('€10M',snapshot)
    assert result.amount_usd==expected
    if expected is None:assert '暂未换算成美元' in fx.format_amount(result)
    if fail and expected is not None:
        assert result.fx_stale==1 and '汇率没更新' in session.status['message']
    assert {p:p.read_bytes() for p in before}==before
    assert not session.requested if fail else session.requested=={fx.DAILY_URL}


def test_37_announcement_then_fetch_day_then_latest_cache(env):
    _,cfg=env
    fixed_rates(cfg,.5)
    cfg.rates_file.write_text(json.dumps({'base':'USD','source':'手写汇率','days':{
        '2026-10-29':{'fetched_at':'2026-10-29 08:40:00','rates':{'EUR':.9}},
        '2026-10-31':{'fetched_at':'2026-10-31 08:40:00','rates':{'EUR':.5}}}}))
    rates=fx.rates_for_run(cfg,cli.parse_args(['--dry-run']),now=NOW)
    assert rates.snapshot(NOW.date()-timedelta(days=2),('EUR',))['date']=='2026-10-29'
    assert rates.snapshot(NOW.date()-timedelta(days=1),('EUR',))['date']=='2026-10-31'
    missing_fetch_day=fx.rates_for_run(cfg,cli.parse_args(['--dry-run']),now=NOW+timedelta(days=1))
    assert missing_fetch_day.snapshot(NOW.date()-timedelta(days=1),('EUR',))['date']=='2026-10-31'


def test_37_replay_fixed_rates_override_rates_file(env,tmp_path,monkeypatch,capsys):
    _,cfg=env
    fixed_rates(cfg,.5)
    external=tmp_path/'external.json';external.write_text(cfg.rates_file.read_text())
    payload={'date':FIRST_DATE,'固定汇率':{'base':'USD','source':'回放固定汇率','days':{
        FIRST_DATE:{'fetched_at':FIRST_DATE+' 08:40:00','rates':{'EUR':.25}}}},
        'items':[{'source_id':'tc_venture','external_id':'replay','url':'https://news.test/replay',
                  'title':'Replay raised €10M in seed funding','published_at':FIRST_DATE+'T08:00:00+08:00','excerpt':'The company is based in London.'}]}
    replay=tmp_path/'replay.json';replay.write_text(json.dumps(payload))
    monkeypatch.setattr(cli,'load_config',lambda *a,**k:cfg)
    assert cli.main(['--replay',str(replay),'--rates-file',str(external),'--no-deep'])==0
    assert '约 4000 万美元' in capsys.readouterr().out
    assert json.loads(cfg.rates_file.read_text())['days'][FIRST_DATE]['rates']['EUR']==.5


FIRST_DATE='2026-10-31'


def test_37_reset_clears_test_rates_and_keeps_production(env):
    conn,cfg=env;conn.close()
    fixed_rates(cfg)
    prod=cfg.state_dir/'rates.json';prod.write_text('production sentinel')
    pipeline.reset_test_db(cfg)
    assert not cfg.rates_file.exists() and prod.read_text()=='production sentinel'


def test_37_amount_pending_sorts_after_undisclosed_and_never_large(env):
    conn,cfg=env
    pending=add_event(conn,'Pending amount',amount='$1',quote='The amount was not disclosed.',old=1)
    apply(env,manifest(env))
    row=dict(conn.execute('SELECT * FROM funding_event WHERE id=?',(pending,)).fetchone())
    assert row['amount_status']=='金额待确认' and scan.amount_label(conn,row)=='金额待确认'
    assert scan._event_marks(['大额'],'种子轮','金额待确认',1e9)==[]
    assert scan._sort_data(row,[])[0] is None
    base=dict(company_id='c',name='C',marks=[],round_label='种子轮',amount_label='金额未披露',lead_label='',tag_label='',group='',summary='',sources=[])
    cards=[Card(**{**base,'company_id':'p','name':'A','amount_status':'金额待确认','amount_label':'金额待确认'}),Card(**{**base,'company_id':'u'})]
    assert [c.company_id for c in render.sort_cards(cards)]==['u','p']


def test_38_official_links_source_and_review_gate(env):
    conn,cfg=env
    eid=add_event(conn,'Terse',filtered=())
    cfg.links_file.write_text(json.dumps([
        {'type':'公司','company':'Terse','verified':False,'url':'https://wrong.test'},
        {'type':'公司','company':' TERSE ','verified':True,'url':'https://terse.test'},
        {'type':'公司','company':'Terse AI','verified':True,'url':'https://different.test'}]))
    cards=scan.collect_listing(conn,NOW.date(),weekly=True,event_ids=[eid])['cards']
    d=DigestData(NOW.date(),None,NOW,'有新增',cards=cards,base_url=cfg.base_url,yc_new_batch=('Fall 2026',31))
    text=render.render_push(d,links_file=cfg.links_file)
    assert '[官网](https://terse.test)' in text and '[来源](https://news.test/0)' in text
    assert '见网页' not in text and '/#' not in text
    assert '/#week' in render.render_push(replace(d,review_site_passed=True),links_file=cfg.links_file)


@pytest.mark.parametrize('after,exceeds',[('99.995',False),('99',True)])
def test_39_balance_reconciliation_reports_after_commit(env,after,exceeds):
    conn,test_cfg=env
    add_event(conn,'Balance')
    data=manifest(env);data['mode']='生产'
    data['rejudge'][0]['summary_model_calls']=[{'kind':'summary','cost':.01}]
    cfg=replace(test_cfg,test_mode=False)
    replies=iter(['100',after]);calls=[]
    def balance():
        calls.append(1)
        return {'is_available':True,'balance_infos':[{'currency':'CNY','total_balance':next(replies)}]}
    file=cfg.outbox_dir/'backfill-verify-balance.json';outbox.write_atomic(file,data)
    if exceeds:
        with pytest.raises(ValueError,match='业务写库已提交'):
            backfill.apply_manifest(conn,cfg,data,file,now=NOW,real_now=NOW,balance_client=balance)
    else:backfill.apply_manifest(conn,cfg,data,file,now=NOW,real_now=NOW,balance_client=balance)
    assert len(calls)==2 and json.loads(file.read_text())['applied_at']
    assert conn.execute('SELECT backfill FROM company').fetchone()[0]=='补充未推送'
    fee=conn.execute("SELECT reason FROM verify_log WHERE field='费用' AND source='补充命令'").fetchone()[0]
    assert '清单估算 0.01 元' in fee


def test_39_test_apply_never_checks_balance(env):
    conn,cfg=env;add_event(conn,'No balance')
    apply(env,manifest(env),balance_client=lambda:pytest.fail('测试不查余额'))
    assert conn.execute("SELECT COUNT(*) FROM verify_log WHERE field='费用'").fetchone()[0]==0


def test_39_production_refuses_an_already_applied_test_manifest(env,capsys):
    _,test_cfg=env
    cfg=replace(test_cfg,test_mode=False)
    file=cfg.outbox_dir/'backfill-verify-applied-test.json'
    outbox.write_atomic(file,{'generated_at':'2026-10-31 14:00:00','mode':'测试',
                             'applied_at':'2026-10-31 14:01:00','candidates':[]})
    args=cli.parse_args(['--backfill-first-issue-gap','--apply'])
    assert backfill.run_backfill(cfg,args,pipeline.Clock(NOW.date(),NOW.time()))==1
    assert '生产不能使用测试清单' in capsys.readouterr().err
    assert not cfg.db_path.exists()


def test_39_balance_mismatch_returns_nonzero_after_business_commit(env,capsys):
    conn,test_cfg=env
    add_event(conn,'Balance exit')
    data=manifest(env);data['mode']='生产'
    data['rejudge'][0]['summary_model_calls']=[{'kind':'summary','cost':.01}]
    cfg=replace(test_cfg,test_mode=False)
    db.backup(conn,cfg.db_path)
    file=cfg.outbox_dir/'backfill-verify-balance-exit.json';outbox.write_atomic(file,data)
    replies=iter(['100','99'])
    def balance():return {'is_available':True,'balance_infos':[{'currency':'CNY','total_balance':next(replies)}]}
    args=cli.parse_args(['--backfill-first-issue-gap','--apply'])
    assert backfill.run_backfill(cfg,args,pipeline.Clock(NOW.date(),NOW.time()),balance_client=balance)==1
    assert '余额差 1 元，清单估算 0.01 元' in capsys.readouterr().err
    check=db.connect(cfg.db_path)
    try:assert check.execute('SELECT backfill FROM company').fetchone()[0]=='补充未推送'
    finally:check.close()
    assert json.loads(file.read_text())['applied_at']


def test_39_historical_test_date_uses_real_generated_at_without_extra_rejection(env):
    conn,cfg=env
    add_event(conn,'Historical test')
    data=manifest(env)
    file=cfg.outbox_dir/'backfill-verify-historical.json';outbox.write_atomic(file,data)
    day=NOW.date()-timedelta(days=1)
    args=cli.parse_args(['--backfill-first-issue-gap','--apply','--dry-run','--date',str(day)])
    assert backfill.run_backfill(cfg,args,pipeline.Clock(day,NOW.time()))==0
    assert json.loads(file.read_text())['applied_at']
    assert conn.execute('SELECT first_ingested_at FROM funding_event').fetchone()[0].startswith(str(day))
