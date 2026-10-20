"""Offline matched-continuation accounting, independent of any scorer or controller.

All labels are provisional. Component parity is evidence of recorded equality,
not qualification of a simulator's restoration coverage. Version 1 can label CPU
fixtures; recorded robotics pairs remain unlabelled until that coverage is qualified.
"""
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal
import json
import math

from .chords import canonical, digest, object_path
from .mechanism import Frame, Tape
from .streams import read_small

MAX_PAIRS = 1000
MAX_EVIDENCE_BYTES = 8 * 1024**2
MAX_PROCESSING_BYTES = 32 * 1024**2
MAX_IDENTIFIER_BYTES = 1024
UTILITY_WEIGHTS = ('success', 'wall_second', 'actor_query', 'scorer_query', 'cost_unit', 'task_failure')


def _integer(value, minimum=0, maximum=10**15):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Bounded integer accounting required')


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10**12:
        raise ValueError('Finite nonnegative objective/accounting value required')


@dataclass(frozen=True)
class PairProtocol:
    initialization_group: str
    split: str
    boundary: int
    horizon: int
    query_ceiling: int
    actor_sha256: str
    continuation_sha256: str
    latency_condition_sha256: str
    input_view_sha256: str
    actor_seeds: tuple[int, ...]

    def __post_init__(self):
        if (not isinstance(self.initialization_group, str) or not self.initialization_group
                or len(self.initialization_group.encode()) > 256
                or self.split not in ('train', 'development', 'sealed_test')):
            raise ValueError('Explicit initialization group and split required')
        _integer(self.boundary, 0, 599)
        _integer(self.horizon, self.boundary + 1, 600)
        _integer(self.query_ceiling, 1, 128)
        for value in (self.actor_sha256, self.continuation_sha256,
                      self.latency_condition_sha256, self.input_view_sha256):
            object_path(value)
        if type(self.actor_seeds) is not tuple or len(self.actor_seeds) != self.query_ceiling:
            raise ValueError('Frozen per-query seed schedule for the full ceiling required')
        for value in self.actor_seeds:
            _integer(value, 0, 2**63 - 1)

    @property
    def identity(self):
        return digest(canonical(asdict(self)))


@dataclass(frozen=True)
class UtilityRecipe:
    """U = success*w - wall_seconds*w - submitted_calls*w - cost_units*w - failures*w.

    Callers explicitly choose all units/weights and the abstention margin. This
    scalar never replaces the raw success, boundary, time and call outcomes.
    """
    success: float
    wall_second: float
    actor_query: float
    scorer_query: float
    cost_unit: float
    task_failure: float
    margin: float
    cost_unit_name: str
    task_failure_definition_sha256: str

    def __post_init__(self):
        for name in (*UTILITY_WEIGHTS, 'margin'):
            _number(getattr(self, name))
        if self.success == 0:
            raise ValueError('Positive declared success weight required')
        if not isinstance(self.cost_unit_name, str) or not self.cost_unit_name.strip() or len(self.cost_unit_name.encode()) > 128:
            raise ValueError('Explicit bounded shared cost-unit name required')
        object_path(self.task_failure_definition_sha256)


@dataclass(frozen=True)
class Continuation:
    """Tape includes the prefix; resource accounting starts at the branch boundary.

    calls logs every submitted query, including failed/late calls. Times are
    integer nanoseconds relative to the branch boundary; cost_units use the same
    declared unit for both arms. Early queries consume the shared remaining cap.
    """
    tape: Tape
    initialization_group: str
    split: str
    protocol_sha256: str
    calls: tuple[dict, ...]
    wall_ns: int
    pause_ns: int
    success_wall_ns: int | None
    task_failures: int
    infrastructure_valid: bool


@dataclass(frozen=True)
class ReplayEvidence:
    uninterrupted: Tape
    restored: Tape
    coverage_sha256: str
    scope: str


def _bounded_tape_ids(episode_id, parent_episode_id):
    # Frame identities hash these strings per observation, including inherited frames.
    for value in (episode_id, parent_episode_id):
        if value is not None and (not isinstance(value, str) or not value or len(value.encode()) > MAX_IDENTIFIER_BYTES):
            raise ValueError('Bounded native tape identifier required')


def _tape_record(tape):
    if not isinstance(tape, Tape) or len(tape.actions) > 600:
        raise ValueError('Bounded audited native Tape required')
    _bounded_tape_ids(tape.episode_id, tape.parent_episode_id)
    return tape.record({})


