"""Bounded portable tree export and safe container-disk restore, never acquisition."""
import argparse
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import time

from .chords import canonical, digest
from .reload_launch import hard_deadline, read_json
from .runpod_cloud import save
from .runpod_runtime import contract
from .streams import CHUNK_BYTES, ObjectRef, checked, file_chunks, file_ref

GIB=1024**3
BOUNDS={'tar':50*GIB,'shard':4*GIB,'shards':13,'manifest':64*1024**2,
        'debs':2*GIB,'code':16*1024**2,'rootfs':70*GIB,'entries':500000}


def safe_name(name):
    if not isinstance(name,str) or not name or name.startswith('/') or '\\' in name or '\x00' in name:
        raise ValueError('Relative POSIX bundle name required')
    parts=name.split('/')
    if any(p in ('','.','..') for p in parts):raise ValueError('Unsafe bundle member')
    return name


def link_target(name,target,roots):
    if not isinstance(target,str) or '\\' in target or '\x00' in target:raise ValueError('Unsafe link')
    logical=target if target.startswith('/') else str(PurePosixPath('/'+name).parent/target)
    normalized=os.path.normpath(logical).lstrip('/')
    if not any(normalized==r or normalized.startswith(r+'/') for r in roots):
        raise ValueError('Link escapes exported absolute prefixes')


def validate_manifest(value):
    if value.get('schema_version')!=1 or value.get('kind')!='runpod-runtime-bundle':raise ValueError('Bundle schema differs')
    roots=value['roots']; entries=value['entries']
    if (roots!=[*contract()['required_trees'],'debs','app'] or not isinstance(entries,dict)
        or len(entries)>BOUNDS['entries'] or len(canonical(value))>BOUNDS['manifest']):
        raise ValueError('Complete bounded bundle prefixes required')
    total=debs=code=0
    for name,item in entries.items():
        safe_name(name)
        if not any(name==r or name.startswith(r+'/') for r in roots):raise ValueError('Unexpected prefix')
        if type(item.get('mode')) is not int or not 0<=item['mode']<=0o777:raise ValueError('Privileged mode rejected')
        if item.get('kind')=='file':
            ref=ObjectRef(item['sha256'],item['size']);total+=ref.size
            if name.startswith('debs/'):debs+=ref.size
            if name.startswith('app/'):code+=ref.size
        elif item.get('kind')=='link':link_target(name,item['target'],roots)
        elif item.get('kind')!='directory':raise ValueError('Special member rejected')
        parent=PurePosixPath(name).parent
        while str(parent)!='.':
            if str(parent) in entries and entries[str(parent)]['kind']!='directory':raise ValueError('Member under a link/file')
            parent=parent.parent
    if total!=value['bytes'] or total>BOUNDS['tar'] or debs>BOUNDS['debs'] or code>BOUNDS['code']:
        raise ValueError('Bundle byte ceiling exceeded')
    for root in roots:
        if entries.get(root,{}).get('kind')!='directory':raise ValueError('Required tree missing')
    for name in contract()['required_files']:
        if name not in entries:raise ValueError('Required runtime file missing')
    if 'debs/lock.json' not in entries:raise ValueError('Offline package lock missing')
    return value


def manifest(root,*,deadline):
    root=Path(root);entries={};total=0
    roots=[*contract()['required_trees'],'debs','app']
    for name in roots:
        base=root/name
        if not base.is_dir() or base.is_symlink():raise ValueError('Complete ordinary tree required: '+name)
        for path in [base,*sorted(base.rglob('*'))]:
            if time.monotonic()>=deadline:raise TimeoutError('Export inventory deadline')
            rel=path.relative_to(root).as_posix();meta=path.lstat();mode=stat.S_IMODE(meta.st_mode)
            if mode&0o7000:raise ValueError('Privileged source mode rejected')
            if path.is_symlink():item={'kind':'link','target':os.readlink(path),'mode':mode}
            elif stat.S_ISDIR(meta.st_mode):item={'kind':'directory','mode':mode}
            elif stat.S_ISREG(meta.st_mode):
                ref=file_ref(path);total+=ref.size;item=dict(ref.to_dict(),kind='file',mode=mode)
            else:raise ValueError('Special source rejected')
            entries[rel]=item
            if total>BOUNDS['tar'] or len(entries)>BOUNDS['entries']:raise ValueError('Export bound')
    return validate_manifest({'schema_version':1,'kind':'runpod-runtime-bundle','roots':roots,
        'entries':entries,'bytes':total,'runtime_contract_sha256':digest(canonical(contract()))})


