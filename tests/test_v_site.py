"""V16l #39⑩ 三阶段页面、唯一 SQL 条件和补充入口。"""
from dataclasses import replace
import json
import shutil
import subprocess

import pytest

from test_v_backfill import (env, offline, add_event, manifest, apply, seed_digest, NOW, APPLY, PUSH,
                             FIRST, TRANSITION_NAMES)
from vcradar import backfill, db, site, scan


def javascript(data, tmp_path, script):
    if not shutil.which('node'):
        pytest.skip('离线前端验证需要 Node')
    path=tmp_path/'data.json';path.write_text(json.dumps(data,ensure_ascii=False))
    return json.loads(subprocess.run(['node','-e',
        "const app=require(process.argv[1]),fs=require('fs'),data=JSON.parse(fs.readFileSync(process.argv[2]));"+script,
        str(site.ASSETS/'app.js'),str(path)],check=True,capture_output=True,text=True).stdout)


def test_39_10_three_phases_match_sql_and_keep_four_first_issue_cards(env,tmp_path):
    conn,cfg=env
    transition=[add_event(conn,name,amount=f'${i+1}M') for i,name in enumerate(TRANSITION_NAMES)]
    other=[add_event(conn,name,round_text='Series B',filtered=('总部非美国','轮次不在范围')) for name in ['NeuroXess','Simplismart']]
    first_four=[add_event(conn,name,pending=True,filtered=(),country=None) for name in ['FinMaq','Sava','Thrive Holdings','Viraj Aero']]
    pending_filtered=[add_event(conn,f'Filtered pending {i}',pending=True,filtered=('轮次不在范围',),round_text='Series B') for i in range(7)]
    baseline=[add_event(conn,f'Baseline {i}',baseline=True) for i in range(35)]
    conn.execute("UPDATE company SET manual_label='AI相关'")
    conn.execute("UPDATE company SET country='美国' WHERE status='总部待核'")
    seed_digest(conn,transition+other+first_four+pending_filtered)
    original_digest=json.loads(conn.execute('SELECT event_ids_json FROM digest').fetchone()[0])
    original_dates={eid:conn.execute('SELECT push_date FROM funding_event WHERE id=?',(eid,)).fetchone()[0] for eid in first_four}
    before=site._snapshot(conn,cfg,NOW)
    assert set(before['week']['cards'])==set(first_four)
    assert set(original_digest)-set(before['week']['event_ids'])==set(transition)
    assert before['status']['pending_ratios']['country']['count']==4
    assert len(before['week']['filtered'])==9
    assert {next(e['name'] for e in before['events'] if e['id']==eid) for eid in transition}==set(TRANSITION_NAMES)
    data=manifest(env);apply(env,data)
    after=site._snapshot(conn,cfg,APPLY)
    assert set(after['week']['cards'])==set(first_four) and len(after['week']['filtered'])==9
    assert json.loads(conn.execute('SELECT event_ids_json FROM digest').fetchone()[0])==original_digest
    assert after['status']['pending_ratios']['country']['count']==4
    assert all(conn.execute('SELECT push_date FROM funding_event WHERE id=?',(eid,)).fetchone()[0]==d for eid,d in original_dates.items())
    unsent={e['id'] for e in after['events'] if e['supplement_unsent']}
    assert unsent==set(transition+baseline) and len(unsent)==55
    script="""
      const f=app.defaultFilters(),ids=x=>app.selectEvents(data,{...f,...x}).map(e=>e.id);
      const route=app.filtersForRoute('#backfill','#list',{...f,tag:'都不是',status:'已纳入'});
      console.log(JSON.stringify({all:ids({}),admitted:ids({status:'已纳入'}),tag:ids({tag:'AI相关'}),
        round:ids({round:'种子轮'}),marked:ids({backfill:true}),period:ids({period:'date:2026-10-31'}),
        route,landing:app.selectEvents(data,route).map(e=>e.id),html:app.listHtml(data,route)}));
    """
    def check(snapshot):
        index,_=site._site_payloads(snapshot)
        result=javascript(index,tmp_path,script)
        sql={r[0] for r in conn.execute("SELECT id FROM company WHERE status IN ('已纳入','总部待核') AND IFNULL(backfill,'')<>'补充未推送'")}
        actual={next(e['company_id'] for e in snapshot['events'] if e['id']==eid) for eid in result['admitted']}
        assert actual==sql
        return result
    result=check(after)
    assert unsent<=set(result['all']) and unsent<=set(result['tag']) and unsent<=set(result['round'])
    assert not unsent & set(result['admitted']+result['period']+result['marked'])
    k={c['event_id'] for c in data['candidates'] if c['rank'] and c['rank']>10}
    assert set(result['landing'])==k and len(k)==45
    assert result['route']==dict(period='all',tag='all',round='all',status='all',backfill=False,unsentOnly=True)
    assert '补充未推送 45 家' not in result['html'] and '查看全部' in result['html']
    assert '总部待核' not in result['html'] and '非美国' not in result['html']
    sections,ids=backfill.collect_supplement(conn,cfg,PUSH.date())
    backfill.mark_supplement(conn,ids,PUSH.date());conn.commit()
    pushed=site._snapshot(conn,cfg,PUSH)
    assert set(pushed['week']['cards'])==set(first_four)
    result=check(pushed)
    # V17c：只改公司补充状态还不够，事件必须已写进 digest.补充事件。
    assert result['marked']==[] and not k & set(result['admitted'])
    assert set(result['landing'])==k
    scan.upsert_digest(conn,{'date':PUSH.date(),'push':True,'event_ids':[],
                            'supplement_event_ids_json':ids,'generated_at':PUSH,'status':'没有新增'})
    conn.commit()
    final=site._snapshot(conn,cfg,PUSH)
    assert final['week']['event_ids']==[] and final['status']['pending_ratios']['denominator']==0
    index,details=site._site_payloads(final)
    by_period=javascript(index,tmp_path,"""
      const f={...app.defaultFilters(),backfill:true,status:'已纳入'};
      console.log(JSON.stringify(['all','30','date:2026-11-06','week'].map(period=>app.selectEvents(data,{...f,period}).map(e=>e.id))));
    """)
    assert by_period==[ids,ids,ids,[]]
    for eid in first_four:
        event=next(e for e in final['events'] if e['id']==eid)
        assert event['push_date']==FIRST and not event['backfill']
    assert all('此前漏掉' not in details[f'c/{c}.json']['companies'][0]['backfill'] for c in ['finmaq','sava','thrive-holdings','viraj-aero'])


