# -*- coding: utf-8 -*-
import functools
import inspect

_LOG_PATH = 'offhangar_debug.log'

def doLog(project, *args, **kwargs):
    def _safeUnicode(obj):
        try:
            if isinstance(obj, unicode):
                return obj
            if isinstance(obj, str):
                return obj.decode('utf-8')
            return unicode(obj)
        except Exception:
            try:
                r = repr(obj)
                if isinstance(r, unicode):
                    return r
                return r.decode('utf-8', 'replace')
            except Exception:
                return u'<unprintable>'

    try:
        project = _safeUnicode(project)
    except Exception:
        project = u'OFFHANGAR'

    argsText = u' '.join([_safeUnicode(s) for s in args])

    kwargsText = u''
    if kwargs:
        kwargsText = _safeUnicode(kwargs)

    line = u'[%s] %s %s' % (project, argsText, kwargsText)

    try:
        print line
    except Exception:
        try:
            print line.encode('utf-8', 'replace')
        except Exception:
            pass
    try:
        f = open(_LOG_PATH, 'a')
        f.write(line.encode('utf-8', 'replace') + '\n')
        f.flush()          # <-- добавь эту строку
        f.close()
    except Exception as e:
        try:
            print 'LOG WRITE FAIL: %s' % e
        except Exception:
            pass

def override(obj, prop, getter=None, setter=None, deleter=None):
    if inspect.isclass(obj) and prop.startswith('__') and prop not in dir(obj) + dir(type(obj)):
        prop = '_' + obj.__name__ + prop
    if not prop.startswith('_'):
        mangled = '_' + prop
        if hasattr(obj, mangled):
            prop = mangled
    src = getattr(obj, prop)
    if type(src) is property and (getter or setter or deleter):
        g = functools.partial(getter, src.fget) if getter else src.fget
        s = functools.partial(setter, src.fset) if setter else src.fset
        d = functools.partial(deleter, src.fdel) if deleter else src.fdel
        setattr(obj, prop, property(g, s, d))
        return getter
    elif getter:
        getter_new = lambda *a, **kw: getter(src, *a, **kw)
        if not inspect.ismethod(src) and inspect.isclass(obj):
            getter_new = staticmethod(getter_new)
        setattr(obj, prop, getter_new)
        return getter
    else:
        return functools.partial(override, obj, prop)