# -*- coding: utf-8 -*-
"""Pure Python 2.6-compatible roster engine; no client imports or side effects."""
import itertools
import math
import random
import time


class SearchLimit(Exception):
    pass


class InvalidPool(Exception):
    pass


def check_time(deadline):
    if time.time() >= deadline:
        raise SearchLimit('search deadline reached; infeasibility NOT proven')


def validate_config(cfg):
    if cfg.get('schema_version') != 5:
        raise ValueError('unsupported config schema')
    def number(value, minimum=0):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < minimum or math.isnan(value) or math.isinf(value):
            raise ValueError('invalid numeric configuration value')
    for group in ('spg', 'scout'):
        settings = cfg['selection'][group]
        if not isinstance(settings['cap'], int) or settings['cap'] < 0:
            raise ValueError('invalid hard cap')
        if len(settings['weights']) < 2 or len(settings['weights']) % 2:
            raise ValueError('saturation sequence needs complete pairs')
        for weight in settings['weights']:
            number(weight)
        number(settings['tail_decay'])
        if settings['tail_decay'] > 1:
            raise ValueError('tail decay must not exceed one')
    for key in ('class_weights', 'vehicle_weights'):
        for weight in cfg['selection'][key].values():
            number(weight)
    vehicle_classes = set(('heavyTank', 'mediumTank', 'AT-SPG', 'lightTank', 'SPG'))
    if set(cfg['selection']['class_weights']) != vehicle_classes:
        raise ValueError('selection class_weights must list every vehicle class')
    scouts = cfg['scouts']
    if not isinstance(scouts.get('vehicles'), list):
        raise ValueError('scout vehicles must be a list')
    scout_names = scouts['vehicles']
    if len(scout_names) != len(set(scout_names)):
        raise ValueError('duplicate scout vehicle')
    scout_exceptions = scouts.get('exceptions', {})
    if not isinstance(scout_exceptions, dict):
        raise ValueError('scout exceptions must be an object')
    if set(scout_exceptions) - set(scout_names):
        raise ValueError('scout exception is not in scout vehicles')
    if set(scout_names).intersection(cfg['overrides']):
        raise ValueError('scout also present in general overrides')
    for table in (scouts['offsets_by_level'], scout_exceptions):
        for offsets in table.values():
            if not isinstance(offsets, list) or not offsets or any(not isinstance(x, int) for x in offsets):
                raise ValueError('invalid scout offsets')
    spg = cfg['spg']
    spg_exceptions = spg.get('exceptions', {})
    if not isinstance(spg_exceptions, dict):
        raise ValueError('SPG exceptions must be an object')
    for table in (spg['offsets_by_level'], spg_exceptions):
        for offsets in table.values():
            if not isinstance(offsets, list) or not offsets or any(not isinstance(x, int) for x in offsets):
                raise ValueError('invalid SPG offsets')
    spg_conflicts = set(spg_exceptions).intersection(set(scout_names) | set(cfg['overrides']))
    if spg_conflicts:
        raise ValueError('conflicting SPG rules: ' + ', '.join(sorted(spg_conflicts)))
    tier_group_members = set()
    tier_groups = cfg.get('battle_tier_groups', {})
    if not isinstance(tier_groups, dict):
        raise ValueError('battle_tier_groups must be an object')
    for group_name, tier_group in tier_groups.items():
        if not isinstance(tier_group, dict):
            raise ValueError('invalid battle tier group: ' + group_name)
        offsets = tier_group.get('offsets')
        vehicles = tier_group.get('vehicles')
        if (not isinstance(offsets, list) or not offsets or
                any(not isinstance(x, int) for x in offsets)):
            raise ValueError('invalid battle tier group offsets: ' + group_name)
        if not isinstance(vehicles, list):
            raise ValueError('battle tier group vehicles must be a list: ' + group_name)
        for name in vehicles:
            if name in tier_group_members:
                raise ValueError('duplicate battle tier group membership: ' + name)
            tier_group_members.add(name)
    conflicts = tier_group_members.intersection(set(scout_names) | set(spg_exceptions) | set(cfg['overrides']))
    if conflicts:
        raise ValueError('conflicting battle tier rules: ' + ', '.join(sorted(conflicts)))
    seen = set()
    nation_weights = cfg['selection'].get('nation_weights', {})
    if not isinstance(nation_weights, dict):
        raise ValueError('nation_weights must be an object')
    for weight in nation_weights.values():
        number(weight)
    for group_name, group in cfg['selection'].get('rarity_groups', {}).items():
        number(group['weight'])
        if not isinstance(group['vehicles'], list):
            raise ValueError('rarity group vehicles must be a list: ' + group_name)
        for name in group['vehicles']:
            if name in seen:
                raise ValueError('duplicate rarity membership: ' + name)
            seen.add(name)
    settings = cfg['balance']
    number(settings['tier_growth'], 1.000001)
    for key in ('class_weights', 'special_scale'):
        for weight in settings[key].values():
            number(weight, 0.000001)
    for key in ('total_weight', 'class_weight', 'concentration_weight', 'special_weight'):
        number(settings[key])
    if len(settings['tier_differences']) != 3 or any(not isinstance(x, int) or x < 0 for x in settings['tier_differences']):
        raise ValueError('invalid tier differences')
    for key in ('pool_attempts', 'restarts', 'swap_passes', 'max_allocations'):
        if not isinstance(cfg['search'][key], int) or cfg['search'][key] < 1:
            raise ValueError('invalid search budget')
    number(cfg['search']['max_seconds'])
    if not isinstance(cfg['search']['repairs'], int) or cfg['search']['repairs'] < 0:
        raise ValueError('invalid repair budget')


