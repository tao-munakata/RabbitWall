#!/usr/bin/env python3
"""Build a source-only public archive; never package the workspace wholesale."""
import argparse
import hashlib
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'tools/release-files.txt'


def sources():
    for entry in MANIFEST.read_text().splitlines():
        entry=entry.strip()
        if not entry or entry.startswith('#'): continue
        relative=Path(entry)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError(f'Invalid release path: {entry}')
        path=ROOT/relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError(f'Invalid release source: {path}')
        yield path


def build(output):
    if output.exists(): raise ValueError(f'Refusing to overwrite {output}')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(output,'w:gz') as archive:
        for path in sources():
            info=archive.gettarinfo(str(path), 'rabbitwall/'+path.relative_to(ROOT).as_posix())
            info.uid=info.gid=0;info.uname=info.gname='';info.mode=0o644
            with path.open('rb') as content:archive.addfile(info,content)
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+'.sha256').write_text(digest+'  '+output.name+'\n')
    print(f'{output}\nSHA-256: {digest}')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=Path('output/rabbitwall.tar.gz'))
    args=parser.parse_args()
    try:build(args.output)
    except (OSError,ValueError) as error:parser.exit(1,str(error)+'\n')
