# -*- coding: utf-8 -*-
"""Засвет (спотинг), как в офлайн-моде 0.8.2.

Враг виден, если его "видит" кто-то из нашей команды (игрок или союзный бот):
  * ближе PROXIMITY (50 м) - виден всегда;
  * в пределах радиуса обзора наблюдателя (circularVisionRadius башни) и есть прямая видимость
    (лучи по статике - рельеф и здания; два луча: в корпус и в башню цели);
  * после засвета цель остаётся видимой ещё MEMORY секунд;
  * стрелявший по вам/попавший по вам засвечивается на несколько секунд (reveal).
Невидимые враги: модель скрыта, маркер над ними убран, на миникарте они уходят в "последнюю известную позицию", в них нельзя
навестись (targetCaps). Те же правила работают для вражеской команды: боты атакуют только тех, кого видит их команда.
Выключатель: spotting=0 в offline_user.txt (или ENABLED = False здесь).
"""
import BigWorld
import Math

from debug_utils import LOG_CURRENT_EXCEPTION
from gui.mods.offhangar.logging import LOG_DEBUG, LOG_NOTE

ENABLED = True
TICK = 0.5                 # как часто пересчитывать засвет каждую цель, с
PARTS = 5                  # v87: цели делятся на PARTS порций, порция считается каждые TICK/PARTS с (лучи не пачкой в одном кадре)
PROXIMITY = 50.0           # на этой дистанции виден всегда, м
MEMORY = 5.0               # сколько секунд цель остаётся видимой после засвета
DEFAULT_VIEW = 350.0       # радиус обзора, если не удалось прочитать у машины, м
MAX_VIEW = 445.0           # потолок радиуса обзора, как в игре
EYE_HEIGHT = 2.5           # высота "глаз" наблюдателя над землёй, м
TARGET_HEIGHTS = (1.5, 2.2)  # высоты точек на цели (корпус, башня), м
TUT_REVEAL_RADIUS = 250.0  # учебный бой: на каком расстоянии мишень появляется (tutorial_spot_radius в offline_user.txt), м
TUT_LOW_H = 2.5            # учебный бой: препятствия ниже этой высоты (ежи, надолбы) обзор не закрывают, м
MAX_SPOTTERS_CHECKED = 3   # сколько ближайших наблюдателей проверять лучами на одну цель
FIRE_REVEAL = 4.0          # стрелявший/попавший засвечивается на столько секунд
HIT_REVEAL = 2.5

_ACTIVE = False
_PART = [0]
_DEADSEEN = set()
_TOKEN = 0
_ON = True
_SEEN = {}                 # (команда, видящая; vehicleID цели) -> время, до которого цель видна
_SHOWN = {}                # vehicleID -> показан ли сейчас враг игрока
_REVEALED = set()          # погибшие враги, которых уже видели (их остовы остаются видимыми)
_EVER_SEEN = set()         # враги, которых игрок хоть раз видел (у остальных на миникарте метки нет вообще)
_VIEW_CACHE = {}
_DEAD_AT = {}                # vehicleID -> когда увидели, что машина погибла
_DEAD_MARKER_DONE = set()  # у каких погибших машин метку уже сняли
MANAGE_MARKERS = True        # False - метки (ник, HP) целиком родные, как в v0.5; True - засвет создаёт/уничтожает метки врагов
_LAST_MARKER_DESTROY = [0.0]   # когда в последний раз уничтожали метку: handle'ы переиспользуются, поэтому сразу создавать новую нельзя
MARKER_RECREATE_GAP = 0.6
_HIDDEN_MARKERS = set()    # враги, чью метку скрыл сам засвет (только им метку можно создавать обратно - иначе получатся двойные метки)
_DEAD_RESTORED = set()     # погибшие, которым уже вернули видимость
REMOVE_DEAD_MARKERS = False # True - убирать метку (ник и HP) над погибшей машиной; False - оставить родную метку клиента (как в v50)
DEAD_MARKER_DELAY = 0.5    # ... через столько секунд после смерти, когда клиент уже закончил обработку смерти
DEAD_QUIET = 0.5           # столько секунд после смерти засвет не трогает модель/метки/миникарту погибшей машины