class SplitWriter:
    def __init__(self,directory,*,shard_bytes,tar_bytes,deadline):
        self.directory=directory;self.shard_bytes=shard_bytes;self.limit=tar_bytes;self.deadline=deadline
        self.stream=None;self.size=0;self.total=0;self.refs=[];self.whole=hashlib.sha256()
    def write(self,data):
        if time.monotonic()>=self.deadline:raise TimeoutError('Export deadline')
        if self.total+len(data)>self.limit:raise ValueError('Tar byte ceiling')
        original=len(data);self.whole.update(data);self.total+=original
        while data:
            if self.stream is None:
                if len(self.refs)>=BOUNDS['shards']:raise ValueError('Shard count ceiling')
                self.path=self.directory/('shard-%02d.tar'%len(self.refs));self.stream=self.path.open('xb');self.size=0
            count=min(len(data),self.shard_bytes-self.size);self.stream.write(data[:count]);data=data[count:];self.size+=count
            if self.size==self.shard_bytes:self.finish_shard()
        return original
    def finish_shard(self):
        if self.stream:
            self.stream.flush();os.fsync(self.stream.fileno());self.stream.close();self.stream=None
            self.refs.append(dict(file_ref(self.path).to_dict(),name=self.path.name))
    def close(self):self.finish_shard()


def export(root,output,*,max_seconds=6000,shard_bytes=BOUNDS['shard']):
    """No source writes; failed exclusive export remains incomplete and recoverable."""
    if not 0<shard_bytes<=BOUNDS['shard'] or not 0<max_seconds<=6720:raise ValueError('Export limits')
    output=Path(output)
    if output.is_symlink() or output.exists():raise ValueError('Fresh export directory required')
    output.mkdir(parents=True)
    if shutil.disk_usage(output).free<BOUNDS['tar']+2*GIB:raise ValueError('Source archive scratch needs52GiB free')
    deadline=time.monotonic()+max_seconds
    with hard_deadline(max_seconds):
        value=manifest(root,deadline=deadline);save(output/'manifest.json',value)
        writer=SplitWriter(output,shard_bytes=shard_bytes,tar_bytes=BOUNDS['tar'],deadline=deadline)
        try:
            with tarfile.open(fileobj=writer,mode='w|',format=tarfile.PAX_FORMAT) as archive:
                for name,item in sorted(value['entries'].items()):
                    member=tarfile.TarInfo(name);member.mode=item['mode'];member.mtime=0
                    if item['kind']=='directory':member.type=tarfile.DIRTYPE;archive.addfile(member)
                    elif item['kind']=='link':member.type=tarfile.SYMTYPE;member.linkname=item['target'];archive.addfile(member)
                    else:
                        path=Path(root)/name;member.size=item['size']
                        if file_ref(path)!=ObjectRef(item['sha256'],item['size']):raise ValueError('Source changed before tar')
                        with path.open('rb') as source:archive.addfile(member,source)
                        if file_ref(path)!=ObjectRef(item['sha256'],item['size']):raise ValueError('Source changed during tar')
        finally:writer.close()
        descriptor={'schema_version':1,'manifest_sha256':digest(canonical(value)),
            'tar_sha256':writer.whole.hexdigest(),'tar_bytes':writer.total,'shards':writer.refs}
        validate_descriptor(descriptor)
        save(output/'bundle.json',descriptor)
    return descriptor


def validate_descriptor(value):
    if value.get('schema_version')!=1:raise ValueError('Descriptor version')
    ObjectRef(value['manifest_sha256'],0);ObjectRef(value['tar_sha256'],value['tar_bytes'])
    shards=value['shards']
    if not 1<=len(shards)<=BOUNDS['shards'] or not 0<value['tar_bytes']<=BOUNDS['tar']:raise ValueError('Export size/count')
    for i,ref in enumerate(shards):
        if ref['name']!='shard-%02d.tar'%i or not 0<ref['size']<=BOUNDS['shard']:raise ValueError('Shard identity')
        ObjectRef(ref['sha256'],ref['size'])
    if sum(s['size'] for s in shards)!=value['tar_bytes']:raise ValueError('Shard total differs')
    return value