def continuation_record(branch):
    """Retain this evidence object separately; a pair overlay binds its exact hash."""
    if not isinstance(branch, Continuation):
        raise ValueError('Explicit continuation evidence required')
    value = {key: value for key, value in vars(branch).items() if key != 'tape'}
    value.update(kind='matched_continuation', schema_version=1, tape=_tape_record(branch.tape))
    if len(canonical(value)) > MAX_EVIDENCE_BYTES:
        raise ValueError('Continuation evidence byte bound exceeded')
    return value


def _outcome(branch, protocol):
    record = continuation_record(branch)
    tape = branch.tape
    if (branch.initialization_group != protocol.initialization_group or branch.split != protocol.split
            or branch.protocol_sha256 != protocol.identity):
        raise ValueError('Continuation group/split or frozen protocol differs')
    if not protocol.boundary <= len(tape.actions) <= protocol.horizon:
        raise ValueError('Continuation lies outside the common remaining horizon')
    _integer(branch.wall_ns)
    _integer(branch.pause_ns, 0, branch.wall_ns)
    _integer(branch.task_failures, 0, 10000)
    if type(branch.infrastructure_valid) is not bool:
        raise ValueError('Explicit infrastructure validity required')
    if type(branch.calls) is not tuple or len(branch.calls) > 256:
        raise ValueError('Bounded submitted-query journal required')
    counts = Counter()
    costs = Counter()
    failed = False
    last_submission = -1
    for call in branch.calls:
        if not isinstance(call, dict) or set(call) != {'kind', 'control', 'started_ns', 'finished_ns', 'status', 'cost_units', 'seed'}:
            raise ValueError('Exact submitted-query journal schema required')
        kind = call['kind']
        if kind not in ('actor', 'scorer') or call['status'] not in ('completed', 'failed', 'timeout', 'submitted'):
            raise ValueError('Explicit query kind/status required')
        _integer(call['control'], protocol.boundary, len(tape.actions))
        _integer(call['started_ns'], 0, branch.wall_ns)
        if call['started_ns'] < last_submission:
            raise ValueError('Query journal must preserve submission order')
        last_submission = call['started_ns']
        if call['status'] == 'submitted':
            if call['finished_ns'] is not None:
                raise ValueError('Pending query cannot have a completion timestamp')
        else:
            _integer(call['finished_ns'], call['started_ns'], branch.wall_ns)
        _number(call['cost_units'])
        if kind == 'actor':
            if counts[kind] >= protocol.query_ceiling or type(call['seed']) is not int or call['seed'] != protocol.actor_seeds[counts[kind]]:
                raise ValueError('Actor query ceiling or frozen seed schedule differs')
        elif call['seed'] is not None:
            raise ValueError('Scorer must not consume the actor seed schedule')
        counts[kind] += 1
        costs[kind] += Decimal(str(call['cost_units']))
        failed |= call['status'] != 'completed'
    end = tape.frames[-1]
    if end.success:
        _integer(branch.success_wall_ns, 0, branch.wall_ns)
    elif branch.success_wall_ns is not None:
        raise ValueError('Time to success requires independent observed success')
    known = end.success or end.terminated or len(tape.actions) == protocol.horizon
    return {'episode_id': tape.episode_id, 'evidence_sha256': digest(canonical(record)),
            'success': end.success, 'terminated': end.terminated, 'truncated': end.truncated,
            'outcome_known': known, 'controls': len(tape.actions) - protocol.boundary,
            'capped_controls_to_success': len(tape.actions) - protocol.boundary if end.success else protocol.horizon - protocol.boundary,
            'wall_ns': branch.wall_ns, 'pause_ns': branch.pause_ns,
            'success_wall_ns': branch.success_wall_ns,
            'actor_queries': counts['actor'], 'scorer_queries': counts['scorer'],
            'actor_cost_units': float(costs['actor']), 'scorer_cost_units': float(costs['scorer']),
            'task_failures': branch.task_failures,
            'infrastructure_valid': branch.infrastructure_valid and not failed}