def isEnabled():
    return _ACTIVE and _ON


def isSpottedBy(team, vid):
    """Видит ли команда team машину vid (при выключенном засвете - всегда True)."""
    if not _ACTIVE or not _ON:
        return True
    try:
        return _SEEN.get((team, vid), 0.0) > BigWorld.time()
    except Exception:
        return True


def reveal(vid, byTeam, seconds):
    """Засветить машину vid для команды byTeam на seconds секунд."""
    if not _ACTIVE or not _ON:
        return
    try:
        key = (byTeam, vid)
        until = BigWorld.time() + seconds
        if _SEEN.get(key, 0.0) < until:
            _SEEN[key] = until
        _refreshOne(vid)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def onShot(shooter, target):
    """Попадание: цель видит стрелявшего, стрелявший видит цель (вызывается из bots.onVehicleDamaged)."""
    if not _ACTIVE or not _ON:
        return
    try:
        from gui.mods.offhangar import bots
        st, tt = bots.getTeam(shooter), bots.getTeam(target)
        if st and tt and st != tt:
            reveal(shooter.id, tt, FIRE_REVEAL)
            reveal(target.id, st, HIT_REVEAL)
    except Exception:
        LOG_CURRENT_EXCEPTION()


# ------------------------------------------------------------
def _vehicles():
    from gui.mods.offhangar import bots
    res = []
    try:
        av = BigWorld.player()
        pv = BigWorld.entity(getattr(av, 'playerVehicleID', 0))
        if pv is not None and getattr(pv, 'isStarted', False):
            res.append(pv)
    except Exception:
        pass
    for ai in list(bots._BOTS.values()):
        try:
            v = ai.vehicle()
            if v is not None and getattr(v, 'isStarted', False):
                res.append(v)
        except Exception:
            pass
    return res


def _playerTeam():
    from gui.mods.offhangar import bots
    try:
        av = BigWorld.player()
        pv = BigWorld.entity(getattr(av, 'playerVehicleID', 0))
        return bots.getTeam(pv)
    except Exception:
        return 0


def _viewRadius(v):
    r = _VIEW_CACHE.get(v.id)
    if r is None:
        try:
            r = float(v.typeDescriptor.turret['circularVisionRadius'])
        except Exception:
            r = DEFAULT_VIEW
        r = max(100.0, min(MAX_VIEW, r))
        _VIEW_CACHE[v.id] = r
    return r


def _alive(v):
    try:
        return v.health > 0
    except Exception:
        return False


def _canSee(spaceID, spotter, target, sp, tp, dist2):
    if dist2 <= PROXIMITY * PROXIMITY:
        return True
    try:
        from gui.mods.offhangar import devices as _d2
        if _d2.isPlayerVid(target.id) and _d2.hasPerk('camouflage'):
            dist2 *= 1.2                     # «маскировка» экипажа: враги замечают игрока примерно на 9% ближе
    except Exception:
        pass
    view = _viewRadius(spotter)
    try:
        from gui.mods.offhangar import devices as _dev
        view = max(50.0, min(MAX_VIEW, view * _dev.viewFactor(spotter.id)))   # СУ, командир, оптика, бинокль
    except Exception:
        pass
    if dist2 > view * view:
        return False
    eye = Math.Vector3(sp.x, sp.y + EYE_HEIGHT, sp.z)
    for h in TARGET_HEIGHTS:
        try:
            if BigWorld.wg_collideSegment(spaceID, eye, Math.Vector3(tp.x, tp.y + h, tp.z), 0) is None:
                return True
        except Exception:
            return True
    return False