def test_40_12_and_13_spend_log_counts_search_month_and_ignores_apply(env):
    conn,test_cfg=env
    eid=add_event(conn,'Spend',country=None,pending=True,filtered=())
    cid=conn.execute('SELECT company_id FROM funding_event WHERE id=?',(eid,)).fetchone()[0]
    cfg=replace(test_cfg,test_mode=False)
    cfg.outbox_dir.mkdir(parents=True)
    log=cfg.outbox_dir/'backfill-spend.log'
    log.write_text('2026-10-31\t3\t0.025\n2026-10-30\t1\t0.0085\n2026-09-30\t3\t10\n')
    for src,cost in [('日常',.01),('首期',.02),('补充命令',.025)]:
        conn.execute("INSERT INTO verify_search(company_id,source,run_date,search_date,requested_at,query,status,cost,model_cost) VALUES (?,?, '2026-11-01','2026-10-31',?,?,200,?,.0035)",
                     (cid,src,'2026-10-31 14:00:00',src,cost))
    conn.commit()
    stats,_=site._verification(conn,NOW,cfg)
    assert stats['searches']==0 and stats['month_used']==pytest.approx(.0705)
    assert stats['backfill_cost']==pytest.approx(.0335) and stats['first_issue_cost']==pytest.approx(.0235)
    conn.execute("INSERT INTO verify_search(company_id,source,run_date,search_date,requested_at,query,status,cost) VALUES (?,'补充命令','2026-10-31','2026-10-31','2026-10-31 14:00:01','apply',200,99)",(cid,))
    conn.commit()
    assert site._verification(conn,NOW,cfg)[0]==stats
    assert db.daily_verify_search_count(conn,NOW.date())==0


def test_39_10_admitted_filter_uses_company_sql_even_with_new_k_event(env,tmp_path):
    conn,cfg=env
    for i in range(11):add_event(conn,f'Extra {i:02}',amount=f'${20-i}M')
    data=manifest(env);apply(env,data)
    k=next(c for c in data['candidates'] if c['rank']==11)
    newer=add_event(conn,k['name'],round_text='Series A',filtered=(),day=PUSH)
    conn.commit()
    snapshot=site._snapshot(conn,cfg,PUSH)
    index,_=site._site_payloads(snapshot)
    result=javascript(index,tmp_path,"""
      const f=app.defaultFilters();
      console.log(JSON.stringify({all:app.selectEvents(data,f).map(e=>e.id),
        admitted:app.selectEvents(data,{...f,status:'已纳入'}).map(e=>e.id)}));
    """)
    assert newer in result['all'] and newer not in result['admitted']
    allowed={r[0] for r in conn.execute("SELECT id FROM company WHERE status IN ('已纳入','总部待核') AND IFNULL(backfill,'')<>'补充未推送'")}
    assert {e['company_id'] for e in snapshot['events'] if e['id'] in result['admitted']}<=allowed
