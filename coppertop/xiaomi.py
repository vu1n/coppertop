"""Derived Xiaomi state view; canonical source state remains sixteen-dimensional."""
import math
import struct
from .episode import vector

def f32(value):return struct.unpack('f',struct.pack('f',value))[0]

def axis_angle_xyzw(quaternion):
    q=vector(quaternion,4);norm=math.sqrt(sum(v*v for v in q))
    if norm<1e-12:return (0.,0.,0.)
    q=tuple(v/norm for v in q)
    if q[3]<0:q=tuple(-v for v in q)
    xyz=q[:3];s=math.sqrt(sum(v*v for v in xyz))
    if s<1e-12:return (0.,0.,0.)
    angle=2*math.atan2(s,max(-1.,min(1.,q[3])))
    return tuple(f32(v/s*angle) for v in xyz)

def native_state(source16):
    s=vector(source16,16)
    # LeRobot is base-first/xyzw; the native evaluator is EE-first/axis-angle.
    return tuple(f32(v) for v in (*s[7:10],*axis_angle_xyzw(s[10:14]),*s[14:16],*s[:3],*axis_angle_xyzw(s[3:7])))
