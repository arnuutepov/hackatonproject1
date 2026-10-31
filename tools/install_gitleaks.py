"""Install the verified Linux x64 Gitleaks release into an ignored tools directory."""
import hashlib
from pathlib import Path
import tarfile
from urllib.request import urlopen

VERSION='8.30.1'
SHA256='551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb'
URL='https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz'


def main():
    target=Path('outputs/roadmap/tools');target.mkdir(parents=True,exist_ok=True)
    with urlopen(URL,timeout=30) as response:data=response.read(20_000_001)
    if len(data)>20_000_000 or hashlib.sha256(data).hexdigest()!=SHA256:raise ValueError('Gitleaks checksum mismatch')
    archive=target/'gitleaks.tar.gz';archive.write_bytes(data)
    with tarfile.open(archive) as source:
        # Extract one known regular file; no archive path extraction.
        member=source.getmember('gitleaks')
        if not member.isfile():raise ValueError('Unexpected release member')
        (target/'gitleaks').write_bytes(source.extractfile(member).read())
    (target/'gitleaks').chmod(0o700)
    print('Verified Gitleaks '+VERSION)

if __name__=='__main__':main()