def _canSeeTutorial(spaceID, sp, tp, dist2):
    """Учебный бой: мишень появляется, когда игрок подъехал ближе TUT_REVEAL_RADIUS (на PROXIMITY - всегда). Прямая видимость
    считается, как в _canSee, но низкие препятствия (ежи, надолбы) её не закрывают: луч, поднятый на TUT_LOW_H, тоже проверяется."""
    if dist2 > TUT_REVEAL_RADIUS * TUT_REVEAL_RADIUS:
        return False
    if dist2 <= PROXIMITY * PROXIMITY:
        return True
    eye = Math.Vector3(sp.x, sp.y + EYE_HEIGHT, sp.z)
    up = Math.Vector3(0.0, TUT_LOW_H, 0.0)
    for h in TARGET_HEIGHTS:
        end = Math.Vector3(tp.x, tp.y + h, tp.z)
        try:
            if BigWorld.wg_collideSegment(spaceID, eye, end, 0) is None:
                return True
            if BigWorld.wg_collideSegment(spaceID, eye + up, end + up, 0) is None:
                return True
        except Exception:
            return True
    return False


def _computeSeen(vehicles, now, part=0, parts=1):
    """Обновляет _SEEN. Возвращает множество погибших врагов, видимых прямо сейчас (для показа остовов)."""
    from gui.mods.offhangar import bots
    space = None
    try:
        space = BigWorld.player().spaceID
    except Exception:
        pass
    info = []
    for v in vehicles:
        info.append((v, bots.getTeam(v), bots._posOf(v), _alive(v)))
    deadSeen = set()
    for team in (bots.TEAM_ALLY, bots.TEAM_ENEMY):
        spotters = [(v, p) for (v, t, p, alive) in info if t == team and alive]
        if not spotters:
            continue
        for idx, (tv, tt, tp, tAlive) in enumerate(info):
            if tt == team or tt == 0:
                continue
            if parts > 1 and idx % parts != part:
                continue
            # ближайшие наблюдатели
            cand = []
            for (sv, sp) in spotters:
                dx, dz = sp.x - tp.x, sp.z - tp.z
                cand.append((dx * dx + dz * dz, sv, sp))
            cand.sort(key=lambda c: c[0])
            seen = False
            tutorial = bots.TUTORIAL[0] and team == bots.TEAM_ALLY
            for d2, sv, sp in cand[:MAX_SPOTTERS_CHECKED]:
                if (_canSeeTutorial(space, sp, tp, d2) if tutorial else _canSee(space, sv, tv, sp, tp, d2)):
                    seen = True
                    break
            if seen:
                if tAlive:
                    _SEEN[(team, tv.id)] = now + MEMORY
                elif team == bots.TEAM_ALLY or team == _playerTeam():
                    deadSeen.add(tv.id)
    return deadSeen


# ------------------------------------------------------------
def _setModelShown(v, show):
    m = getattr(v, 'model', None)
    if m is not None:
        try:
            m.visible = show
        except Exception:
            pass
        try:
            m.visibleAttachments = show
        except Exception:
            pass
    # Раньше здесь ещё писалось .visible в модель из battle._CORPSES. Это сохранённая ссылка: после смерти клиент
    # подменяет модель машины, и запись в уже уничтоженную модель валила игру. Теперь только текущая v.model.
    try:
        v.targetCaps = [1] if show else []
    except Exception:
        pass


def _battleWindow():
    try:
        from gui.WindowsManager import g_windowsManager
        return g_windowsManager.battleWindow
    except Exception:
        return None


def _destroyMarkerSafe(vm, handle):
    """Уничтожает метку КАК СЛЕДУЕТ. Родной VehicleMarkersManager.destroyMarker только удаляет handle из списка и из Flash, но не вызывает
    _VehicleMarker.destroy(): подписка marker на appearance.onModelChanged остаётся жить. Когда у машины потом меняется модель (смерть),
    старый обработчик делает markerSetMatrix(СТАРЫЙ handle, ...) - а этот handle уже выдан другой машине (handle'ы переиспользуются).
    Итог: метка живого союзника перепрыгивала на труп врага и там зависала. Поэтому сначала снимаем подписку (destroy), потом удаляем."""
    try:
        markers = getattr(vm, '_VehicleMarkersManager__markers', None)
        obj = markers.get(handle) if markers is not None else None
        if obj is not None:
            obj.destroy()
    except Exception:
        LOG_CURRENT_EXCEPTION()
    vm.destroyMarker(handle)


