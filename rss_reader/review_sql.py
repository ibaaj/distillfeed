"""Bounded SQLite review queries. No full descriptions leave SQL for day counts."""
from __future__ import annotations
import json
from typing import Any, Mapping
from distillfeed_arxiv.dates import canonical_day_sql


def query_context(connection, scope, minimum_relevance: int) -> tuple[str, list[Any]]:
    connection.create_function('df_casefold', 1, lambda value: str(value or '').casefold(), deterministic=True)
    marks = ','.join('?' for _ in scope.feed_ids) or 'NULL'
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='distillfeed_arxiv_papers'").fetchone()
    if exists:
        ax = '''ap.item_id AS arxiv_item_id, ap.arxiv_id, ap.pdf_url, ap.local_score,
          ap.llm_score, ap.decision AS arxiv_decision, ap.why AS arxiv_why,
          ap.tags_json AS arxiv_tags_json, ap.local_reasons_json, ap.evaluation_status'''
        join = 'LEFT JOIN distillfeed_arxiv_papers ap ON ap.item_id=i.id'
        day = canonical_day_sql(connection)
    else:
        ax = '''NULL AS arxiv_item_id, NULL AS arxiv_id, NULL AS pdf_url, NULL AS local_score,
          NULL AS llm_score, NULL AS arxiv_decision, NULL AS arxiv_why,
          NULL AS arxiv_tags_json, NULL AS local_reasons_json, NULL AS evaluation_status'''
        join = ''
        day = "COALESCE(date(i.published_at),date(i.discovered_at),'undated')"
    # A single current evaluation exists per item; the legacy path is indexed by item_id.
    source_date = 'i.published_at'
    future = "(f.xml_url NOT LIKE 'plugin://arxiv/%' AND julianday(i.published_at)>julianday(i.discovered_at)+2.0/24)"
    display_date = f"CASE WHEN {future} THEN i.discovered_at ELSE i.published_at END"
    day = day.replace('date(i.published_at)', f'date({display_date})')
    cte = f'''WITH raw AS (
      SELECT i.id,i.feed_id,i.title,i.url,i.author,{display_date} AS published_at,i.discovered_at,
        {source_date} AS source_published_at,{future} AS future_date_warning,
        i.description_text,i.summary_eligible,i.is_read,i.is_starred,i.is_read_later,
        f.title AS feed_title,f.xml_url,f.llm_enabled,
        (f.xml_url LIKE 'plugin://arxiv/%') AS is_arxiv,{day} AS day,
        e.relevance AS ordinary_score,e.description AS ordinary_summary,
        e.justification AS ordinary_why,e.story_cluster,
        l.importance AS legacy_score,l.description AS legacy_summary,l.justification AS legacy_why,
        COALESCE((SELECT group_concat(t.name,' · ') FROM item_tags it
          JOIN tags t ON t.id=it.tag_id WHERE it.item_id=i.id),'') AS user_tags,
        {ax}
      FROM items i JOIN feeds f ON f.id=i.feed_id
      LEFT JOIN ai_evaluations e ON e.item_id=i.id AND e.current=1
      LEFT JOIN summary_items l ON l.rowid=(
        SELECT si.rowid FROM summary_items si JOIN summaries s ON s.id=si.summary_id
        JOIN llm_runs lr ON lr.id=s.llm_run_id
        WHERE si.item_id=i.id AND si.included=1 AND lr.status='success'
        ORDER BY lr.id DESC LIMIT 1)
      {join} WHERE f.enabled=1 AND i.feed_id IN ({marks})
    ), scored AS (
      SELECT *, CASE WHEN is_arxiv THEN llm_score ELSE COALESCE(ordinary_score,legacy_score) END AS ai_score,
        CASE WHEN is_arxiv THEN COALESCE(arxiv_why,'') ELSE COALESCE(ordinary_why,legacy_why,'') END AS why,
        COALESCE(ordinary_summary,legacy_summary,'') AS ai_summary,
        COALESCE(CAST(strftime('%s',COALESCE(published_at,discovered_at)) AS INTEGER),0)*1000000 AS stamp
      FROM raw
    ), states AS (
      SELECT *, CASE WHEN ai_score IS NOT NULL THEN 'scored'
          WHEN is_arxiv AND evaluation_status='pending' THEN 'pending'
          WHEN NOT is_arxiv AND llm_enabled AND summary_eligible THEN 'pending'
          ELSE 'not-sent' END AS ai_state,
        CASE WHEN ai_score IS NULL THEN ''
          WHEN is_arxiv AND arxiv_decision IN ('keep','drop') THEN arxiv_decision
          WHEN ai_score>=? THEN 'keep' ELSE 'drop' END AS decision
      FROM scored
    ), r AS (
      SELECT *, CASE WHEN ai_state='scored' AND decision='keep' THEN 0
        WHEN ai_state='scored' AND decision='drop' THEN 1
        WHEN ai_state='scored' THEN 2 WHEN ai_state='pending' THEN 3 ELSE 4 END AS cohort
      FROM states
    )'''
    return cte, [*scope.feed_ids, int(minimum_relevance)]


