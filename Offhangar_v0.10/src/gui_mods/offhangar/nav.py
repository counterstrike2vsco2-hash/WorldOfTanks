# -*- coding: utf-8 -*-
"""Карта проходимости для ботов: сетка клеток поверх карты + поиск пути A*.

Зачем: локальный объезд (лучи вперёд на 12-20 м) не видит тупиков между домами - боты заезжали в карманы и застревали.
Здесь карта разбивается на клетки (~8-10 м); клетка считается свободной, если в ней можно поставить корпус
(нет домов, стен, крыш, крутых склонов, глубокой воды). Бот строит путь по свободным клеткам в обход домов/воды
и едет по узлам пути. Разрушаемые объекты (заборы) сеткой не считаются препятствием: их бот ломает, как и раньше.
Модуль не зависит от BigWorld: пробу клетки (probe) и планировщик задач (schedule) передаёт вызывающий код.
"""
import math
import heapq
import time

try:
    _range = xrange
except NameError:
    _range = range

CELL_MIN = 8.0            # минимальный размер клетки, м
MAX_CELLS = 6400          # лимит клеток (иначе клетка растёт)
WATER_MAX_DEPTH = 1.0     # вода глубже - непроходима, м
BUILD_BUDGET = 0.004      # сек работы за один шаг построения
BUILD_STEP = 0.04         # пауза между шагами построения, сек
LEARN_TTL = 45.0          # сколько секунд "выученное" препятствие (место застревания) считается стеной
PLAN_GAP = 0.25           # не чаще одного поиска пути за этот промежуток (сглаживает пики нагрузки)
MAX_EXPANSIONS = 2200     # предел раскрытых клеток в одном поиске

FREE, BLOCK = 1, 2        # 0 - ещё не проверена (считается свободной)

_SQ2 = 1.41421356
NOISE = 0.0027             # случайная добавка к стоимости клетки (у каждого бота своя): пути ботов расходятся, а не идут колонной
HW = 1.8                  # вес эвристики (взвешенный A*: путь чуть длиннее, но поиск в разы быстрее)