def _removeDeadMarker(v):
    """Один раз снимает метку над погибшей машиной. Делается заметно позже смерти (DEAD_MARKER_DELAY): сразу после
    смерти клиент сам работает с этой меткой, и вмешательство в этот момент роняло игру."""
    if v.id in _DEAD_MARKER_DONE:
        return
    _DEAD_MARKER_DONE.add(v.id)
    try:
        if v.id == getattr(BigWorld.player(), 'playerVehicleID', None):
            return
    except Exception:
        pass
    bw = _battleWindow()
    vm = getattr(bw, 'vMarkersManager', None) if bw is not None else None
    marker = getattr(v, 'marker', None)
    if vm is None or marker in (None, -1):
        return
    try:
        LOG_DEBUG('[DEATH] removing marker of dead vehicle %s (handle %s)' % (v.id, marker))
        _destroyMarkerSafe(vm, marker)
        v.marker = None
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _syncMarker(v, show):
    """Метка над живой машиной. Метки погибших не трогаем вообще: их снимает сам клиент в обработчике смерти;
    принудительное уничтожение (двойное уничтожение одного handle) роняло игру сразу после убийства врага."""
    if not MANAGE_MARKERS or not _alive(v):
        return
    bw = _battleWindow()
    vm = getattr(bw, 'vMarkersManager', None) if bw is not None else None
    if vm is None:
        return
    try:
        from gui.mods.offhangar import bots
        if bots.getTeam(v) == _playerTeam():
            return                      # союзников и своих метки не трогаем вообще
    except Exception:
        return
    marker = getattr(v, 'marker', None)
    has = marker not in (None, -1)
    try:
        if (show and not has and v.id in _HIDDEN_MARKERS and getattr(v, 'proxy', None) is not None
                and BigWorld.time() - _LAST_MARKER_DESTROY[0] >= MARKER_RECREATE_GAP):
            _HIDDEN_MARKERS.discard(v.id)
            v.marker = vm.createMarker(v.proxy)
            LOG_DEBUG('[MARKER] created for enemy %s -> handle %s' % (v.id, v.marker))
            try:
                vm.onVehicleHealthChanged(v.marker, v.health)
            except Exception:
                pass
        elif (not show) and has:
            _destroyMarkerSafe(vm, marker)
            _HIDDEN_MARKERS.add(v.id)
            _LAST_MARKER_DESTROY[0] = BigWorld.time()
            LOG_DEBUG('[MARKER] hidden for enemy %s (handle %s)' % (v.id, marker))
            v.marker = -1               # как у клиента после destroyMarker: -1 для всех его обработчиков (invokeMarker(-1) ничего не делает)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _ownID():
    try:
        return getattr(BigWorld.player(), 'playerVehicleID', 0)
    except Exception:
        return 0


def _removeOwnMarker():
    """Над собственной машиной игрока маркера (ник, HP) быть не должно."""
    if not MANAGE_MARKERS:
        return
    try:
        own = BigWorld.entity(_ownID())
        if own is None:
            return
        marker = getattr(own, 'marker', None)
        if marker in (None, -1):
            return
        bw = _battleWindow()
        vm = getattr(bw, 'vMarkersManager', None) if bw is not None else None
        if vm is not None:
            _destroyMarkerSafe(vm, marker)
            own.marker = None
            LOG_DEBUG('[SPOT] removed marker above own vehicle')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _minimap():
    bw = _battleWindow()
    return getattr(bw, 'minimap', None) if bw is not None else None


