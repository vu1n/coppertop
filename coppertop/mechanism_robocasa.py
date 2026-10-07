"""Pinned CoffeeSetupMug replay adapter; hashes are audit evidence, not restoration."""
from collections import deque
import random

import numpy as np

from .chords import canonical, digest
from .mechanism import Frame, native_actions


def evidence(value, omissions, path='', depth=0):
    """Read simple latent fields without invoking sensors, callbacks or properties.

    Opaque references are recorded as omissions, never advertised as complete state.
    """
    if value is None or type(value) in (bool, int, str):
        return value
    if isinstance(value, (float, np.number)):
        number = float(value)
        return number if np.isfinite(number) else {'nonfinite': str(number)}
    if isinstance(value, np.ndarray):
        if value.dtype.hasobject:
            omissions.append(path + ':object_array')
            return None
        return {'dtype': str(value.dtype), 'shape': list(value.shape),
                'sha256': digest(np.ascontiguousarray(value).tobytes())}
    if isinstance(value, np.random.Generator):
        return evidence(value.bit_generator.state, omissions, path, depth+1)
    if depth < 10 and isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return {k: evidence(v, omissions, path+'/'+k, depth+1) for k, v in sorted(value.items())}
    if depth < 10 and isinstance(value, (list, tuple, deque)):
        return [evidence(v, omissions, path+'/'+str(i), depth+1) for i, v in enumerate(value)]
    # Known buffer/controller/interpolator objects expose mutable fields in __dict__.
    module = type(value).__module__
    if depth < 10 and module.startswith(('robosuite.controllers.', 'robosuite.utils.buffers', 'robosuite.utils.observables')):
        return evidence(vars(value), omissions, path, depth+1)
    omissions.append(path + ':' + module + '.' + type(value).__name__)
    return None


class RoboCasaReplay:
    def __init__(self):
        from .performance_runtime import load_official, simulator_gate
        simulator_gate()
        self.entry = load_official('entry')
        self.env = None
        self.coverage = {'complete_state_certified': False, 'omissions': {}}

    def reset(self, seed):
        if self.env is not None:
            raise ValueError('Fresh adapter instance required for every reset')
        import gymnasium as gym
        random.seed(seed)
        np.random.seed(seed)
        # Matches pilot: global episode seed before constructor, base seed1000 in make.
        self.env = gym.make('robocasa/CoffeeSetupMug', split='pretrain', seed=1000)
        self.physics = self.env.unwrapped.env
        self.observation, self.info = self.env.reset(seed=seed)
        self.index = 0
        self.flags = (False, False, False)
        self.states = deque(maxlen=7)
        self.images = {key: deque(maxlen=7) for key in self.entry.CAMERA_KEYS}
        self.control_ns = round(self.physics.control_timestep * 1e9)
        if self.control_ns != 50_000_000:
            raise ValueError('Pinned simulator 20Hz control clock required')
        self._append()
        return self.frame()

    def _append(self):
        state = np.array(self.entry.observation_to_state(self.observation), dtype=np.float32, copy=True)
        if state.shape != (14,) or not np.isfinite(state).all():
            raise ValueError('Pinned simulator native14 state differs')
        self.states.append(state)
        for key, image in self.entry.collect_images(self.observation).items():
            if image.shape != (256, 256, 3) or image.dtype != np.uint8:
                raise ValueError('Pinned camera geometry/dtype differs')
            self.images[key].append(np.array(image, copy=True))

    def inputs(self):
        # Detached immutable snapshot prevents a future query reading later observations.
        states = self.entry.sample_history(self.states, 4, 2).copy()
        images = {key: self.entry.sample_history(queue, 4, 2).copy() for key, queue in self.images.items()}
        for array in [states, *images.values()]:
            array.setflags(write=False)
        return states, images, self.observation['annotation.human.task_description']

    def step(self, action):
        from robocasa.utils.env_utils import convert_action
        if any(self.flags):
            raise ValueError('Cannot step across episode boundary')
        control = np.asarray(native_actions(action), dtype=np.float32)
        self.observation, _, done, truncated, self.info = self.env.step(convert_action(control))
        self.flags = (bool(self.info['success']), bool(done), bool(truncated))
        self.index += 1
        self._append()
        return self.frame()

    def frame(self):
        import mujoco
        sim = self.physics.sim
        state = np.empty(mujoco.mj_stateSize(sim.model._model, mujoco.mjtState.mjSTATE_INTEGRATION))
        mujoco.mj_getState(sim.model._model, sim.data._data, state, mujoco.mjtState.mjSTATE_INTEGRATION)
        # Hash all exposed native model arrays, including mutable fixture site colors.
        arrays = {key: getattr(sim.model._model, key) for key in dir(sim.model._model)
                  if not key.startswith('_') and isinstance(getattr(sim.model._model, key), np.ndarray)}
        values = {'physics': state, 'model': arrays,
            'task': {'env_fields': vars(self.physics), 'fixtures': {key: vars(value) for key, value in self.physics.fixtures.items()}},
            'controller': {str(i): {'composite': robot.composite_controller,
                                   'recent': {k: v for k, v in vars(robot).items() if k.startswith('recent')}}
                           for i, robot in enumerate(self.physics.robots)},
            'rng': {'python': random.getstate(), 'numpy': np.random.get_state(), 'env': self.physics.rng},
            'observables': {'values': self.physics._observables, 'cache': self.physics._obs_cache},
            'history': {'states': self.states, 'images': self.images},
            'observation': self.observation}
        components = []
        for key, value in values.items():
            missing = []
            components.append((key, digest(canonical({'value': evidence(value, missing, key)}))))
            self.coverage['omissions'][key] = sorted(set(missing))
        return Frame(self.index, self.index * self.control_ns, tuple(components), *self.flags)

    def close(self):
        if self.env is not None:
            self.env.close()