def _match(continuing, replanning, replay, protocol):
    if replay is None:
        return ['missing_replay_verification'], None
    record = replay_record(replay)
    records = [record['uninterrupted'], record['restored']]
    reference, restored = replay.uninterrupted, replay.restored
    reasons = []
    # A second observation alone does not test restoration: compare an actual
    # no-intervention suffix as well as every audited prefix component/action.
    if (len(reference.actions) <= protocol.boundary or reference.seed != restored.seed
            or reference.actions != restored.actions or reference.frames != restored.frames):
        reasons.append('no_intervention_replay_mismatch')
    for branch in (continuing, replanning):
        if (branch.tape.seed != reference.seed
                or branch.tape.actions[:protocol.boundary] != reference.actions[:protocol.boundary]
                or branch.tape.frames[:protocol.boundary + 1] != reference.frames[:protocol.boundary + 1]):
            reasons.append('branch_prefix_mismatch')
    if reference.frames[min(protocol.boundary, len(reference.actions))].stopped:
        reasons.append('stopped_decision_boundary')
    if replay.scope == 'recorded_replay':
        reasons.append('simulator_restoration_coverage_unqualified')
    evidence = {'scope': replay.scope, 'coverage_sha256': replay.coverage_sha256,
                'uninterrupted_sha256': digest(canonical(records[0])),
                'restored_sha256': digest(canonical(records[1]))}
    return reasons, evidence


def _utility(outcome, recipe):
    weights = {key: Decimal(str(getattr(recipe, key))) for key in UTILITY_WEIGHTS}
    return (weights['success'] * outcome['success'] - weights['wall_second'] * outcome['wall_ns'] / 10**9
            - weights['actor_query'] * outcome['actor_queries'] - weights['scorer_query'] * outcome['scorer_queries']
            - weights['cost_unit'] * (Decimal(str(outcome['actor_cost_units'])) + Decimal(str(outcome['scorer_cost_units'])))
            - weights['task_failure'] * outcome['task_failures'])


def evaluate_pair(continuing, replanning, *, protocol, recipe, replay=None, eligible=True,
                  ineligible_reason=None):
    """Reduce a single declared comparison; no model calls, training export or actuation.

    Missing replay, unknown prefixes, unqualified robotics coverage and failed
    infrastructure remain visible with null deltas/labels, never 'continue'.
    """
    if not isinstance(protocol, PairProtocol) or not isinstance(recipe, UtilityRecipe):
        raise ValueError('Frozen protocol and explicit objective recipe required')
    if type(eligible) is not bool or (eligible and ineligible_reason is not None) or (
            not eligible and (not isinstance(ineligible_reason, str) or not ineligible_reason or len(ineligible_reason) > 256)):
        raise ValueError('Runtime eligibility and exclusion reason must be separate from value')
    outcomes = {'continue': _outcome(continuing, protocol), 'replan': _outcome(replanning, protocol)}
    reasons, match = _match(continuing, replanning, replay, protocol)
    for arm, outcome in outcomes.items():
        if not outcome['outcome_known']:
            reasons.append(arm + '_outcome_unknown')
        if not outcome['infrastructure_valid']:
            reasons.append(arm + '_infrastructure_invalid')
    changed_indices = [i for i, (a, b) in enumerate(zip(continuing.tape.actions[protocol.boundary:],
                                                      replanning.tape.actions[protocol.boundary:]), protocol.boundary) if a != b]
    changed = len(changed_indices)
    if changed and not any(call['kind'] == 'actor' and call['status'] == 'completed'
                           and call['control'] <= changed_indices[0] for call in replanning.calls):
        reasons.append('changed_actions_without_actor_query')
    if not changed and any(outcomes['continue'][key] != outcomes['replan'][key]
                           for key in ('success', 'terminated', 'truncated', 'controls', 'task_failures')):
        reasons.append('outcomes_differ_without_changed_controls')
    reasons = sorted(set(reasons))
    valid = eligible and not reasons
    utilities = {arm: _utility(value, recipe) for arm, value in outcomes.items()} if valid else None
    delta = utilities['replan'] - utilities['continue'] if valid else None
    margin = Decimal(str(recipe.margin))
    label = None
    if delta is not None:
        label = 'replan' if delta > margin else 'continue' if delta < -margin else 'uncertain'
    record = {'kind': 'timing_outcome_pair', 'schema_version': 1,
              'initialization_group': protocol.initialization_group, 'split': protocol.split,
              'case_frame_id': continuing.tape.identity(protocol.boundary),
              'protocol': dict(asdict(protocol), actor_seeds=list(protocol.actor_seeds)), 'protocol_sha256': protocol.identity,
              'recipe': asdict(recipe), 'recipe_sha256': digest(canonical(asdict(recipe))),
              'branches': outcomes, 'replay_verification': match,
              'eligible': eligible, 'ineligible_reason': ineligible_reason,
              'invalid_reasons': reasons, 'valid': valid,
              'changed_controls': changed, 'handoff': 'changed_actions' if changed else 'no_handoff',
              'utilities': {arm: float(value) for arm, value in utilities.items()} if valid else None,
              'delta': float(delta) if valid else None, 'timing_label': label,
              'label_status': 'provisional_cpu_fixture' if valid else 'unlabelled',
              'robotics_benefit_established': False}
    return dict(record, pair_id=digest(canonical(record)))