def prepare_catalog(rows, cfg):
    validate_config(cfg)
    rarity = {}
    for group in cfg['selection'].get('rarity_groups', {}).values():
        for name in group['vehicles']:
            rarity[name] = group['weight']
    catalog = {}
    available_nations = set()
    scout_names = set(cfg['scouts']['vehicles'])
    scout_exceptions = cfg['scouts'].get('exceptions', {})
    spg_exceptions = cfg['spg'].get('exceptions', {})
    tier_group_offsets = {}
    for tier_group in cfg.get('battle_tier_groups', {}).values():
        for vehicle_name in tier_group['vehicles']:
            tier_group_offsets[vehicle_name] = tier_group['offsets']
    for source in rows:
        name = source['name']
        # Canonical client IDs include the nation; no fixed nation list.
        nation = name.split(':', 1)[0]
        available_nations.add(nation)
        if name in catalog:
            raise ValueError('duplicate catalog name: ' + name)
        row = dict(source)
        level, cls = int(row['level']), row['vehicle_class']
        override = cfg['overrides'].get(name, {})
        group = 'scout' if name in scout_names else override.get('group', 'spg' if cls == 'SPG' else 'ordinary')
        if group not in ('spg', 'scout', 'ordinary'):
            raise ValueError('unknown group: ' + name)
        offsets = cfg['default_offsets']
        if level <= cfg['low_tier_preferred_max'] and name not in cfg['low_tier_exceptions']:
            offsets = [0, 1]
        if cls == 'SPG':
            offsets = spg_exceptions.get(name, cfg['spg']['offsets_by_level'].get(str(level), cfg['default_offsets']))
        elif name in spg_exceptions:
            raise ValueError('SPG exception is not an SPG: ' + name)
        if name in tier_group_offsets:
            offsets = tier_group_offsets[name]
        if group == 'scout':
            if cls != 'lightTank':
                raise ValueError('scout is not a light tank: ' + name)
            if name in scout_exceptions:
                offsets = scout_exceptions[name]
            elif str(level) in cfg['scouts']['offsets_by_level']:
                offsets = cfg['scouts']['offsets_by_level'][str(level)]
            else:
                raise ValueError('missing scout level rule: ' + name)
        offsets = override.get('offsets', offsets)
        if 'battle_tiers' in override:
            tiers = override['battle_tiers']
        else:
            tiers = [level + x for x in offsets if 1 <= level + x <= 12]
        if not tiers or any(not isinstance(x, int) or x < 1 or x > 12 for x in tiers):
            raise ValueError('invalid battle tiers: ' + name)
        row.update(group=group, battle_tiers=sorted(set(tiers)), rarity_weight=rarity.get(name, 1.0))
        row['nation_weight'] = cfg['selection'].get('nation_weights', {}).get(nation, 1.0)
        catalog[name] = row
    unknown_nations = set(cfg['selection'].get('nation_weights', {})) - available_nations
    if unknown_nations:
        raise ValueError('unknown configured nations: ' + ', '.join(sorted(unknown_nations)))
    referenced = (set(cfg['excluded']) | set(cfg['overrides']) | set(scout_names) |
                  set(spg_exceptions) |
                  set(tier_group_offsets) |
                  set(cfg['selection']['vehicle_weights']) | set(rarity))
    missing = referenced - set(catalog)
    if missing:
        raise ValueError('unknown configured vehicles: ' + ', '.join(sorted(missing)))
    return catalog