def _mmHide(vid):
    """Полностью убирает метку с миникарты: враг ещё ни разу не был засвечен, его там быть не должно.
    (notifyVehicleStop оставил бы метку в 'последней известной позиции' - на его стартовой точке.)"""
    mm = _minimap()
    if mm is None:
        return
    try:
        waiting = getattr(mm, '_Minimap__vehiclesWaitStart', None)
        if waiting is not None and vid in waiting:
            waiting.remove(vid)          # метка ещё не создана, но создалась бы при старте миникарты
    except Exception:
        pass
    try:
        entries = getattr(mm, '_Minimap__entries', None)
        if entries is not None and vid in entries:
            mm._Minimap__delEntry(vid)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _setShown(v, show):
    if v.id == _ownID():
        return                           # своя машина: засвет и маркеры не нужны
    if not _alive(v):
        return                           # погибших засвет не трогает (трупы и их метки - как в v0.5)
    prev = _SHOWN.get(v.id)
    _setModelShown(v, show)
    _syncMarker(v, show)

    if show:
        _EVER_SEEN.add(v.id)

    mm = _minimap()
    try:
        if mm is not None:
            if show:
                if prev is not None and prev != show:
                    mm.notifyVehicleStart(v.id)
            elif v.id in _EVER_SEEN:
                # потеряли из виду: метка остаётся в последней известной позиции
                if prev is not None and prev != show:
                    mm.notifyVehicleStop(v.id)
            else:
                # ни разу не видели: на миникарте его нет вообще (проверяем на каждом тике:
                # метка может появиться позже - из очереди старта миникарты или по событию арены)
                _mmHide(v.id)
    except Exception:
        LOG_CURRENT_EXCEPTION()

    if prev is not None and prev != show:
        LOG_DEBUG('[SPOT] vehicle %s %s' % (v.id, 'spotted' if show else 'lost'))
    _SHOWN[v.id] = show


def _wantShown(v, team, ptTeam, now):
    if team == ptTeam or team == 0:
        return True
    if _alive(v):
        return _SEEN.get((ptTeam, v.id), 0.0) > now
    return v.id in _REVEALED


def _refreshOne(vid):
    try:
        v = BigWorld.entity(vid)
        if v is None or not getattr(v, 'isStarted', False):
            return
        from gui.mods.offhangar import bots
        pt = _playerTeam()
        _setShown(v, _wantShown(v, bots.getTeam(v), pt, BigWorld.time()))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def onBotReady(vehicle):
    """Только что созданный бот: вражеские сразу скрываем (до первой проверки засвета)."""
    if not _ACTIVE or not _ON:
        return
    try:
        from gui.mods.offhangar import bots
        pt = _playerTeam()
        t = bots.getTeam(vehicle)
        if pt and t and t != pt:
            _setShown(vehicle, False)
            _SHOWN[vehicle.id] = False
            _mmHide(vehicle.id)
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _dumpMarkers(dead, vehicles):
    """Диагностика меток: у погибшей машины - её handle метки и позиция; рядом - все живые машины с их handle (ищем чужую метку над трупом)."""
    try:
        def pos(x):
            m = getattr(x, 'model', None)
            p = m.position if m is not None else x.position
            return (round(p.x, 1), round(p.y, 1), round(p.z, 1))
        LOG_DEBUG('[MARKER] DEAD %s marker=%s pos=%s hidden=%s' % (dead.id, getattr(dead, 'marker', None), pos(dead), dead.id in _HIDDEN_MARKERS))
        dp = pos(dead)
        for o in vehicles:
            if o.id == dead.id:
                continue
            op = pos(o)
            if abs(op[0] - dp[0]) < 25.0 and abs(op[2] - dp[2]) < 25.0:
                LOG_DEBUG('[MARKER]   near: %s alive=%s marker=%s pos=%s' % (o.id, _alive(o), getattr(o, 'marker', None), op))
        seen = {}
        for o in vehicles:
            h = getattr(o, 'marker', None)
            if h not in (None, -1):
                seen.setdefault(h, []).append(o.id)
        dup = dict((h, ids) for h, ids in seen.items() if len(ids) > 1)
        LOG_DEBUG('[MARKER] handles: %d vehicles with marker, duplicates: %s' % (len(seen), dup))
    except Exception:
        LOG_CURRENT_EXCEPTION()