def summarize(pairs):
    """All scheduled comparisons stay visible; count initialization groups, not views."""
    if not isinstance(pairs, (tuple, list)) or len(pairs) > MAX_PAIRS:
        raise ValueError('Bounded pair report required')
    groups, identities, reasons, labels = {}, set(), Counter(), Counter()
    total = 0
    for pair in pairs:
        raw = canonical(pair)
        total += len(raw)
        if total > MAX_EVIDENCE_BYTES:
            raise ValueError('Pair report byte bound exceeded')
        body = {key: value for key, value in pair.items() if key != 'pair_id'}
        if pair.get('kind') != 'timing_outcome_pair' or pair.get('schema_version') != 1 or pair.get('pair_id') != digest(canonical(body)):
            raise ValueError('Versioned immutable pair report required')
        identity = pair['pair_id']
        if identity in identities:
            raise ValueError('Duplicate pair cannot inflate evidence')
        identities.add(identity)
        group = pair['initialization_group']
        if groups.setdefault(group, pair['split']) != pair['split']:
            raise ValueError('Initialization siblings cannot cross splits')
        reasons.update(pair['invalid_reasons'])
        if not pair['eligible']:
            reasons.update(['ineligible:' + pair['ineligible_reason']])
        if pair['timing_label'] is not None:
            labels.update([pair['timing_label']])
    return {'kind': 'intervention_value_report', 'schema_version': 1,
            'scheduled_pairs': len(pairs), 'initialization_groups': len(groups),
            'valid_fixture_pairs': sum(pair['valid'] for pair in pairs),
            'qualified_robotics_pairs': 0, 'robotics_benefit_established': False,
            'labels': dict(sorted(labels.items())), 'exclusions': dict(sorted(reasons.items())),
            'missing_evidence': ['qualified recorded matched continuations',
                                 'independent semantic annotation/retrieval evaluation',
                                 'budget-matched random-timing and fixed-cadence outcomes'],
            'pairs': list(pairs)}


def replay_record(replay):
    if not isinstance(replay, ReplayEvidence):
        raise ValueError('Explicit replay evidence required')
    object_path(replay.coverage_sha256)
    if replay.scope not in ('synthetic_fixture', 'recorded_replay'):
        raise ValueError('Explicit synthetic or recorded replay evidence scope required')
    return {'kind': 'continuation_replay_verification', 'schema_version': 1,
            'uninterrupted': _tape_record(replay.uninterrupted),
            'restored': _tape_record(replay.restored),
            'coverage_sha256': replay.coverage_sha256, 'scope': replay.scope}


def _read_tape(value):
    if (not isinstance(value, dict) or not isinstance(value.get('actions'), list)
            or len(value['actions']) > 600 or not isinstance(value.get('frames'), list)
            or len(value['frames']) != len(value['actions']) + 1):
        raise ValueError('Bounded recorded native tape required')
    _bounded_tape_ids(value.get('episode_id'), value.get('parent_episode_id'))
    frames = tuple(Frame(step=row['step'], time_ns=row['time_ns'], components=tuple(row['components'].items()),
                         success=row['success'], terminated=row['terminated'], truncated=row['truncated'])
                   for row in value['frames'])
    tape = Tape(value['episode_id'], value['seed'], tuple(tuple(row) for row in value['actions']),
                frames, value['parent_episode_id'], value['inherited_frames'])
    return tape


