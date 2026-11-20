"""Build Listen's per-user Windows MSI from an explicit Git revision."""

import argparse
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from bundle_runtime import stage_runtime


def run(*arguments):
    subprocess.run(arguments, check=True)


def git(repo, *arguments):
    return subprocess.check_output(['git', '-c', f'safe.directory={repo}', '-C', str(repo), *arguments])


def installer_id(name):
    return '{' + str(uuid.uuid5(uuid.NAMESPACE_URL, 'https://github.com/Sczitzo/Listen/windows/' + name)).upper() + '}'


class InstallerDatabase:
    def __init__(self, path):
        api = c.WinDLL('msi')
        self.open = self.function(api, 'MsiOpenDatabaseW', w.LPCWSTR, w.LPCWSTR, c.POINTER(w.UINT))
        self.import_table = self.function(api, 'MsiDatabaseImportW', w.UINT, w.LPCWSTR, w.LPCWSTR)
        self.open_view = self.function(api, 'MsiDatabaseOpenViewW', w.UINT, w.LPCWSTR, c.POINTER(w.UINT))
        self.record = api.MsiCreateRecord
        self.record.argtypes, self.record.restype = [w.UINT], w.UINT
        self.set_string = self.function(api, 'MsiRecordSetStringW', w.UINT, w.UINT, w.LPCWSTR)
        self.set_stream = self.function(api, 'MsiRecordSetStreamW', w.UINT, w.UINT, w.LPCWSTR)
        self.execute = self.function(api, 'MsiViewExecute', w.UINT, w.UINT)
        self.open_summary = self.function(api, 'MsiGetSummaryInformationW', w.UINT, w.LPCWSTR, w.UINT, c.POINTER(w.UINT))
        self.set_summary = self.function(api, 'MsiSummaryInfoSetPropertyW', w.UINT, w.UINT, w.UINT, c.c_int, c.c_void_p, w.LPCWSTR)
        self.persist = self.function(api, 'MsiSummaryInfoPersist', w.UINT)
        self.commit = self.function(api, 'MsiDatabaseCommit', w.UINT)
        self.close = self.function(api, 'MsiCloseHandle', w.UINT)
        self.handle = w.UINT()
        self.check(self.open(str(path), c.cast(c.c_void_p(3), w.LPCWSTR), c.byref(self.handle)), 'create database')

    @staticmethod
    def function(api, name, *arguments):
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, w.UINT
        return function

    @staticmethod
    def check(result, action):
        if result:
            raise OSError(f'{action}: Windows Installer returned {result}')

    def table(self, folder, name, columns, types, key, rows):
        path = folder / (name + '.idt')
        fields = [columns.split(), types.split(), [name, *key.split()], *rows]
        path.write_bytes(('\r\n'.join('\t'.join(map(str, row)) for row in fields) + '\r\n').encode('mbcs'))
        self.check(self.import_table(self.handle, str(folder), path.name), 'import ' + name)

    def stream(self, name, path):
        view, record = w.UINT(), self.record(2)
        try:
            self.check(self.open_view(self.handle, 'INSERT INTO `_Streams` (`Name`, `Data`) VALUES (?, ?)', c.byref(view)), 'open cabinet stream')
            self.check(self.set_string(record, 1, name), 'name cabinet stream')
            self.check(self.set_stream(record, 2, str(path)), 'read cabinet')
            self.check(self.execute(view, record), 'embed cabinet')
        finally:
            self.close(record)
            if view.value:
                self.close(view)

    def finish(self):
        summary = w.UINT()
        self.check(self.open_summary(self.handle, None, 10, c.byref(summary)), 'open summary')
        try:
            properties = {2: 'Listen Windows installer', 3: 'Listen', 4: 'Sczitzo', 7: 'x64;1033',
                          9: '{' + str(uuid.uuid4()).upper() + '}', 14: 500, 15: 10}
            for key, value in properties.items():
                string = isinstance(value, str)
                self.check(self.set_summary(summary, key, 30 if string else 3, 0 if string else value,
                                            None, value if string else None), 'set summary')
            self.check(self.persist(summary), 'save summary')
            self.check(self.commit(self.handle), 'commit database')
        finally:
            self.close(summary)


