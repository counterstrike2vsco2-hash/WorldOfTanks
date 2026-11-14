# -*- coding: utf-8 -*-
"""Experimental forced battle-tier generator for Python 2.6.

The player's vehicle occupies one slot but is deliberately absent from every
balance constraint and score. The stable generator is not modified or called.
"""
import itertools
import random
import time


def _core():
    # CameraNode registers the full module name but does not add an attribute
    # to the gui.mods parent package.
    return __import__('gui.mods.offhangar.mm_core', fromlist=['*'])


def validate_forced_config(cfg):
    settings = cfg.get('experimental_modes', {}).get('forced_battle_tier', {})
    if not isinstance(settings, dict):
        raise ValueError('forced_battle_tier must be an object')
    if not isinstance(settings.get('enabled', False), bool):
        raise ValueError('forced_battle_tier.enabled must be boolean')
    tiers = settings.get('battle_tiers', [])
    if not isinstance(tiers, list) or not tiers:
        raise ValueError('forced battle_tiers must be a non-empty list')
    if any(not isinstance(tier, int) or isinstance(tier, bool) or tier < 1 or tier > 12
           for tier in tiers):
        raise ValueError('forced battle tier outside 1..12')
    if len(tiers) != len(set(tiers)):
        raise ValueError('duplicate forced battle tier')
    return settings


def _guest_player():
    return {'group': 'ordinary', 'level': 0, 'vehicle_class': 'guest', 'name': '__guest__'}


def _group_options(pool, group, cfg, a_size, b_size, deadline):
    core = _core()
    levels = sorted(set(row['level'] for row in pool if row['group'] == group), reverse=True)
    totals = [sum(row['group'] == group and row['level'] == level for row in pool)
              for level in levels]
    ranges = []
    for index, total in enumerate(totals):
        choices = list(range(total + 1))
        if group == 'ordinary':
            bound = cfg['balance']['tier_differences'][min(index, 2)]
            choices = [count for count in choices if abs(2 * count - total) <= bound]
            if index < 2:
                choices = [count for count in choices if count > 0 and total - count > 0]
        ranges.append(choices)
    result = {}
    allocations = 0
    for allocation in itertools.product(*ranges):
        core.check_time(deadline)
        a_count = sum(allocation)
        b_count = sum(totals) - a_count
        if a_count > a_size or b_count > b_size:
            continue
        if group != 'ordinary':
            cap = core.cap_for(group, _guest_player(), cfg, max(a_size, b_size))
            if (max(a_count, b_count) > cap or abs(a_count - b_count) > 1 or
                    (a_count == 0) != (b_count == 0)):
                continue
            if a_count + b_count == 2 and len(levels) != 1:
                continue
        else:
            valid = True
            for side in (allocation, [total - count for total, count in zip(totals, allocation)]):
                present = [level for level, count in zip(levels, side) if count]
                if present and max(present) - min(present) + 1 != len(present):
                    valid = False
            if not valid:
                continue
        allocations += 1
        if allocations > cfg['search']['max_allocations']:
            raise core.SearchLimit('forced allocation memory budget reached')
        result.setdefault(a_count, []).append(dict(zip(levels, allocation)))
    return result


def hard_errors_guest(a, b, cfg, a_size=14, b_size=15):
    core = _core()
    errors = []
    if len(a) != a_size or len(b) != b_size:
        errors.append('guest team size')
    guest = _guest_player()
    for group in ('spg', 'scout'):
        aa = [row for row in a if row['group'] == group]
        bb = [row for row in b if row['group'] == group]
        cap = core.cap_for(group, guest, cfg, max(a_size, b_size))
        if max(len(aa), len(bb)) > cap:
            errors.append(group + ' cap')
        if abs(len(aa) - len(bb)) > 1 or (not aa) != (not bb):
            errors.append(group + ' presence/count')
        if len(aa) + len(bb) == 2 and len(set(row['level'] for row in aa + bb)) != 1:
            errors.append(group + ' pair level')
    levels = sorted(set(row['level'] for row in a + b if row['group'] == 'ordinary'), reverse=True)
    for index, level in enumerate(levels):
        na = sum(row['group'] == 'ordinary' and row['level'] == level for row in a)
        nb = sum(row['group'] == 'ordinary' and row['level'] == level for row in b)
        if abs(na - nb) > cfg['balance']['tier_differences'][min(index, 2)]:
            errors.append('tier difference')
        if index < 2 and (not na or not nb):
            errors.append('upper/middle presence')
    for side in (a, b):
        present = set(row['level'] for row in side if row['group'] == 'ordinary')
        if present and max(present) - min(present) + 1 != len(present):
            errors.append('tier gap')
    return errors


