"""Explicit source preferences; feed > nearest non-empty group > global."""
from __future__ import annotations
MAX_INTEREST_CHARS = 8000


def validate_interests(value) -> str:
    if not isinstance(value,str): raise ValueError('Interests must be text')
    value=value.strip()
    if len(value)>MAX_INTEREST_CHARS or '\x00' in value:
        raise ValueError('Use at most 8,000 characters without NUL characters')
    return value


def interest_map(connection, global_profile: str = '') -> dict[int,str]:
    groups={int(r['id']):dict(r) for r in connection.execute('SELECT id,parent_id,interest_profile FROM groups')}
    cache={}
    def group_profile(identifier, trail=frozenset()):
        if identifier in cache: return cache[identifier]
        if identifier in trail or identifier not in groups: return global_profile
        row=groups[identifier]
        value=str(row['interest_profile'] or '').strip()
        if not value and row['parent_id'] is not None:
            value=group_profile(int(row['parent_id']),trail|{identifier})
        cache[identifier]=value or global_profile
        return cache[identifier]
    return {int(r['id']):str(r['interest_profile'] or '').strip() or group_profile(int(r['group_id']))
            for r in connection.execute('SELECT id,group_id,interest_profile FROM feeds')}


def scope_interests(connection, scope, global_profile: str) -> dict:
    table='feeds' if scope.kind=='feed' else 'groups'
    row=connection.execute(f'SELECT interest_profile FROM {table} WHERE id=?',(scope.scope_id,)).fetchone()
    own=str(row[0] or '') if row else ''
    if scope.kind=='feed':
        effective=interest_map(connection,global_profile).get(scope.scope_id,global_profile)
    else:
        effective=own
        identifier=scope.scope_id; seen=set()
        while not effective and identifier not in seen:
            seen.add(identifier)
            row=connection.execute('SELECT parent_id,interest_profile FROM groups WHERE id=?',(identifier,)).fetchone()
            if not row: break
            effective=str(row['interest_profile'] or '').strip()
            if row['parent_id'] is None: break
            identifier=int(row['parent_id'])
        effective=effective or global_profile
    return {'value':own,'effective':effective,'inherited':not bool(own), 'scope_title':scope.title,
            'max_length':MAX_INTEREST_CHARS, 'editable':not scope.is_arxiv}