def payload(repo, revision, staging):
    names = git(repo, 'ls-tree', '-r', '--name-only', revision).decode().splitlines()
    selected = [name for name in names if
                ('/' not in name and name.endswith('.py')) or name.startswith('ui/') or
                name in {'launch_listen.pyw', 'requirements.txt', 'LICENSE', 'README.md'}]
    selected.append('packaging/windows/packaged_setup.py')
    for name in selected:
        destination = staging / (Path(name).name if name.startswith('packaging/') else name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(git(repo, 'show', f'{revision}:{name}'))


def build(repo, revision, version, output):
    packaging = Path(__file__).resolve().parent
    compiler = Path('C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe')
    with tempfile.TemporaryDirectory(prefix='listen-msi-', dir=output.parent) as build_dir:
        folder = Path(build_dir).resolve()
        assert folder.parent == output.parent.resolve()
        staging = folder / 'files'
        staging.mkdir()
        payload(repo, revision, staging)
        lock = folder / 'requirements.lock'
        lock.write_bytes(git(repo, 'show', f'{revision}:packaging/windows/requirements.lock'))
        stage_runtime(staging, folder, output.parent, lock, staging / 'requirements.txt')
        names = sorted(path.relative_to(staging).as_posix() for path in staging.rglob('*') if path.is_file())
        names = [name for name in names if not name.startswith('python/Lib/site-packages/bin/')]
        run(str(compiler), '/nologo', '/target:winexe', '/platform:x64',
            '/reference:System.Windows.Forms.dll', f'/win32icon:{staging / "ui/app.ico"}',
            f'/out:{staging / "Listen.exe"}', str(packaging / 'ListenLauncher.cs'))
        names.append('Listen.exe')
        directories = {Path('.'): 'INSTALLDIR', Path('ui'): 'ListenUi'}
        for name in names:
            for parent in reversed(Path(name).parents):
                if parent not in directories:
                    directories[parent] = 'D' + uuid.uuid5(uuid.NAMESPACE_URL, parent.as_posix()).hex
        files, cabinet = [], []
        for sequence, name in enumerate(names, 1):
            path = staging / name
            key = f'F{sequence:04}'
            # Some upstream tar members have a Unix epoch timestamp. CAB uses DOS dates.
            if path.stat().st_mtime < 315619200:
                os.utime(path, (315619200, 315619200))
            short = key + path.suffix[:4]
            directory = directories[Path(name).parent]
            files.append((key, directory, short + '|' + path.name, path.stat().st_size, '', '', 512, sequence))
            cabinet.append(f'"{path}" {key}')
        cabinet_path = folder / 'listen.cab'
        descriptor = folder / 'listen.ddf'
        descriptor.write_bytes(('\r\n'.join([f'.Set DiskDirectoryTemplate="{folder}"', '.Set CabinetNameTemplate=listen.cab',
                                '.Set Cabinet=on', '.Set Compress=on', '.Set CompressionType=MSZIP',
                                '.Set MaxDiskSize=0', '.Set InfFileName=nul', '.Set RptFileName=nul', *cabinet]) + '\r\n').encode('mbcs'))
        run('makecab.exe', '/F', str(descriptor))

        db = InstallerDatabase(folder / 'listen.msi')
        def table(name, columns, types, key, rows):
            db.table(folder, name, columns, types, key, rows)

        try:
            properties = {'ProductCode': installer_id('product-' + version), 'UpgradeCode': installer_id('upgrade'),
                          'ProductVersion': version, 'ProductLanguage': 1033, 'ProductName': 'Listen', 'Manufacturer': 'Sczitzo',
                          'ARPURLINFOABOUT': 'https://github.com/Sczitzo/Listen', 'ARPNOMODIFY': 1,
                          'SecureCustomProperties': 'LISTENOLDER;LISTENNEWER'}
            table('Property', 'Property Value', 's72 l0', 'Property', list(properties.items()))
            table('Directory', 'Directory Directory_Parent DefaultDir', 's72 S72 l255', 'Directory', [
                ('TARGETDIR', '', 'SourceDir'), ('LocalAppDataFolder', 'TARGETDIR', '.'),
                ('ListenPrograms', 'LocalAppDataFolder', 'Programs'), ('INSTALLDIR', 'ListenPrograms', 'Listen'),
                ('ProgramMenuFolder', 'TARGETDIR', '.'),
                ('LISTENMENUDIR', 'ProgramMenuFolder', '.'),
                *((identifier, directories[path.parent], path.name)
                  for path, identifier in directories.items() if path != Path('.')),
            ])
            components = [*directories.values(), 'ListenMenu']
            table('Component', 'Component ComponentId Directory_ Attributes Condition KeyPath', 's72 S38 s72 i2 S255 S72', 'Component',
                  [(key, installer_id(key), key if key != 'ListenMenu' else 'LISTENMENUDIR', 4, '', 'Key' + key) for key in components])
            table('Registry', 'Registry Root Key Name Value Component_', 's72 i2 l255 L255 L0 s72', 'Registry',
                  [('Key' + key, 1, 'Software\\Listen\\Installer', key,
                    '[LISTENMENUDIR]' if key == 'ListenMenu' else f'[{key}]', key) for key in components])
            table('RegLocator', 'Signature_ Root Key Name Type', 's72 i2 s255 S255 I2', 'Signature_', [
                ('InstalledAppDirectory', 1, 'Software\\Listen\\Installer', 'INSTALLDIR', 2),
                ('InstalledMenuDirectory', 1, 'Software\\Listen\\Installer', 'ListenMenu', 2),
            ])
            table('AppSearch', 'Property Signature_', 's72 s72', 'Property Signature_', [
                ('INSTALLDIR', 'InstalledAppDirectory'), ('LISTENMENUDIR', 'InstalledMenuDirectory'),
            ])
            table('Signature', 'Signature FileName MinVersion MaxVersion MinSize MaxSize MinDate MaxDate Languages',
                  's72 s255 S20 S20 I4 I4 I4 I4 S255', 'Signature', [])
            table('Upgrade', 'UpgradeCode VersionMin VersionMax Language Attributes Remove ActionProperty',
                  's38 S20 S20 S255 i4 S255 s72', 'UpgradeCode VersionMin VersionMax Language Attributes', [
                      (properties['UpgradeCode'], '', version, '', 0, '', 'LISTENOLDER'),
                      (properties['UpgradeCode'], version, '', '', 2, '', 'LISTENNEWER'),
                  ])
            table('LaunchCondition', 'Condition Description', 's255 l255', 'Condition', [
                ('Installed OR NOT LISTENNEWER', 'A newer version of Listen is already installed.'),
            ])
            table('Feature', 'Feature Feature_Parent Title Description Display Level Directory_ Attributes', 's38 S38 L64 L255 I2 i2 S72 i2', 'Feature',
                  [('App', '', 'Listen', 'Windows dictation', 1, 1, 'INSTALLDIR', 0)])
            table('FeatureComponents', 'Feature_ Component_', 's38 s72', 'Feature_ Component_', [('App', key) for key in components])
            table('File', 'File Component_ FileName FileSize Version Language Attributes Sequence', 's72 s72 l255 i4 S72 S20 I2 i4', 'File', files)
            table('Media', 'DiskId LastSequence DiskPrompt Cabinet VolumeLabel Source', 'i2 i4 L64 S255 S32 S72', 'DiskId',
                  [(1, len(files), '', '#listen.cab', '', '')])
            table('Shortcut', 'Shortcut Directory_ Name Component_ Target Arguments Description Hotkey Icon_ IconIndex ShowCmd WkDir',
                  's72 s72 l128 s72 s72 S255 L255 I2 S72 I2 I2 S72', 'Shortcut',
                  [('ListenShortcut', 'LISTENMENUDIR', 'Listen', 'ListenMenu', f'[#{files[-1][0]}]', '', 'Windows local dictation', '', '', '', 1, 'INSTALLDIR')])
            actions = [('FindRelatedProducts', 25), ('AppSearch', 400), ('LaunchConditions', 500),
                       ('CostInitialize', 800), ('FileCost', 900), ('CostFinalize', 1000), ('InstallValidate', 1400),
                       ('InstallInitialize', 1500), ('RemoveExistingProducts', 1550),
                       ('ProcessComponents', 1600), ('UnpublishFeatures', 1800),
                       ('RemoveShortcuts', 3200), ('RemoveRegistryValues', 3300), ('RemoveFiles', 3500),
                       ('InstallFiles', 4000), ('CreateShortcuts', 4500), ('WriteRegistryValues', 5000),
                       ('RegisterUser', 6000), ('RegisterProduct', 6100), ('PublishFeatures', 6300),
                       ('PublishProduct', 6400), ('InstallFinalize', 6600)]
            table('InstallExecuteSequence', 'Action Condition Sequence', 's72 S255 I2', 'Action',
                  [(action, 'Installed OR LISTENOLDER' if action == 'AppSearch' else '', sequence) for action, sequence in actions])
            db.stream('listen.cab', cabinet_path)
            db.finish()
        finally:
            db.close(db.handle)
        shutil.copyfile(folder / 'listen.msi', output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--version', default='26.11.21', help='Windows Installer version (major.minor.build)')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.repo.resolve(), args.revision, args.version, args.output.resolve())