def _tick(token):
    if token != _TOKEN or not _ACTIVE:
        return
    BigWorld.callback(TICK / PARTS, lambda: _tick(token))
    part = _PART[0]
    _PART[0] = (part + 1) % PARTS
    try:
        from gui.mods.offhangar import battle as off_battle
        if not off_battle._active():
            return
        now = BigWorld.time()
        vehicles = _vehicles()
        pt = _playerTeam()
        if not pt:
            return
        dbg = any(now - t0 < 3.0 for t0 in _DEAD_AT.values())     # недавно кто-то погиб - пишем хлебные крошки
        if dbg:
            LOG_DEBUG('[SPOTDBG] tick begin (%d vehicles)' % len(vehicles))
        if part == 0:
            _DEADSEEN.clear()
        _DEADSEEN.update(_computeSeen(vehicles, now, part, PARTS))
        if part != PARTS - 1:
            return                       # остальную работу (показ/скрытие, метки) делаем раз в TICK, на последней порции
        deadSeen = set(_DEADSEEN)
        if dbg:
            LOG_DEBUG('[SPOTDBG] computeSeen done')
        _removeOwnMarker()
        for v in vehicles:
            try:
                from gui.mods.offhangar import bots
                team = bots.getTeam(v)
                if not _alive(v):
                    # Погибшая машина: засвет её не трогает - труп виден всегда, метка родная (как в v0.5).
                    t0 = _DEAD_AT.get(v.id)
                    if t0 is None:
                        _DEAD_AT[v.id] = now
                        LOG_DEBUG('[DEATH] spotting: vehicle %s died (corpse and marker left to the client)' % v.id)
                        _dumpMarkers(v, vehicles)
                    elif now - t0 >= DEAD_QUIET and v.id not in _DEAD_RESTORED:
                        _DEAD_RESTORED.add(v.id)
                        if _SHOWN.get(v.id) is False:
                            _setModelShown(v, True)      # враг погиб скрытым - показываем остов
                    continue
                _setShown(v, _wantShown(v, team, pt, now))
            except Exception:
                LOG_CURRENT_EXCEPTION()
        if dbg:
            LOG_DEBUG('[SPOTDBG] tick end')
    except Exception:
        LOG_CURRENT_EXCEPTION()


def start():
    global _ACTIVE, _TOKEN, _ON
    _TOKEN += 1
    _SEEN.clear()
    _SHOWN.clear()
    _REVEALED.clear()
    _EVER_SEEN.clear()
    _VIEW_CACHE.clear()
    _DEAD_AT.clear()
    _DEAD_MARKER_DONE.clear()
    _DEAD_RESTORED.clear()
    _HIDDEN_MARKERS.clear()
    try:
        from gui.mods.offhangar import _constants as _c
        val = str(_c.readOfflineSettings().get('spotting', '1')).strip().lower()
        _ON = ENABLED and val not in ('0', 'off', 'false', 'no', 'нет', 'выкл')
    except Exception:
        _ON = ENABLED
    _ACTIVE = True
    LOG_NOTE('[SPOT] spotting %s' % ('enabled' if _ON else 'disabled'))
    if _ON:
        _PART[0] = 0
        BigWorld.callback(TICK / PARTS, lambda t=_TOKEN: _tick(t))


def stop():
    global _ACTIVE, _TOKEN
    _ACTIVE = False
    _TOKEN += 1
    _SEEN.clear()
    _SHOWN.clear()
    _REVEALED.clear()
    _EVER_SEEN.clear()
    _VIEW_CACHE.clear()
    _HIDDEN_MARKERS.clear()
