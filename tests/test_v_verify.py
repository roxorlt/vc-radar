"""V16l #40：查证范围、费用边界及新状态与同名事实的回归。"""
from datetime import timedelta
import hashlib
import json

import pytest

from test_v_backfill import offline
from test_t_verify import env, add_company, Services, rows_for, findings_for, run, logs, company, event
from vcradar import db, fx, site, verify, scan
from vcradar.deep_fetch import Budget


@pytest.mark.parametrize('changes,expected',[
    ({},True),({'country':'Colombia'},False),({'baseline':True},False),({'filtered':True},False),
    ({'country':'USA','amount':None},True),({'country':'USA','round_text':None},True),
])
def test_40_1_scope(env,changes,expected):
    cid=add_company(env,**changes);service=Services();run(env,cid,service)
    assert bool(service.queries)==expected


def test_40_2_multi_field_searches_share_three_calls(env):
    cid=add_company(env,round_text=None,amount=None,label=True)
    service=Services(rows_for());run(env,cid,service)
    assert len(service.queries)==3 and len(set(service.queries))==3
    assert len(service.model_requests)==2
    assert all('trust administration' in q for q in service.queries)


@pytest.mark.parametrize('placeholder',['others','UNDISCLOSED','Various','Existing Investors','ANGEL INVESTORS','未披露'])
def test_40_3_non_names_never_enter_list_query_or_known_facts(env,placeholder):
    cid=add_company(env,terms=(),leads=(placeholder,),follows=('YC','General Catalyst'))
    context=verify.company_context(env[0],cid,env[1])
    assert context['investors']==[] and event(env,cid)['leads_json']=='[]'
    row={'title':'Aster','snippet':f'Aster backed by {placeholder}, YC and General Catalyst.','url':'https://news.test/a'}
    assert verify.known_facts(row,context)==[]
    service=Services();run(env,cid,service)
    assert not service.queries


def test_40_3_techstars_is_distinguishing_and_common_list_is_exact(env):
    assert verify.TIERS['common_investors']==['Y Combinator','General Catalyst']
    cid=add_company(env,terms=(),leads=('Techstars',));service=Services();run(env,cid,service)
    assert len(service.queries)==3 and all('Techstars' in q for q in service.queries)
    context=verify.company_context(env[0],cid,env[1])
    assert '投资方：Techstars' in verify.known_facts({'title':'Aster','snippet':'Aster backed by Techstars.','url':'https://news.test/a'},context)


def test_40_4_unknown_url_or_nonverbatim_quote_is_rejected(env):
    cid=add_company(env);rows=rows_for();findings=findings_for(rows)
    findings[0]['url']='https://missing.test/a';findings[1]['quote']='Invented headquarters sentence.'
    service=Services(rows,findings);run(env,cid,service)
    assert company(env,cid)['country']=='国家待确认'


@pytest.mark.parametrize('official,expected',[(True,'披露'),(False,'未披露')])
def test_40_5_amount_requires_official_and_independent_media(env,official,expected):
    cid=add_company(env,amount=None)
    rows=rows_for(official=official,amount='$6M')
    run(env,cid,Services(rows,findings_for(rows,amount='$6M')))
    assert event(env,cid)['amount_status']==expected


def test_40_6_detail_shows_confirmed_links(env):
    cid=add_company(env);rows=rows_for();run(env,cid,Services(rows,findings_for(rows)))
    details=site._verification(env[0],env[2].now())[1][cid]
    assert all(v['text']=='入库时已查证' and v['urls'] for v in details)


def test_40_7_follow_investor_identity_and_failed_search_retry(env):
    cid=add_company(env,terms=(),leads=('others',),follows=('Rare Partners',))
    service=Services(responses=[{'status':500}]);run(env,cid,service)
    assert 'Rare Partners' in service.queries[0]
    assert logs(env,cid)['国家']['result']=='本次未查'
    fresh=Services();run(env,cid,fresh)
    assert fresh.queries


