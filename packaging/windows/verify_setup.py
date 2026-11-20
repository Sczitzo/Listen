"""Check setup recovery against small, disposable model files."""

import hashlib
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

import packaged_setup as setup


def verify():
    expected = b'example-model'
    expected_hash = hashlib.sha256(expected).hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        model = Path(tmp) / 'model.gguf'
        model.write_bytes(b'invalid-model')
        cancelled = threading.Event()

        def fake_download(url, path, limit, report, stop):
            path.write_bytes(expected)
            return len(expected), expected_hash

        with (patch.object(setup, 'model_path', return_value=model),
              patch.object(setup, 'MODEL_SIZE', len(expected)),
              patch.object(setup, 'MODEL_SHA256', expected_hash),
              patch.object(setup, 'download', side_effect=fake_download) as download):
            assert model.stat().st_size == len(expected)
            assert not setup.speech_model_ready()
            setup.prepare_model(lambda *args: None, cancelled)
            assert model.read_bytes() == expected and setup.speech_model_ready()
            download.assert_called_once()
            model.with_suffix('.verified').unlink()
            setup.prepare_model(lambda *args: None, cancelled)
            download.assert_called_once()
        assert not list(Path(tmp).glob('listen-download-*'))
    with (patch.object(setup, 'vc_runtime_ready', side_effect=[False, True]),
          patch.object(setup, 'webview_ready', side_effect=[False, True]),
          patch.object(setup, 'install_microsoft') as install,
          patch.object(setup, 'model_selected', return_value=False)):
        setup.prepare(lambda *args: None, threading.Event())
        assert [call.kwargs['administrator'] for call in install.call_args_list] == [True, False]


if __name__ == '__main__':
    verify()
    print('Setup repaired a damaged model of the expected size and reused a verified copy.')