def evaluate_plan(store, plan_ref):
    """Recompute a saved plan from hash-checked evidence, never trust saved labels.

    Missing scheduled artifacts remain listed separately from evaluated pairs.
    Corruption and malformed artifacts fail rather than becoming task failures.
    """
    cache, total, processed = {}, 0, 0

    def read(key):
        nonlocal total, processed
        object_path(key)
        if key not in cache:
            raw = read_small(store, key, limit=MAX_EVIDENCE_BYTES)
            total += len(raw)
            if total > MAX_EVIDENCE_BYTES:
                raise ValueError('Evaluation closure metadata byte bound exceeded')
            cache[key] = (json.loads(raw), len(raw))
        # Cached metadata still incurs tape reconstruction/normalization per comparison.
        processed += cache[key][1]
        if processed > MAX_PROCESSING_BYTES:
            raise ValueError('Evaluation processing byte bound exceeded')
        return cache[key][0]

    def continuation(key):
        value = read(key)
        if value.get('kind') != 'matched_continuation' or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
            raise ValueError('Versioned continuation artifact required')
        fields = {key: item for key, item in value.items() if key not in ('kind', 'schema_version', 'tape')}
        fields['calls'] = tuple(fields['calls'])
        result = Continuation(tape=_read_tape(value['tape']), **fields)
        if canonical(continuation_record(result)) != canonical(value):
            raise ValueError('Continuation artifact normalization differs')
        return result

    def replay(key):
        value = read(key)
        if (value.get('kind') != 'continuation_replay_verification'
                or type(value.get('schema_version')) is not int or value['schema_version'] != 1):
            raise ValueError('Versioned replay verification required')
        evidence = ReplayEvidence(_read_tape(value['uninterrupted']), _read_tape(value['restored']),
                                  value['coverage_sha256'], value['scope'])
        if canonical(replay_record(evidence)) != canonical(value):
            raise ValueError('Replay evidence binding differs')
        return evidence

    plan = read(plan_ref)
    if (not isinstance(plan, dict) or set(plan) != {'kind', 'schema_version', 'recipe', 'comparisons'}
            or plan['kind'] != 'intervention_evaluation_plan' or type(plan['schema_version']) is not int
            or plan['schema_version'] != 1 or not isinstance(plan['comparisons'], list)
            or len(plan['comparisons']) > MAX_PAIRS):
        raise ValueError('Bounded versioned evaluation plan required')
    recipe = UtilityRecipe(**plan['recipe'])
    pairs, missing, identities, groups = [], [], set(), {}
    for spec in plan['comparisons']:
        if (not isinstance(spec, dict) or set(spec) != {'comparison_id', 'protocol', 'continue', 'replan', 'replay', 'eligible', 'ineligible_reason'}
                or not isinstance(spec['comparison_id'], str) or not 0 < len(spec['comparison_id']) <= 256
                or spec['comparison_id'] in identities):
            raise ValueError('Distinct scheduled comparison identity/schema required')
        identities.add(spec['comparison_id'])
        fields = dict(spec['protocol'], actor_seeds=tuple(spec['protocol']['actor_seeds']))
        protocol = PairProtocol(**fields)
        if groups.setdefault(protocol.initialization_group, protocol.split) != protocol.split:
            raise ValueError('Initialization siblings cannot cross splits, including missing cases')
        artifacts, missing_object = {}, False
        for name in ('continue', 'replan', 'replay'):
            if spec[name] is None:
                continue
            try:
                artifacts[name] = replay(spec[name]) if name == 'replay' else continuation(spec[name])
            except FileNotFoundError:
                missing_object = True
        absent = [arm for arm in ('continue', 'replan') if spec[arm] is None]
        if absent or missing_object:
            for arm in ('continue', 'replan'):
                if arm in artifacts:
                    _outcome(artifacts[arm], protocol)
        if absent:
            missing.append({'comparison_id': spec['comparison_id'], 'reason': 'missing_branch', 'arms': absent})
            continue
        if missing_object:
            missing.append({'comparison_id': spec['comparison_id'], 'reason': 'missing_object'})
            continue
        pairs.append(evaluate_pair(artifacts['continue'], artifacts['replan'], protocol=protocol, recipe=recipe,
                                   replay=artifacts.get('replay'), eligible=spec['eligible'], ineligible_reason=spec['ineligible_reason']))
    report = summarize(pairs)
    report.update(plan_sha256=plan_ref, scheduled_pairs=len(plan['comparisons']),
                  evaluated_pairs=len(pairs), initialization_groups=len(groups), missing_comparisons=missing)
    return report