class JoinedReader(io.RawIOBase):
    def __init__(self,paths,descriptor,deadline):
        self.paths=iter(paths);self.descriptor=descriptor;self.deadline=deadline;self.stream=None
        self.sha=hashlib.sha256();self.total=0
    def readable(self):return True
    def readinto(self,buffer):
        if time.monotonic()>=self.deadline:raise TimeoutError('Restore deadline')
        while True:
            if self.stream is None:
                try:self.stream=next(self.paths).open('rb')
                except StopIteration:return 0
            chunk=self.stream.read(len(buffer))
            if chunk:
                buffer[:len(chunk)]=chunk;self.sha.update(chunk);self.total+=len(chunk)
                if self.total>self.descriptor['tar_bytes']:raise ValueError('Tar oversized')
                return len(chunk)
            self.stream.close();self.stream=None
    def close(self):
        if self.stream:self.stream.close()
        super().close()


def restore(bundle,destination,*,volume,free_bytes=None,max_seconds=5400):
    """Manually extract verified members on container disk; symlinks created last."""
    bundle=Path(bundle);destination=Path(destination);volume=Path(volume).resolve()
    if destination.is_symlink() or destination.exists():raise ValueError('Fresh restore directory required')
    if destination.resolve()==volume or volume in destination.resolve().parents:
        raise ValueError('Archive and extracted content must not share network volume')
    descriptor=validate_descriptor(read_json(bundle/'bundle.json',65536))
    value=validate_manifest(read_json(bundle/'manifest.json',BOUNDS['manifest']))
    if digest(canonical(value))!=descriptor['manifest_sha256']:raise ValueError('Manifest differs')
    if value['runtime_contract_sha256']!=digest(canonical(contract())):raise ValueError('Runtime contract differs')
    destination.parent.mkdir(parents=True,exist_ok=True)
    available=shutil.disk_usage(destination.parent).free if free_bytes is None else free_bytes
    if available<value['bytes']+2*GIB:raise ValueError('Insufficient container scratch')
    deadline=time.monotonic()+max_seconds
    paths=[bundle/s['name'] for s in descriptor['shards']]
    with hard_deadline(max_seconds):
        for path,ref in zip(paths,descriptor['shards']):
            if path.is_symlink() or file_ref(path)!=ObjectRef(ref['sha256'],ref['size']):raise ValueError('Shard checksum differs')
        destination.mkdir();seen=set();links=[]
        joined=JoinedReader(paths,descriptor,deadline)
        with io.BufferedReader(joined,CHUNK_BYTES) as source:
            with tarfile.open(fileobj=source,mode='r|') as archive:
                for member in archive:
                    name=safe_name(member.name);item=value['entries'].get(name)
                    if name in seen or item is None or member.mode!=item['mode'] or member.sparse is not None:
                        raise ValueError('Duplicate, sparse, altered or unexpected member')
                    seen.add(name);path=destination/name
                    path.parent.mkdir(parents=True,exist_ok=True)
                    if item['kind']=='directory' and member.isdir():path.mkdir(exist_ok=True)
                    elif item['kind']=='link' and member.issym() and member.linkname==item['target']:links.append((path,item))
                    elif item['kind']=='file' and member.isreg() and member.size==item['size']:
                        stream=archive.extractfile(member)
                        with path.open('xb') as out:
                            for chunk in checked(iter(lambda:stream.read(CHUNK_BYTES),b''),ObjectRef(item['sha256'],item['size'])):out.write(chunk)
                            out.flush();os.fsync(out.fileno())
                        path.chmod(item['mode'])
                    else:raise ValueError('Tar member type/size/link differs')
            # Tarfile stops at the first EOF; drain and verify the whole aggregate.
            while source.read(CHUNK_BYTES):pass
            if joined.total!=descriptor['tar_bytes'] or joined.sha.hexdigest()!=descriptor['tar_sha256']:
                raise ValueError('Aggregate archive differs')
        if seen!=set(value['entries']):raise ValueError('Incomplete runtime archive')
        for path,item in links:path.symlink_to(item['target'])
        for name,item in sorted(value['entries'].items(),reverse=True):
            if item['kind']=='directory':(destination/name).chmod(item['mode'])
        save(destination/'.restore-complete.json',{'manifest_sha256':descriptor['manifest_sha256'],
            'tar_sha256':descriptor['tar_sha256'],'bytes':value['bytes'],'all_files_verified':True})
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--export-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--seconds',type=int,default=6000)
    args=parser.parse_args();export(args.export_root,args.output,max_seconds=args.seconds)


if __name__=='__main__':main()
