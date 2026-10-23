"""Local effect indexing and evidence reduction. No model or simulator executor."""
import argparse
from dataclasses import asdict
import io
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .analysis import from_release
from .chord_effects import effect_annotation, from_effect_release
from .chords import LocalStore, canonical, digest, fork, frame_id, release
from .intervention_value import (Continuation, PairProtocol, ReplayEvidence, UtilityRecipe,
                                 continuation_record, evaluate_plan, replay_record)
from .mechanism import COMPONENTS, Frame, Tape, native_actions
from .official_recording import ACTION_LAYOUT, STATE_LAYOUT


def _fixture_tape(name, *, changed=False, success=False):
    actions = tuple(native_actions([float(changed and i >= 1)] * 12) for i in range(4))
    frames = tuple(Frame(i, i * 50_000_000,
                         tuple((key, digest(f'{key}/{i}/{changed and i > 1}'.encode())) for key in COMPONENTS),
                         success=success and i == 4) for i in range(5))
    return Tape(name, 1152, actions, frames, parent_episode_id='synthetic/decision', inherited_frames=2)


def _fixture_call(kind, index):
    return {'kind': kind, 'control': 1, 'started_ns': index * 100, 'finished_ns': index * 100 + 100,
            'status': 'completed', 'cost_units': 0.01, 'seed': 17 if kind == 'actor' else None}


def demo(destination):
    """Create scripted data in a fresh directory; this is a plumbing demonstration."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    store = LocalStore(destination / 'store')
    rubric = store.put(canonical({'kind': 'synthetic_rubric', 'scope': 'scripted labels; no visual or robot evidence'}))
    episode_id = 'synthetic/effects'
    rows = [dict(step=i, time_ns=i * 50_000_000, frame_id=frame_id(episode_id, i),
                 state=[0.0]*16, action=[0.0]*12 if i < 4 else None,
                 observation=digest(f'scripted observation {i}'.encode()),
                 success=False, terminated=False, truncated=False) for i in range(5)]
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), sink)
    signals = store.put(sink.getvalue())
    episode = store.put(canonical({'schema_version': 1, 'kind': 'generated_official_rollout',
        'episode_id': episode_id, 'initialization_group': 'synthetic/init',
        'state_layout': STATE_LAYOUT, 'action_layout': ACTION_LAYOUT,
        'signals': signals, 'length': len(rows), 'videos': {},
        'provenance': {'real_simulation': False, 'split': 'development', 'license': 'synthetic example'},
        'official_result': {'status': 'completed', 'horizon': 4, 'success': False,
                            'outcome_scope': 'scripted bounded prefix; eventual success unknown'}}))
    base = release(store, [episode])
    data = from_release(store, base)
    annotations = [effect_annotation(data, episode_id=episode_id, start=start, end=end,
                    entity='mug', predicate='support_relation', before=before, after=after,
                    evidence_kind='synthetic_fixture', provenance=rubric)
                   for start, end, before, after in ((0, 2, 'table', 'gripper'), (2, 4, 'gripper', 'counter'))]
    child = fork(store, base, overlays=tuple(store.put(canonical(value)) for value in annotations))
    _, effects = from_effect_release(store, child)
    pq.write_table(effects, destination / 'effects.parquet')

    protocol = PairProtocol('synthetic/init', 'development', 1, 4, 2, digest(b'fixture actor'),
                            digest(b'fixture continuation policy'), digest(b'paused fixture clock'),
                            digest(b'causal fixture input view'), (17, 17))
    failure_definition = store.put(canonical({'kind': 'synthetic_failure_definition',
                                              'rule': 'zero scripted task-failure events in these fixtures'}))
    recipe = UtilityRecipe(1.0, 0.01, 0.1, 0.02, 1.0, 0.2, 0.001, 'synthetic_cost_unit', failure_definition)
    replay = ReplayEvidence(_fixture_tape('uninterrupted'), _fixture_tape('restored'), rubric, 'synthetic_fixture')
    verification = store.put(canonical(replay_record(replay)))
    comparisons = []
    for name, first_success, second_success, changed, extra, verified in (
            ('helpful', False, True, True, True, True),
            ('harmful', True, False, True, True, True),
            ('neutral', False, False, True, False, True),
            ('no-handoff', False, False, False, True, True),
            ('missing-replay', False, False, False, False, False)):
        refs = {}
        for arm, success in (('continue', first_success), ('replan', second_success)):
            calls = (_fixture_call('actor', 0),)
            if arm == 'replan' and extra:
                calls = (_fixture_call('scorer', 0), _fixture_call('actor', 1), _fixture_call('actor', 2))
            branch = Continuation(_fixture_tape(f'synthetic/{name}/{arm}', changed=changed and arm == 'replan', success=success),
                                  protocol.initialization_group, protocol.split, protocol.identity, calls,
                                  1_000_000_000, 1000, 900_000_000 if success else None, 0, True)
            refs[arm] = store.put(canonical(continuation_record(branch)))
        comparisons.append({'comparison_id': name, 'protocol': asdict(protocol), **refs,
                            'replay': verification if verified else None, 'eligible': True, 'ineligible_reason': None})
    comparisons.append(dict(comparisons[-1], comparison_id='missing-branch', replan=None))
    plan = store.put(canonical({'kind': 'intervention_evaluation_plan', 'schema_version': 1,
                               'recipe': asdict(recipe), 'comparisons': comparisons}))
    report = evaluate_plan(store, plan)
    (destination / 'report.json').write_bytes(canonical(report))
    manifest = {'kind': 'synthetic_effect_value_example', 'schema_version': 1,
                'scope': 'scripted annotations/outcomes; validates plumbing, not robotics performance',
                'base_release': base, 'effect_release': child, 'evaluation_plan': plan,
                'effects': effects.num_rows, 'scheduled_pairs': report['scheduled_pairs'],
                'valid_fixture_pairs': report['valid_fixture_pairs'],
                'qualified_robotics_pairs': 0, 'robotics_benefit_established': False}
    (destination / 'example.json').write_bytes(canonical(manifest))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    example = commands.add_parser('demo', help='write a fresh synthetic example')
    example.add_argument('--output', required=True, type=Path)
    for command, identity in (('index', 'release'), ('evaluate', 'plan')):
        sub = commands.add_parser(command)
        sub.add_argument('--store', required=True, type=Path)
        sub.add_argument('--'+identity, required=True)
        sub.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == 'demo':
        print(json.dumps(demo(args.output), sort_keys=True))
    elif args.command == 'index':
        _, table = from_effect_release(LocalStore(args.store), args.release)
        with args.output.open('xb') as target:
            pq.write_table(table, target)
        print(json.dumps({'effect_rows': table.num_rows, 'output': str(args.output)}))
    else:
        report = evaluate_plan(LocalStore(args.store), args.plan)
        with args.output.open('xb') as target:
            target.write(canonical(report))
        print(json.dumps({key: report[key] for key in ('scheduled_pairs', 'evaluated_pairs', 'qualified_robotics_pairs')}))


if __name__ == '__main__':
    main()