def cap_for(group, player, cfg, size):
    settings = cfg['selection'][group]
    cap = settings['cap']
    if not isinstance(cap, int) or cap < 0:
        raise ValueError('cap must be a nonnegative integer')
    if settings.get('disabled', False):
        return 1 if player['group'] == group else 0
    return min(size, cap) if cap else size


def weighted_pick(eligible, counts, player, cfg, size, rng):
    weighted = []
    for row in eligible:
        weight = cfg['selection']['class_weights'].get(row['vehicle_class'], 1.0)
        weight *= cfg['selection']['vehicle_weights'].get(row['name'], row.get('rarity_weight', 1.0))
        weight *= row.get('nation_weight', 1.0)
        group = row['group']
        if group != 'ordinary':
            n = counts.get(group, 0)
            if n >= 2 * cap_for(group, player, cfg, size):
                continue
            settings = cfg['selection'][group]
            sequence = settings['weights']
            if n < len(sequence):
                weight *= sequence[n]
            else:
                # Continue alternating suppressed pair starts and pair completion.
                weight *= sequence[-2 + (n % 2)] * settings['tail_decay'] ** (1 + (n - len(sequence)) // 2)
        if weight < 0 or math.isnan(weight) or math.isinf(weight):
            raise ValueError('invalid selection weight')
        if weight:
            weighted.append((row, weight))
    total = sum(w for row, w in weighted)
    if not total:
        raise InvalidPool('no positive-weight eligible vehicle')
    target = rng.random() * total
    for row, weight in weighted:
        target -= weight
        if target < 0:
            return row
    return weighted[-1][0]


def counts_by_group(pool):
    counts = dict((g, 0) for g in ('ordinary', 'spg', 'scout'))
    for row in pool:
        counts[row['group']] += 1
    return counts


def group_options(pool, group, player, cfg, size, deadline):
    """Enumerate count allocations, not permutations of 30 individual vehicles.
    Player is pool[0], fixed on conceptual side A. Returns exact hard feasibility
    for this group unless the explicit time budget interrupts enumeration.
    """
    levels = sorted(set(r['level'] for r in pool if r['group'] == group), reverse=True)
    totals = [sum(r['group'] == group and r['level'] == lv for r in pool) for lv in levels]
    ranges = []
    for index, (lv, total) in enumerate(zip(levels, totals)):
        choices = list(range(total + 1))
        if group == 'ordinary':
            bound = cfg['balance']['tier_differences'][min(index, 2)]
            choices = [x for x in choices if abs(2 * x - total) <= bound]
            if index < 2:
                choices = [x for x in choices if x > 0 and total - x > 0]
        if player['group'] == group and player['level'] == lv:
            choices = [x for x in choices if x >= 1]
        ranges.append(choices)
    result = {}
    allocations = 0
    for allocation in itertools.product(*ranges):
        check_time(deadline)
        a, b = sum(allocation), sum(totals) - sum(allocation)
        if a > size or b > size:
            continue
        if group != 'ordinary':
            cap = cap_for(group, player, cfg, size)
            if max(a, b) > cap or abs(a - b) > 1 or (a == 0) != (b == 0):
                continue
            if a + b == 2 and len(levels) != 1:
                continue
        else:
            valid = True
            for side in (allocation, [n - x for n, x in zip(totals, allocation)]):
                present = [lv for lv, count in zip(levels, side) if count]
                if present and max(present) - min(present) + 1 != len(present):
                    valid = False
            if not valid:
                continue
        allocations += 1
        if allocations > cfg['search']['max_allocations']:
            raise SearchLimit('allocation memory budget reached; infeasibility NOT proven')
        result.setdefault(a, []).append(dict(zip(levels, allocation)))
    return result


def hard_errors(a, b, cfg, size, player):
    errors = []
    if len(a) != size or len(b) != size:
        errors.append('team size')
    for group in ('spg', 'scout'):
        aa = [r for r in a if r['group'] == group]
        bb = [r for r in b if r['group'] == group]
        if max(len(aa), len(bb)) > cap_for(group, player, cfg, size):
            errors.append(group + ' cap')
        if abs(len(aa) - len(bb)) > 1 or (not aa) != (not bb):
            errors.append(group + ' presence/count')
        if len(aa) + len(bb) == 2 and len(set(r['level'] for r in aa + bb)) != 1:
            errors.append(group + ' pair level')
    levels = sorted(set(r['level'] for r in a + b if r['group'] == 'ordinary'), reverse=True)
    for index, lv in enumerate(levels):
        na = sum(r['group'] == 'ordinary' and r['level'] == lv for r in a)
        nb = sum(r['group'] == 'ordinary' and r['level'] == lv for r in b)
        if abs(na - nb) > cfg['balance']['tier_differences'][min(index, 2)]:
            errors.append('tier difference')
        if index < 2 and (not na or not nb):
            errors.append('upper/middle presence')
    for side in (a, b):
        present = set(r['level'] for r in side if r['group'] == 'ordinary')
        if present and max(present) - min(present) + 1 != len(present):
            errors.append('tier gap')
    return errors


def value(row, cfg):
    settings = cfg['balance']
    group = row['group']
    if group == 'ordinary':
        base = settings['class_weights'][row['vehicle_class']]
        reference = settings['ordinary_reference_level']
    else:
        base = settings['special_scale'][group]
        reference = settings['special_reference_level'][group]
    return base * settings['tier_growth'] ** (row['level'] - reference)


def score(a, b, cfg):
    settings = cfg['balance']
    total = abs(sum(value(r, cfg) for r in a) - sum(value(r, cfg) for r in b))
    concentration = 0.0
    class_difference = 0
    for cls in ('heavyTank', 'mediumTank', 'AT-SPG', 'lightTank', 'SPG'):
        for group in ('ordinary', 'scout', 'spg'):
            aa = [r for r in a if r['vehicle_class'] == cls and r['group'] == group]
            bb = [r for r in b if r['vehicle_class'] == cls and r['group'] == group]
            class_difference += abs(len(aa) - len(bb))
            if aa or bb:
                top = max(r['level'] for r in aa + bb)
                concentration += abs(sum(r['level'] == top for r in aa) - sum(r['level'] == top for r in bb))
    special = sum(abs(sum(value(r, cfg) for r in a if r['group'] == g) -
                      sum(value(r, cfg) for r in b if r['group'] == g)) for g in ('spg', 'scout'))
    return (settings['total_weight'] * total + settings['class_weight'] * class_difference +
            settings['concentration_weight'] * concentration + settings['special_weight'] * special)


def partition(pool, cfg, size, rng, deadline):
    player = pool[0]
    options = dict((g, group_options(pool, g, player, cfg, size, deadline))
                   for g in ('ordinary', 'spg', 'scout'))
    sizes = [(o, s, size - o - s) for o in options['ordinary'] for s in options['spg']
             if size - o - s in options['scout']]
    if not sizes:
        raise InvalidPool('no allocation satisfies hard rules')
    best = None
    for unused in range(cfg['search']['restarts']):
        check_time(deadline)
        quantities = rng.choice(sizes)
        target = {}
        for group, count in zip(('ordinary', 'spg', 'scout'), quantities):
            allocation = rng.choice(options[group][count])
            for lv, n in allocation.items():
                target[group, lv] = n
        a, b = [player], []
        target[player['group'], player['level']] -= 1
        rest = list(pool[1:])
        rng.shuffle(rest)
        for row in rest:
            key = row['group'], row['level']
            if target.get(key, 0) > 0:
                a.append(row)
                target[key] -= 1
            else:
                b.append(row)
        current = score(a, b, cfg)
        for iteration in range(cfg['search']['swap_passes']):
            check_time(deadline)
            improved = None
            candidate_score = current
            seen = set()
            for i in range(1, size):  # the mandatory player is never swapped
                for j in range(size):
                    key = (a[i]['group'], a[i]['level'], a[i]['vehicle_class'],
                           b[j]['group'], b[j]['level'], b[j]['vehicle_class'])
                    if key in seen or key[:3] == key[3:]:
                        continue
                    seen.add(key)
                    check_time(deadline)
                    a[i], b[j] = b[j], a[i]
                    if not hard_errors(a, b, cfg, size, player):
                        proposed = score(a, b, cfg)
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


def repair(pool, eligible, cfg, size, rng):
    """Single replacement, prioritising an unpaired/unequal special group.
    Never replaces pool[0] (the player); no balance-score-based repairs.
    """
    for group in ('spg', 'scout'):
        indices = [i for i, r in enumerate(pool) if r['group'] == group]
        if len(indices) == 1:
            replace = [i for i in range(1, len(pool)) if pool[i]['group'] == 'ordinary']
            if replace:
                index = rng.choice(replace)
                matching = [r for r in eligible if r['group'] == group and r['level'] == pool[indices[0]]['level']]
                remaining = pool[:index] + pool[index + 1:]
                pool[index] = weighted_pick(matching, counts_by_group(remaining), pool[0], cfg, size, rng)
                return
        if len(indices) == 2 and pool[indices[0]]['level'] != pool[indices[1]]['level']:
            change = rng.choice([i for i in indices if i != 0])
            other = next(i for i in indices if i != change)
            matching = [r for r in eligible if r['group'] == group and r['level'] == pool[other]['level']]
            remaining = pool[:change] + pool[change + 1:]
            pool[change] = weighted_pick(matching, counts_by_group(remaining), pool[0], cfg, size, rng)
            return
    # General bounded repair for tier gaps/presence or coupled size constraints.
    index = rng.randrange(1, len(pool))
    remaining = pool[:index] + pool[index + 1:]
    pool[index] = weighted_pick(eligible, counts_by_group(remaining), pool[0], cfg, size, rng)


def generate(catalog, cfg, player_name, seed=None, battle_tier=None, size=15):
    if size < 1 or size > 15:
        raise ValueError('team size outside supported range 1..15')
    started = time.time()
    deadline = started + cfg['search']['max_seconds']
    rng = random.Random(seed)
    if player_name not in catalog or player_name in cfg['excluded']:
        raise ValueError('player vehicle excluded or unknown')
    player = catalog[player_name]
    bt = rng.choice(player['battle_tiers']) if battle_tier is None else battle_tier
    if bt not in player['battle_tiers']:
        raise ValueError('battle tier not permitted for player')
    eligible = [catalog[n] for n in sorted(catalog) if n not in cfg['excluded'] and bt in catalog[n]['battle_tiers']]
    repairs = 0
    for attempt in range(cfg['search']['pool_attempts']):
        check_time(deadline)
        pool = [player]
        while len(pool) < 2 * size:
            counts = counts_by_group(pool)
            pool.append(weighted_pick(eligible, counts, player, cfg, size, rng))
        initial = [r['name'] for r in pool]
        for local in range(cfg['search']['repairs'] + 1):
            try:
                result_score, a, b = partition(pool, cfg, size, rng, deadline)
                errors = hard_errors(a, b, cfg, size, player)
                if errors:
                    raise AssertionError(errors)
                return dict(battle_tier=bt, player=player_name, seed=seed,
                            teams=[[r['name'] for r in a], [r['name'] for r in b]],
                            initial_pool=initial, final_pool=[r['name'] for r in pool],
                            score=result_score, repairs=repairs, pool_attempts=attempt + 1,
                            elapsed=time.time() - started)
            except InvalidPool:
                if local < cfg['search']['repairs']:
                    repair(pool, eligible, cfg, size, rng)
                    repairs += 1
    raise InvalidPool('bounded pool repairs exhausted at battle tier %s' % bt)
