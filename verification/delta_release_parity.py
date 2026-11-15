"""Exact source-only successor parity, including immutable base layer ancestry."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).absolute().parent))
from ssh_release_parity import inventory, sha, run, PROBE
import zipfile


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True,type=Path);p.add_argument('--artifacts',required=True,type=Path)
    p.add_argument('--core',required=True);p.add_argument('--media',required=True)
    p.add_argument('--out',required=True,type=Path)
    a=p.parse_args();assert not a.out.exists()
    build=json.loads((a.artifacts/'build-receipt.json').read_text());wheel=Path(build['wheel'])
    expected=inventory(a.source/'atlas_engine')
    assert expected==inventory(a.artifacts/'build/atlas_engine')==inventory(a.artifacts/'build/build/lib/atlas_engine')
    with zipfile.ZipFile(wheel) as z:
        packaged={n.removeprefix('atlas_engine/'):sha(z.read(n)) for n in z.namelist() if n.startswith('atlas_engine/') and not n.endswith('/')}
    assert expected==packaged
    assert sha(wheel.read_bytes())==build['sha256']
    assert wheel.read_bytes()==(a.artifacts/'repro'/wheel.name).read_bytes()
    installed=json.loads(run([build['fixture_python'],'-I','-B','-c',PROBE]))
    assert installed['files']==expected and installed['version']=='0.4.0.dev19'
    assert Path(installed['root']).is_relative_to(Path(build['fixture_python']).parent.parent)
    helper_probe='''import hashlib,json
from pathlib import Path
paths=['/opt/atlas/ssh-agent.py','/opt/atlas-media/init.py']
print(json.dumps(dict(helpers={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths if Path(p).is_file()},wheels={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('/opt/atlas/ssh-release').glob('*.whl')})))
'''
    records={}
    bases={'core':'sha256:e821ca4dc3ebcfff8ca0758b49060f23736c4052a0fa5d49786d125e3814a25e','media':'sha256:d922ec0fa8e22c0c9e0e4acd64ce0736e773175eb20cfdc1e7fa2490672de5a4'}
    for kind,image in [('core',a.core),('media',a.media)]:
        info=json.loads(run(['docker','image','inspect',image]))[0]
        base=json.loads(run(['docker','image','inspect',bases[kind]]))[0]
        assert info['Id']==image and info['Config']['User']=='10001:10001'
        assert info['RootFS']['Layers'][:len(base['RootFS']['Layers'])]==base['RootFS']['Layers']
        def probe(image,code):return json.loads(run(['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--tmpfs','/var/lib/atlas:rw,noexec,nosuid,size=1m','--entrypoint','python',image,'-I','-B','-c',code]))
        package=probe(image,PROBE);extras=probe(image,helper_probe);old=probe(bases[kind],helper_probe)
        assert package['files']==expected and package['version']=='0.4.0.dev19'
        assert extras['helpers']==old['helpers']
        assert extras['helpers']['/opt/atlas/ssh-agent.py']==sha((a.source/'container/ssh-agent.py').read_bytes())
        if kind=='media':assert extras['helpers']['/opt/atlas-media/init.py']==sha((a.source/'container/init-media.py').read_bytes())
        assert extras['wheels']=={wheel.name:build['sha256']}
        records[kind]={'id':image,'base':bases[kind],'base_layers_unchanged':True,'package':package,**extras}
    helpers={str(x.relative_to(a.source)):sha(x.read_bytes()) for x in (a.source/'container').iterdir() if x.is_file()}
    result={'equal':True,'file_count':len(expected),'files':expected,'wheel_sha256':build['sha256'],'reproducible':True,'installed':installed,'images':records,'container_files':helpers,'scope':'complete non-bytecode package file sets and SHA256 source/stage/build/wheel/isolated-install/both images; exact embedded wheel; retained base layers and helper bytes'}
    a.out.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps({'equal':True,'file_count':len(expected),'wheel_sha256':build['sha256'],'receipt':str(a.out)}))


if __name__=='__main__':main()