class NavGrid(object):
    def __init__(self):
        self.reset()

    def reset(self):
        self.token = getattr(self, 'token', 0) + 1
        self.ready = False
        self.W = self.H = 0
        self.cell = 10.0
        self.x0 = self.z0 = 0.0
        self.cells = bytearray()
        self.done = 0
        self.total = 0
        self.learned = {}
        self.nextPlanAt = 0.0
        self.plans = 0
        self.planTime = 0.0
        self.noPath = 0

    # ------------------------------------------------------------------ построение
    def start(self, bbox, probe, schedule, log=None, now=time.time):
        """bbox = (x0, z0, x1, z1) в метрах; probe(x, z) -> True, если клетка проходима; schedule(delay, fn) - отложенный вызов."""
        self.reset()
        x0, z0, x1, z1 = bbox
        w = max(40.0, x1 - x0)
        h = max(40.0, z1 - z0)
        cell = max(CELL_MIN, math.sqrt(w * h / float(MAX_CELLS)))
        self.cell = cell
        self.x0, self.z0 = x0, z0
        self.W = int(math.ceil(w / cell))
        self.H = int(math.ceil(h / cell))
        self.total = self.W * self.H
        self.cells = bytearray(self.total)
        token = self.token
        t0 = now()

        def _step():
            if token != self.token:
                return
            end = now() + BUILD_BUDGET
            cells = self.cells
            W = self.W
            cell = self.cell
            while self.done < self.total:
                idx = self.done
                i = idx % W
                j = idx // W
                try:
                    ok = probe(self.x0 + (i + 0.5) * cell, self.z0 + (j + 0.5) * cell)
                except Exception:
                    ok = True
                cells[idx] = 0 if ok is None else (FREE if ok else BLOCK)
                self.done += 1
                if now() >= end:
                    break
            if self.done >= self.total:
                nb = sum(1 for c in cells if c == BLOCK)
                self.ready = nb < 0.8 * self.total       # почти всё заблокировано - карта негодна, боты ездят как раньше
                if log is not None:
                    blocked = sum(1 for c in cells if c == BLOCK)
                    log('[NAV] grid ready: %dx%d cells of %.1f m, blocked %d (%.0f%%), %.1f s' % (
                        self.W, self.H, self.cell, blocked, 100.0 * blocked / max(1, self.total), now() - t0))
                return
            schedule(BUILD_STEP, _step)
        schedule(BUILD_STEP, _step)

    # ------------------------------------------------------------------ клетки
    def cellOf(self, x, z):
        i = int((x - self.x0) / self.cell)
        j = int((z - self.z0) / self.cell)
        return (min(max(i, 0), self.W - 1), min(max(j, 0), self.H - 1))

    def center(self, i, j):
        return (self.x0 + (i + 0.5) * self.cell, self.z0 + (j + 0.5) * self.cell)

    def blocked(self, i, j, now=0.0):
        if i < 0 or j < 0 or i >= self.W or j >= self.H:
            return True
        idx = j * self.W + i
        if self.cells[idx] == BLOCK:
            return True
        if self.learned:
            exp = self.learned.get(idx)
            if exp is not None:
                if exp > now:
                    return True
                del self.learned[idx]
        return False

    def markStuck(self, x, z, now):
        """Бот застрял здесь: клетку (и соседей по курсу) временно считаем стеной - следующий путь пойдёт в обход."""
        if not self.ready:
            return
        i, j = self.cellOf(x, z)
        self.learned[j * self.W + i] = now + LEARN_TTL
        if len(self.learned) > 400:
            for k in [k for k, v in self.learned.items() if v <= now]:
                del self.learned[k]

    def nearestFree(self, i, j, now, maxR=4):
        if not self.blocked(i, j, now):
            return (i, j)
        for r in _range(1, maxR + 1):
            best = None
            for di in _range(-r, r + 1):
                for dj in _range(-r, r + 1):
                    if max(abs(di), abs(dj)) != r:
                        continue
                    if not self.blocked(i + di, j + dj, now):
                        d = di * di + dj * dj
                        if best is None or d < best[0]:
                            best = (d, i + di, j + dj)
            if best is not None:
                return (best[1], best[2])
        return None

    def _los(self, a, b, now):
        """Прямая видимость между центрами клеток a и b по сетке (свободны все клетки вдоль отрезка и их соседи по бокам)."""
        (ax, az), (bx, bz) = a, b
        dx, dz = bx - ax, bz - az
        dist = math.hypot(dx, dz)
        if dist < 1e-6:
            return True
        n = int(dist / (self.cell * 0.34)) + 1
        px, pz = -dz / dist, dx / dist           # поперёк отрезка
        side = self.cell * 0.30
        for k in _range(1, n):
            t = k / float(n)
            x, z = ax + dx * t, az + dz * t
            for off in (0.0, side, -side):
                i, j = self.cellOf(x + px * off, z + pz * off)
                if self.blocked(i, j, now):
                    return False
        return True

    # ------------------------------------------------------------------ поиск пути
    def canPlan(self, now):
        return self.ready and now >= self.nextPlanAt

    def plan(self, sx, sz, gx, gz, now, seed=0):
        """Путь [(x, z), ...] от (sx, sz) до (gx, gz) в обход препятствий. None - пути нет/не нужен (ехать напрямую)."""
        if not self.ready:
            return None
        self.nextPlanAt = now + PLAN_GAP
        t0 = time.time()
        try:
            return self._plan(sx, sz, gx, gz, now, seed)
        finally:
            dt = time.time() - t0
            self.plans += 1
            self.planTime += dt
            self.nextPlanAt = now + max(PLAN_GAP, dt * 25.0)      # дорогой поиск - долгая пауза до следующего (держим нагрузку ~4%)

    def _plan(self, sx, sz, gx, gz, now, seed=0):
        si, sj = self.cellOf(sx, sz)
        gi, gj = self.cellOf(gx, gz)
        goalOrig = (gi, gj)
        s = self.nearestFree(si, sj, now, 3)
        g = self.nearestFree(gi, gj, now, 5)
        if s is None or g is None:
            self.noPath += 1
            return None
        si, sj = s
        gi, gj = g
        W = self.W
        start = sj * W + si
        goal = gj * W + gi
        if start == goal:
            return [(gx, gz)] if g == goalOrig else [self.center(gi, gj)]

        def h(i, j):
            dx, dz = abs(i - gi), abs(j - gj)
            return (dx + dz) + (_SQ2 - 2.0) * min(dx, dz)

        grid = bytearray(self.cells)
        for k, until in self.learned.items():
            if until > now and 0 <= k < len(grid):
                grid[k] = BLOCK
        Wd, Hd = self.W, self.H

        def bl(i, j, _n=0.0):
            return i < 0 or j < 0 or i >= Wd or j >= Hd or grid[j * Wd + i] == BLOCK

        gScore = {start: 0.0}
        came = {}
        openHeap = [(HW * h(si, sj), start)]
        closed = set()
        expansions = 0
        found = False
        nb = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
              (1, 1, _SQ2), (1, -1, _SQ2), (-1, 1, _SQ2), (-1, -1, _SQ2))
        blocked = bl
        while openHeap:
            f, cur = heapq.heappop(openHeap)
            if cur in closed:
                continue
            if cur == goal:
                found = True
                break
            closed.add(cur)
            expansions += 1
            if expansions > MAX_EXPANSIONS:
                break
            ci, cj = cur % W, cur // W
            gc = gScore[cur]
            for di, dj, cost in nb:
                ni, nj = ci + di, cj + dj
                if blocked(ni, nj, now):
                    continue
                if di and dj and (blocked(ci + di, cj, now) or blocked(ci, cj + dj, now)):
                    continue                       # по диагонали между двумя углами не пролезаем
                nidx = nj * W + ni
                if nidx in closed:
                    continue
                # клетки у стен дороже: путь держится подальше от домов
                pen = 0.0
                if blocked(ni + 1, nj, now) or blocked(ni - 1, nj, now) or blocked(ni, nj + 1, now) or blocked(ni, nj - 1, now):
                    pen = 0.45
                ng = gc + cost + pen + ((((nidx * 2654435761) ^ (seed * 40503)) >> 3) & 255) * NOISE
                if ng < gScore.get(nidx, 1e18):
                    gScore[nidx] = ng
                    came[nidx] = cur
                    heapq.heappush(openHeap, (ng + HW * h(ni, nj), nidx))
        if not found:
            self.noPath += 1
            return None
        cellsPath = [goal]
        while cellsPath[-1] != start:
            cellsPath.append(came[cellsPath[-1]])
        cellsPath.reverse()
        pts = [self.center(c % W, c // W) for c in cellsPath]
        pts[0] = (sx, sz)
        # спрямление: от текущей точки сразу к самой дальней видимой
        out = []
        a = 0
        last = len(pts) - 1
        while a < last:
            b = a + 1
            lim = min(last, a + 14)
            while b < lim and self._los(pts[a], pts[b + 1], now):
                b += 1
            out.append(pts[b])
            a = b
        if out and g == goalOrig:
            out[-1] = (gx, gz)
        return out


NAV = NavGrid()