def filter_sql(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    terms: list[str] = []; params: list[Any] = []
    if filters.get('q'):
        terms.append("instr(df_casefold(COALESCE(title,'')||' '||COALESCE(author,'')||' '||feed_title||' '||why||' '||ai_summary||' '||description_text||' '||COALESCE(arxiv_id,'')||' '||COALESCE(arxiv_tags_json,'')||' '||user_tags),?)>0")
        params.append(str(filters['q']).casefold())
    if filters['read'] != 'all':
        terms.append('is_read=?'); params.append(int(filters['read']=='read'))
    if filters['saved'] != 'all':
        terms.append('is_starred=1' if filters['saved']=='starred' else 'is_read_later=1')
    for field, column in [('ai','ai_state'),('decision','decision')]:
        if filters[field]!='all': terms.append(f'{column}=?'); params.append(filters[field])
    if int(filters['source']): terms.append('feed_id=?'); params.append(int(filters['source']))
    if int(filters['min_ai']): terms.append('ai_score>=?'); params.append(int(filters['min_ai']))
    if filters['from'] or filters['to']: terms.append("day<>'undated'")
    if filters['from']: terms.append('day>=?'); params.append(filters['from'])
    if filters['to']: terms.append('day<=?'); params.append(filters['to'])
    return ' AND '.join(terms) or '1', params


def order_keys(mode: str) -> tuple[str, ...]:
    if mode=='date': return ('-stamp','-id')
    if mode=='local': return ('-COALESCE(local_score,-1)','cohort','-COALESCE(ai_score,-1)','-stamp','-id')
    return ('cohort','-COALESCE(ai_score,-1)','-COALESCE(local_score,-1)','-stamp','-id')


COMPACT = '''id,feed_id,feed_title,title,url,author,published_at,day,is_read,is_starred,is_read_later,
  ai_state,ai_score,decision,local_score,substr(why,1,280) AS why,is_arxiv,arxiv_id,pdf_url,source_published_at,future_date_warning,
  arxiv_tags_json,user_tags,cohort,stamp'''


def item_dict(row) -> dict[str, Any]:
    result = dict(row)
    for key in ('is_read','is_starred','is_read_later','is_arxiv'):
        result[key] = bool(result.get(key))
    try:
        tags = json.loads(result.pop('arxiv_tags_json',None) or '[]')
        tags = tags if isinstance(tags,list) else []
    except (TypeError, ValueError): tags=[]
    user_tags = str(result.pop('user_tags','') or '').split(' · ')
    result['tags'] = list(dict.fromkeys(str(t) for t in [*tags,*user_tags] if str(t).strip()))
    result['arxiv_id'] = str(result.get('arxiv_id') or '')
    result['pdf_url'] = str(result.get('pdf_url') or '')
    result['author'] = str(result.get('author') or '')
    result['url'] = str(result.get('url') or '')
    if 'local_reasons_json' in result:
        try: result['local_reasons'] = json.loads(result.pop('local_reasons_json') or '[]')
        except (TypeError,ValueError): result['local_reasons']=[]
    from .media import youtube_metadata
    result.update(youtube_metadata(result['url']))
    return result
