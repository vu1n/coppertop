"""Source-compatible episode boundary; demonstrations never imply proposal or recovery labels."""
from dataclasses import dataclass, asdict
import hashlib
import math

CAMERAS=('robot0_agentview_left','robot0_agentview_right','robot0_eye_in_hand')
EXPLORED_DEV_SEEDS={7,8,9,12,15,23}
LEROBOT_TO_NATIVE=(5,6,7,8,9,10,11,0,1,2,3,4)
NATIVE_TO_LEROBOT=(7,8,9,10,11,0,1,2,3,4,5,6)

def vector(values, width):
    result=tuple(float(v) for v in values)
    if len(result)!=width or not all(math.isfinite(v) for v in result):
        raise ValueError(f'Expected {width} finite values')
    return result

def reorder_action(action, source_layout, destination_layout):
    action=vector(action,12)
    if source_layout not in {'native','lerobot'} or destination_layout not in {'native','lerobot'}:
        raise ValueError('Action layout must be explicit')
    if source_layout==destination_layout:
        return action
    order=LEROBOT_TO_NATIVE if source_layout=='lerobot' else NATIVE_TO_LEROBOT
    return tuple(action[i] for i in order)

@dataclass(frozen=True)
class Episode:
    episode_id: str
    initialization_group: str
    split: str
    instruction: str
    source_state16: tuple
    executed_native: tuple
    timestamps: tuple
    rewards: tuple
    dones: tuple
    videos: dict
    provenance: dict
    quality: dict
    proposed_chunks: tuple=()
    native_chunk_membership: tuple=()

    def validate(self):
        n=len(self.timestamps)
        if not n or n>10000 or not self.episode_id or not self.initialization_group or not self.instruction.strip():
            raise ValueError('Missing or oversized episode identity/content')
        if self.split not in {'train','development','sealed_test'}:raise ValueError('Unknown split')
        seed=self.provenance.get('simulator_seed')
        if seed in EXPLORED_DEV_SEEDS and self.split!='development':raise ValueError('Explored seed must remain DEV')
        if any(len(x)!=n for x in [self.source_state16,self.executed_native,self.rewards,self.dones,self.native_chunk_membership]):
            raise ValueError('Episode fields have different lengths')
        for s,a in zip(self.source_state16,self.executed_native):vector(s,16);vector(a,12)
        if any(not math.isfinite(t) or t<0 for t in self.timestamps) or any(a>=b for a,b in zip(self.timestamps,self.timestamps[1:])):
            raise ValueError('Timestamps must be finite and strictly increasing')
        if any(type(d) is not bool for d in self.dones) or any(self.dones[:-1]):raise ValueError('Episode crosses a done boundary')
        if any(r is not None and not math.isfinite(r) for r in self.rewards):raise ValueError('Invalid reward')
        if set(self.videos)!=set(CAMERAS):raise ValueError('Need three declared camera streams')
        for stream in self.videos.values():
            if stream['frame_count']!=n or stream['pixel_origin'] not in {'video_decoded','raw_rgb'}:raise ValueError('Camera alignment/origin mismatch')
        if self.provenance['real_data'] and not self.provenance.get('source_sha256'):raise ValueError('Real episode requires source hashes')
        chunk_ids=set()
        for chunk in self.proposed_chunks:
            if not isinstance(chunk.get('chunk_id'),str) or not chunk['chunk_id'] or chunk['chunk_id'] in chunk_ids:
                raise ValueError('Proposal requires a unique chunk identity')
            chunk_ids.add(chunk['chunk_id'])
            if type(chunk['available_at_frame']) is not int or not 0<=chunk['available_at_frame']<n:raise ValueError('Invalid proposal availability')
            if len(chunk['actions_native'])!=16:raise ValueError('Native proposal must contain sixteen actions')
            for action in chunk['actions_native']:vector(action,12)
        if any(member is not None and member not in chunk_ids for member in self.native_chunk_membership):
            raise ValueError('Executed action refers to an unknown native chunk')
        return self

    def to_dict(self):return {'schema_version':1,**asdict(self)}

def initialization_group(repo, logical_dataset, episode_index):
    # Revisions and window/prefix choices are excluded so copies cannot be resplit.
    identity=f'{repo}/{logical_dataset}/episode-{episode_index}'
    return 'official-episode/'+hashlib.sha256(identity.encode()).hexdigest()

def check_group_splits(episodes, registry):
    for episode in episodes:
        episode.validate()
        if registry.get(episode.initialization_group)!=episode.split:raise ValueError('Group registry disagreement')