def _partition(pool, cfg, rng, deadline, a_size=14, b_size=15):
    core = _core()
    options = dict((group, _group_options(pool, group, cfg, a_size, b_size, deadline))
                   for group in ('ordinary', 'spg', 'scout'))
    sizes = [(ordinary, spg, a_size - ordinary - spg)
             for ordinary in options['ordinary'] for spg in options['spg']
             if a_size - ordinary - spg in options['scout']]
    if not sizes:
        raise core.InvalidPool('no forced allocation satisfies hard rules')
    best = None
    for unused in range(cfg['search']['restarts']):
        core.check_time(deadline)
        quantities = rng.choice(sizes)
        target = {}
        for group, count in zip(('ordinary', 'spg', 'scout'), quantities):
            allocation = rng.choice(options[group][count])
            for level, number in allocation.items():
                target[group, level] = number
        a, b = [], []
        rest = list(pool)
        rng.shuffle(rest)
        for row in rest:
            key = row['group'], row['level']
            if target.get(key, 0) > 0:
                a.append(row)
                target[key] -= 1
            else:
                b.append(row)
        current = core.score(a, b, cfg)
        for iteration in range(cfg['search']['swap_passes']):
            core.check_time(deadline)
            improved = None
            candidate_score = current
            seen = set()
            for i in range(a_size):
                for j in range(b_size):
                    core.check_time(deadline)
                    key = (a[i]['group'], a[i]['level'], a[i]['vehicle_class'],
                           b[j]['group'], b[j]['level'], b[j]['vehicle_class'])
                    if key in seen or key[:3] == key[3:]:
                        continue
                    seen.add(key)
                    a[i], b[j] = b[j], a[i]
                    if not hard_errors_guest(a, b, cfg, a_size, b_size):
                        proposed = core.score(a, b, cfg)
                        if proposed < candidate_score - 1e-9:
                            improved, candidate_score = (i, j), proposed
                    a[i], b[j] = b[j], a[i]
            if improved is None:
                break
            i, j = improved
            a[i], b[j] = b[j], a[i]
            current = candidate_score
        if best is None or current < best[0]:
            best = current, a, b
    return best


def _repair(pool, eligible, cfg, rng):
    core = _core()
    guest = _guest_player()
    for group in ('spg', 'scout'):
        indices = [index for index, row in enumerate(pool) if row['group'] == group]
        if len(indices) == 1:
            ordinary = [index for index, row in enumerate(pool) if row['group'] == 'ordinary']
            if ordinary:
                index = rng.choice(ordinary)
                matching = [row for row in eligible if row['group'] == group and
                            row['level'] == pool[indices[0]]['level']]
                remaining = pool[:index] + pool[index + 1:]
                pool[index] = core.weighted_pick(matching, core.counts_by_group(remaining),
                                                 guest, cfg, 15, rng)
                return
        if len(indices) == 2 and pool[indices[0]]['level'] != pool[indices[1]]['level']:
            index = rng.choice(indices)
            other = indices[0] if index == indices[1] else indices[1]
            matching = [row for row in eligible if row['group'] == group and
                        row['level'] == pool[other]['level']]
            remaining = pool[:index] + pool[index + 1:]
            pool[index] = core.weighted_pick(matching, core.counts_by_group(remaining),
                                             guest, cfg, 15, rng)
            return
    index = rng.randrange(len(pool))
    remaining = pool[:index] + pool[index + 1:]
    pool[index] = core.weighted_pick(eligible, core.counts_by_group(remaining),
                                     guest, cfg, 15, rng)


def generate_forced(catalog, cfg, player_name, seed=None, battle_tier=None, size=15):
    core = _core()
    if size != 15:
        raise ValueError('forced mode requires 15-player teams')
    settings = validate_forced_config(cfg)
    if not settings.get('enabled', False):
        raise ValueError('forced mode is disabled')
    if player_name not in catalog or player_name in cfg['excluded']:
        raise ValueError('player vehicle excluded or unknown')
    started = time.time()
    deadline = started + cfg['search']['max_seconds']
    rng = random.Random(seed)
    tiers = settings['battle_tiers']
    bt = rng.choice(tiers) if battle_tier is None else battle_tier
    if bt not in tiers:
        raise ValueError('battle tier not enabled for forced mode')
    eligible = [catalog[name] for name in sorted(catalog)
                if name not in cfg['excluded'] and bt in catalog[name]['battle_tiers']]
    if not eligible:
        raise core.InvalidPool('no vehicles for forced battle tier %s' % bt)
    guest = _guest_player()
    repairs = 0
    for attempt in range(cfg['search']['pool_attempts']):
        core.check_time(deadline)
        pool = []
        while len(pool) < 2 * size - 1:
            pool.append(core.weighted_pick(eligible, core.counts_by_group(pool),
                                           guest, cfg, size, rng))
        initial = [row['name'] for row in pool]
        for local in range(cfg['search']['repairs'] + 1):
            try:
                result_score, a, b = _partition(pool, cfg, rng, deadline)
                errors = hard_errors_guest(a, b, cfg)
                if errors:
                    raise AssertionError(errors)
                return dict(battle_tier=bt, player=player_name, seed=seed,
                            teams=[[player_name] + [row['name'] for row in a],
                                   [row['name'] for row in b]],
                            initial_pool=initial, final_pool=[row['name'] for row in pool],
                            score=result_score, repairs=repairs,
                            pool_attempts=attempt + 1, elapsed=time.time() - started,
                            forced=True)
            except core.InvalidPool:
                if local < cfg['search']['repairs']:
                    _repair(pool, eligible, cfg, rng)
                    repairs += 1
    raise core.InvalidPool('forced pool repairs exhausted at battle tier %s' % bt)
