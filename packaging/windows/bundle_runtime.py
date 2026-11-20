"""Stage app-local Python and the pinned native speech runtime for the MSI."""

import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile


PYTHON_ARCHIVE = 'python-3.14.10-amd64.zip'
PYTHON_HASH = 'cc28026a650970cc1570bb127312f1f9634eb67abfe157ae6ca1acbbc627d091'
SPEECH_ARCHIVE = 'transcribe-native-0.4.2-windows-x86_64-cpu-vulkan.tar.gz'
SPEECH_HASH = '6094102cf57b404b050707ceb258b12ce647b92e801b5d46077214e5b030c8aa'


def verified_archive(cache, name, url, digest):
    path = cache / name
    if not path.exists():
        temporary = path.with_suffix('.part')
        try:
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open('wb') as output:
                shutil.copyfileobj(response, output)
            with temporary.open('rb') as source:
                if hashlib.file_digest(source, 'sha256').hexdigest() != digest:
                    raise ValueError(f'Checksum mismatch: {name}')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    with path.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != digest:
            raise ValueError(f'Checksum mismatch: {name}')
    return path


def stage_runtime(staging, build, cache, lock, requirements):
    runtime = verified_archive(cache, PYTHON_ARCHIVE,
        f'https://www.python.org/ftp/python/3.14.10/{PYTHON_ARCHIVE}', PYTHON_HASH)
    with zipfile.ZipFile(runtime) as archive:
        archive.extractall(build / 'vendor-python')
    vendor = build / 'vendor-python'
    private = staging / 'python'
    private.mkdir()
    for path in vendor.iterdir():
        if path.suffix in {'.exe', '.dll', '.txt'}:
            shutil.copyfile(path, private / path.name)
    shutil.copytree(vendor / 'DLLs', private / 'DLLs')
    shutil.copytree(vendor / 'Lib', private / 'Lib', ignore=shutil.ignore_patterns(
        'site-packages', '__pycache__', 'test', 'idlelib', 'ensurepip'))
    (private / 'python314._pth').write_text('.\nDLLs\nLib\nLib/site-packages\n..\nimport site\n', encoding='ascii')
    packages = private / 'Lib' / 'site-packages'
    # proxy_tools ships only as source. Build it with the exact reviewed setuptools wheel.
    resolver = [str(vendor / 'python.exe'), '-I', '-B', '-m', 'pip', '--isolated',
                '--disable-pip-version-check', '--no-cache-dir']
    lock_lines = lock.read_text(encoding='ascii').splitlines()
    build_pin = build / 'build-tool.txt'
    build_pin.write_text(next(line for line in lock_lines if line.startswith('setuptools==')) + '\n', encoding='ascii')
    subprocess.run([*resolver, 'install', '--require-hashes', '--no-deps', '--only-binary=:all:',
                    '-r', str(build_pin)], check=True)

    expected = {line.split('==', 1)[0].lower(): line.split()[0].lower()
                for line in lock_lines if '==' in line}
    for line in requirements.read_text(encoding='utf-8').splitlines():
        pin = line.partition('#')[0].strip().lower()
        if pin and not pin.startswith('nvidia-') and expected.get(pin.split('==')[0]) != pin:
            raise ValueError(f'Missing Windows lock entry for {pin}')
    subprocess.run([*resolver, 'install', '--require-hashes', '--no-build-isolation',
                    '--only-binary=:all:', '--no-binary=proxy_tools', '--no-compile',
                    '--target', str(packages), '-r', str(lock)], check=True)

    speech = verified_archive(cache, SPEECH_ARCHIVE,
        f'https://github.com/handy-computer/transcribe.cpp/releases/download/v0.4.2/{SPEECH_ARCHIVE}', SPEECH_HASH)
    with tarfile.open(speech) as archive:
        archive.extractall(build / 'speech', filter='data')
    unpacked = build / 'speech' / 'transcribe-native-windows-x86_64-cpu-vulkan'
    if not (unpacked / 'transcribe.dll').is_file():
        raise ValueError('Pinned speech runtime is incomplete')
    unpacked.rename(staging / 'speech-runtime')
