from __future__ import annotations
import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import pytest
from rss_reader.db import connect
from rss_reader.review import resolve_review_scope,parse_review_filters,list_review_days
from rss_reader.ai_readiness import _arxiv_estimate
from distillfeed_arxiv.config import load_plugin_config
from distillfeed_arxiv.models import Paper
from distillfeed_arxiv.llm import LLMUsage
from distillfeed_arxiv.dates import ensure_date_schema
from test_release025 import configured_at

p=importlib.import_module('distillfeed_arxiv.plugin')


def make_paper(identifier,day,source='rss'):
    stamp=datetime.fromisoformat(day).replace(tzinfo=UTC)
    return Paper(arxiv_id=identifier,version='v1',title='Reasoning under uncertainty',abstract='Symbolic learning and logic.',authors=['Author'],categories=['cs.AI'],primary_category='cs.AI',link='https://arxiv.org/abs/'+identifier,pdf_link=None,published=stamp,updated=None,source=source,announced_at=stamp if source=='rss' else None,announcement_source='rss' if source=='rss' else 'unknown')


def setup(tmp_path,n):
    config=configured_at(tmp_path)
    c=connect(config.database_path); p.plugin.initialize(c,config)
    cfg=load_plugin_config(config); cfg['llm']['enabled']=True;cfg['filters']['broad_candidate_threshold']=0
    group,feeds=p._ensure_sources(c,cfg)
    for index in range(n):
        # Old enough to be outside every ordinary RSS age window. Local scores
        # below today's gate must not erase existing completed AI work.
        day=(datetime(2024,1,1)+timedelta(days=index)).date().isoformat()
        identifier,_,_=p._store_paper(c,make_paper(f'2401.{index:05d}',day),feeds['cs.AI'])
        c.execute("UPDATE distillfeed_arxiv_papers SET local_score=-2,llm_score=91,final_score=29.85,decision='keep',why='Relevant logic',evaluation_status='complete' WHERE item_id=?",(identifier,))
    context=SimpleNamespace(connection=c,config=config,feed_id=None,group_id=group,automatic=False,cancel_requested=lambda:False)
    return c,context,cfg,group,feeds


def evaluate(context,cfg,group,feeds):
    return p.plugin._evaluate_pending(context,cfg,group,feeds,list(feeds),{'status':'success','attempted':0,'succeeded':0,'failed':0,'new_items':0},advance_watermark=False,allow_model=True)


def test_49_old_days_actual_executor_matches_readiness_without_reranking(tmp_path,monkeypatch):
    c,context,cfg,group,feeds=setup(tmp_path,49)
    monkeypatch.setattr(p,'_model_blocker',lambda cfg:None)
    monkeypatch.setattr(p,'rerank',lambda *a,**kw:pytest.fail('Existing AI scores were ranked again'))
    calls=[]
    def daily(entries,cfg,language):
        days={p._paper_day(paper) for paper,_local,_decision in entries};assert len(days)==1
        calls.append(next(iter(days)))
        return {'overview':'# Historical brief\n\nRecovered.','sections':[]},LLMUsage()
    monkeypatch.setattr(p,'daily_digest',daily)
    try:
        assert _arxiv_estimate(c,cfg)['missing_daily_digests']==49
        result=evaluate(context,cfg,group,feeds)
        assert result['status']=='success',result
        assert result['daily_digests']==49 and result['evaluated_items']==0
        assert len(calls)==len(set(calls))==49
        assert _arxiv_estimate(c,cfg)['missing_daily_digests']==0
        assert _arxiv_estimate(c,cfg)['planned_requests']==0
        assert evaluate(context,cfg,group,feeds)['status']=='unchanged'
        assert len(calls)==49
        assert c.execute('SELECT COUNT(*) FROM distillfeed_arxiv_papers WHERE llm_score=91').fetchone()[0]==49
        assert c.execute('SELECT COUNT(*) FROM distillfeed_arxiv_daily_briefs').fetchone()[0]==49
        print('49 historical days: 49 distinct briefs; 0 rerank calls; second update: 0 provider calls')
    finally: c.close()


def test_failure_retains_completed_days_and_resumes_remaining(tmp_path,monkeypatch):
    c,context,cfg,group,feeds=setup(tmp_path,7)
    monkeypatch.setattr(p,'_model_blocker',lambda cfg:None)
    calls=[]; fail=True
    def daily(entries,cfg,language):
        day=p._paper_day(entries[0][0]);calls.append(day)
        if fail and day=='2024-01-04': raise RuntimeError('Simulated transient failure')
        return {'overview':day,'sections':[]},LLMUsage()
    monkeypatch.setattr(p,'daily_digest',daily)
    try:
        result=evaluate(context,cfg,group,feeds)
        assert result['status']=='llm-failed' and result['daily_digests']==3
        assert _arxiv_estimate(c,cfg)['missing_daily_digests']==4
        fail=False
        result=evaluate(context,cfg,group,feeds)
        assert result['daily_digests']==4
        assert calls[:3]==['2024-01-01','2024-01-02','2024-01-03']
        assert all(calls.count(day)==1 for day in calls[:3])
        assert _arxiv_estimate(c,cfg)['missing_daily_digests']==0
    finally:c.close()


def test_unknown_dates_last_and_api_enrichment_preserves_announced_day(tmp_path):
    c,context,cfg,group,feeds=setup(tmp_path,2)
    try:
        unknown=make_paper('2609.00001','2026-09-05','api')
        identifier,_,_=p._store_paper(c,unknown,feeds['cs.AI'])
        # The API submission date is not promoted to an announcement date.
        scope=resolve_review_scope(c,group_id=group);f=parse_review_filters({'preset':'everything'})
        days=list_review_days(c,scope,f,minimum_relevance=70)
        assert days['days'][-1]['day']=='undated'
        assert days['default_open_day']!='undated'
        rss=make_paper('2609.00001','2026-09-08','rss')
        p._store_paper(c,rss,feeds['cs.AI'])
        p._store_paper(c,unknown,feeds['cs.AI'])
        row=c.execute('SELECT ap.*,i.* FROM distillfeed_arxiv_papers ap JOIN items i ON i.id=ap.item_id WHERE i.id=?',(identifier,)).fetchone()
        assert p._paper_day(p._paper_from_row(row))=='2026-09-08'
        assert row['published_at'].startswith('2026-09-08')
        ensure_date_schema(c);ensure_date_schema(c)
        assert c.execute('SELECT announced_at FROM distillfeed_arxiv_papers WHERE item_id=?',(identifier,)).fetchone()[0].startswith('2026-09-08')
    finally:c.close()
