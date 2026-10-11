"""Typed shadow judgments and a timing gate; never emits native robot actions."""
from dataclasses import dataclass
from decimal import Decimal
import math

from .chords import canonical, digest

QUESTIONS = {
    'timing': {
        'type': 'choice',
        'instructions': 'At this completed control boundary, should Xiaomi request a fresh action chunk early? Judge only the observed prefix and queued predictions. Do not infer future success or invent a recovery skill.',
        'criteria': {
            'continue': 'Keep the current queued controls until the normal replan boundary; no observed reason to query early.',
            'replan': 'Observed task progress or current queue mismatch provides a reason to request a fresh Xiaomi chunk early.',
            'uncertain': 'The supplied observations do not establish which timing choice is better.',
        },
    },
    'data_usefulness': {
        'type': 'choice',
        'instructions': 'Does this observed prefix contain useful task-directed behavior for later training-data review? This is a prediction, not a validated data-utility label or an intervention decision.',
        'criteria': {
            'useful': 'Observed task-directed behavior makes the prefix a candidate for training-data review.',
            'not_useful': 'Observed behavior is uninformative or visibly inconsistent with the task.',
            'uncertain': 'The prefix is insufficient to judge training-data usefulness.',
        },
    },
}
SCHEMA_SHA256 = digest(canonical(QUESTIONS))


def probability(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('Finite probability in [0,1] required')
    return float(value)


def validate_answers(answers, *, probability_decimals=None):
    if probability_decimals not in (None, 4):
        raise ValueError("Supported probability precision required")
    if not isinstance(answers, dict) or set(answers) != set(QUESTIONS):
        raise ValueError('Exact question set required')
    checked = {}
    for name, question in QUESTIONS.items():
        answer = answers[name]
        if not isinstance(answer, dict) or answer.get('type') != 'choice':
            raise ValueError('Typed choice answer required')
        values = answer.get('probabilities')
        if not isinstance(values, dict) or set(values) != set(question['criteria']):
            raise ValueError('Exact option probabilities required')
        values = {key: probability(value) for key, value in values.items()}
        tolerance = 1e-5
        if probability_decimals == 4:
            if any(abs(value-round(value,4)) > 1e-12 for value in values.values()):
                raise ValueError('Four-decimal probabilities required')
            tolerance = len(values) * 0.5e-4 + 1e-12
        if abs(sum(values.values()) - 1) > tolerance:
            raise ValueError('Option probabilities must sum to one')
        choice = answer.get('choice')
        if choice not in values or values[choice] < max(values.values()) - 1e-8:
            raise ValueError('Chosen option must maximize probability')
        checked[name] = {'type': 'choice', 'choice': choice, 'probabilities': values,
                         'confidence': probability(answer['confidence'])}
    return checked


@dataclass(frozen=True)
class GatePolicy:
    threshold: float = 0.8
    margin: float = 0.2
    max_age_controls: int = 2
    probability_decimals: int | None = None

    def __post_init__(self):
        probability(self.threshold)
        probability(self.margin)
        if type(self.max_age_controls) is not int or not 0 <= self.max_age_controls <= 8:
            raise ValueError('Bounded decision age required')
        if self.probability_decimals not in (None, 4):
            raise ValueError('Supported probability precision required')

    def decide(self, answers, *, observed_frame: int, current_frame: int,
               queued_until: int, remaining_queries: int, stopped: bool = False):
        """Query admission only. Candidate arrival/handoff remains the async runner's job."""
        checked = validate_answers(answers, probability_decimals=self.probability_decimals)
        if (any(type(value) is not int or value < 0 for value in
                (observed_frame, current_frame, queued_until, remaining_queries))
                or current_frame < observed_frame or type(stopped) is not bool):
            raise ValueError('Completed control indices and query budget required')
        timing = checked['timing']['probabilities']
        # Compare serialized decimal probabilities without a binary subtraction
        # turning an exact .6000-.4000 boundary into less than .2.
        margin = Decimal(str(timing['replan'])) - max(
            Decimal(str(timing[key])) for key in ('continue', 'uncertain'))
        reason = 'no_early_replan'
        if stopped:
            reason = 'episode_stopped'
        elif current_frame >= queued_until:
            reason = 'normal_queue_boundary'
        elif current_frame - observed_frame > self.max_age_controls:
            reason = 'stale_decision'
        elif remaining_queries == 0:
            reason = 'query_budget_exhausted'
        elif (timing['replan'] >= self.threshold
              and margin >= Decimal(str(self.margin))):
            reason = 'early_replan'
        return {'request_early_replan': reason == 'early_replan', 'reason': reason,
                'observed_frame': observed_frame, 'current_frame': current_frame,
                'queued_until': queued_until, 'policy': self.__dict__,
                'threshold_calibrated': False}