@pytest.mark.parametrize('status',['币种待确认','暂未换算','金额待确认'])
def test_40_8_uncomparable_amounts_do_not_create_ten_percent_conflicts(env,status):
    cid=add_company(env)
    env[0].execute('UPDATE funding_event SET amount_status=?',(status,))
    context=verify.company_context(env[0],cid,env[1])
    rows=rows_for(official=True,amount='$60M')
    findings=verify.validate_findings({'fields':findings_for(rows,amount='$60M')},rows,context,{},None)
    _,conflicts=verify.judge(context,findings,['国家'])
    assert not any(c['field']=='金额' for c in conflicts)
    assert '金额' not in verify.known_facts({'title':'Aster','snippet':'Aster raised $10M','url':'https://news.test/a'},context)


def test_40_9_auth_failures_stop_search_without_throwing(env):
    cid=add_company(env);service=Services(responses=[{'status':402}]);run(env,cid,service)
    assert len(service.queries)==1 and logs(env,cid)['国家']['reason']=='key 或余额问题（401 / 402）'


def test_40_10_daily_limit_and_after_holiday(env):
    cfg=env[1]
    assert verify.daily_limit(cfg,env[2].now().date())==60
    assert verify.daily_limit(cfg,env[2].now().date()-timedelta(days=1))==120
    cfg.verify_daily_limit=1
    for name in ['Aster','Birch']:
        cid=add_company(env,name);run(env,cid,Services())
    assert db.daily_verify_search_count(env[0],env[2].now().date())==1
    assert any(r[0]=='超当天上限' for r in env[0].execute('SELECT reason FROM verify_log'))


def test_40_11_confirmed_field_rechecks_only_after_thirty_days(env):
    cid=add_company(env,round_text=None);rows=rows_for();run(env,cid,Services(rows,findings_for(rows)))
    service=Services();run(env,cid,service);assert not service.queries
    env[2].advance(30*86400)
    run(env,cid,service);assert service.queries


def test_40_12_and_13_zero_results_unbilled_and_daily_stats_match_sql(env):
    cid=add_company(env);run(env,cid,Services())
    conn=env[0]
    assert conn.execute('SELECT SUM(cost) FROM verify_search').fetchone()[0]==0
    stats=site._verification(conn,env[2].now())[0]
    assert stats['searches']==db.daily_verify_search_count(conn,env[2].now().date())==3
    assert stats['companies']==1 and stats['fields']==0


def test_40_14_ambiguous_yen_and_no_zero_search_label(env):
    assert fx.parse_amount('¥8000 万（约 110 万美元）')[0] is None
    assert fx.parse_amount('¥500 万日元')[0]=='JPY'
    cid=add_company(env,terms=(),leads=('YC',));run(env,cid,Services())
    details=site._verification(env[0],env[2].now())[1][cid]
    assert all('已查过 0 次' not in v['text'] for v in details)


@pytest.mark.parametrize('status,delay',[(200,16),(403,0)])
def test_40_15_fixed_page_failure_only_affects_labels(env,tmp_path,status,delay):
    conn,cfg,clock=env
    cid=add_company(env,label=True)
    rows=rows_for(official=True)
    root=tmp_path/'fixtures';(root/'search').mkdir(parents=True)
    (root/'verify-model').mkdir();(root/'verify-pages').mkdir()
    (root/'search/a.json').write_text(json.dumps({'match':'Aster','status':200,'results':rows}))
    fields=findings_for(rows)+[{'field':'标签','value':{'ai':{'verdict':'是','quote':'AI is the product core.'},'social':{'verdict':'否','quote':''}},
                              'quote':'AI is the product core.','url':rows[0]['url']}]
    for i in [1,2]:(root/f'verify-model/aster-{i}.json').write_text(json.dumps({'fields':fields}))
    stem=root/'verify-pages'/hashlib.sha1(rows[0]['url'].encode()).hexdigest()
    stem.with_suffix('.html').write_text('<p>AI is the product core.</p>')
    stem.with_suffix('.meta.json').write_text(json.dumps({'status':status,'delay_s':delay}))
    run(env,cid,verify.FixtureServices(root,clock))
    assert company(env,cid)['country']=='美国'
    assert logs(env,cid)['标签']['result']=='本次未查' and logs(env,cid)['标签']['reason']=='抓页失败'
