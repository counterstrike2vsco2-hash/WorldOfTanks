# -*- coding: utf-8 -*-
"""Поиск карты, выбранной игроком в окне "Демонстрация карты".

Окно родное (клиентское): мод его не рисует, а бой стартует через dispatcher.doAction (красная кнопка "В бой!").
Куда клиент кладёт выбранную карту, из мода не видно, поэтому здесь поиск "вслепую": обходим объекты
(action, диспетчер и всё, что в них лежит, на глубину 3) и ищем поле, в имени которого есть arenaType / mapID,
а значение - существующий id типа арены. Всё найденное и не найденное пишется в лог ([MAP] ...), чтобы по логу
можно было точно указать, где лежит выбор.
"""
import re

import ArenaType

from gui.mods.offhangar.logging import LOG_DEBUG

_NAME_RE = re.compile(r'arena_?type|map_?id|selected_?map|chosen_?map|demo_?map|mapname|geometry', re.I)
_SKIP_TYPES = (int, long, float, bool, basestring, type(None), tuple, set, frozenset)
_LOGGED = {'dump': 0}
MAX_NODES = 2500


def _isArenaID(v):
    try:
        return isinstance(v, (int, long)) and not isinstance(v, bool) and int(v) in ArenaType.g_cache
    except Exception:
        return False


def _geometryToID(name):
    """Имя геометрии ('11_murovanka') -> id обычного (ctf) варианта этой карты."""
    try:
        for tid, at in ArenaType.g_cache.items():
            if getattr(at, 'geometryName', None) == name and getattr(at, 'gameplayName', '') == 'ctf':
                return tid
        for tid, at in ArenaType.g_cache.items():
            if getattr(at, 'geometryName', None) == name:
                return tid
    except Exception:
        pass
    return None


def probe(roots, report=True):
    """roots: [(имя, объект)]. Возвращает id типа арены или None."""
    found = []
    seen = set()
    budget = [MAX_NODES]
    names = []

    def scan(obj, path, depth):
        if obj is None or depth > 3 or budget[0] <= 0 or id(obj) in seen:
            return
        if isinstance(obj, _SKIP_TYPES) or callable(obj) and not hasattr(obj, '__dict__'):
            return
        seen.add(id(obj))
        budget[0] -= 1
        if isinstance(obj, dict):
            items = list(obj.items())[:60]
            for k, v in items:
                key = '%s[%r]' % (path, k)
                if isinstance(k, basestring) and _NAME_RE.search(k):
                    _consider(key, v)
                scan(v, key, depth + 1)
            return
        if isinstance(obj, (list,)):
            for i, v in enumerate(obj[:20]):
                scan(v, '%s[%d]' % (path, i), depth + 1)
            return
        try:
            attrs = list(getattr(obj, '__dict__', {}).items())
        except Exception:
            attrs = []
        for n, v in attrs:
            key = '%s.%s' % (path, n)
            if depth == 0 and len(names) < 80:
                names.append(n)
            if _NAME_RE.search(n):
                _consider(key, v)
            if isinstance(v, _SKIP_TYPES):
                continue
            mod = getattr(type(v), '__module__', '') or ''
            if isinstance(v, (dict, list)) or mod.startswith(('gui', 'account_helpers', 'helpers', '__main__')):
                scan(v, key, depth + 1)

    def _consider(key, v):
        if _isArenaID(v):
            found.append((key, int(v), 'id'))
        elif isinstance(v, basestring):
            tid = _geometryToID(v)
            if tid is not None:
                found.append((key, int(tid), 'geometry'))

    for name, obj in roots:
        try:
            scan(obj, name, 0)
        except Exception:
            pass

    if report and _LOGGED['dump'] < 6:
        _LOGGED['dump'] += 1
        LOG_DEBUG('[MAP] probe: %d candidates %s; first-level attributes: %s' % (
            len(found), found[:8], names[:60]))
    for key, value, kind in found:
        return value
    return None
