import tempfile
import unittest
from pathlib import Path
from coppertop.chords import canonical, object_path
from coppertop.cloud_stage import StageManifest
from coppertop.cloud_smoke import train_command, reload_command, output_manifest, ARTIFACTS
from coppertop.gpu_smoke import RELEASE, MODEL_REPO, MODEL_REVISION
from coppertop.streams import ObjectRef, file_ref


class NativeCloudContractTests(unittest.TestCase):
    def inputs(self):return StageManifest('run',RELEASE,{object_path(RELEASE):ObjectRef(RELEASE,1)})

    def fixture(self,directory):
        (directory/'head.safetensors').write_bytes(b'head')
        (directory/'validation.safetensors').write_bytes(b'case')
        head=file_ref(directory/'head.safetensors');case=file_ref(directory/'validation.safetensors')
        fields={'release':RELEASE,'checkpoint_sha256':head.sha256,'checkpoint_bytes':head.size,
                'validation_sha256':case.sha256,'validation_bytes':case.size}
        (directory/'result.json').write_bytes(canonical(dict(fields,status='passed')))
        (directory/'head-manifest.json').write_bytes(canonical(dict(fields,base_model=MODEL_REPO,revision=MODEL_REVISION)))

    def test_fixed_pinned_commands_use_volume_paths_and_distinct_native_limits(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);self.fixture(directory)
            inputs=self.inputs();checkpoint=output_manifest(inputs,directory)
            train=train_command(inputs,Path('/inputs'),Path('/outputs/train'))
            reload=reload_command(inputs,checkpoint,Path('/inputs'),Path('/weights'),Path('/outputs/reload'))
            self.assertEqual(train[:3],['/opt/conda/bin/python','-m','coppertop.smoke_runtime'])
            self.assertEqual(train[-2:],['--max-seconds','480'])
            self.assertEqual(reload[-2:],['--max-seconds','120'])
            self.assertIn('/weights/head.safetensors',reload)
            self.assertEqual(set(checkpoint.objects),set(ARTIFACTS))
            self.assertNotIn('R2',str(train));self.assertNotIn('secret',str(reload))

    def test_corrupt_generated_weights_cannot_be_committed(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);self.fixture(directory)
            (directory/'head.safetensors').write_bytes(b'wrong')
            with self.assertRaises(ValueError):output_manifest(self.inputs(),directory)

    def test_wrong_release_and_missing_case_rejected(self):
        bad=StageManifest('run','a'*64,{'signal':ObjectRef('b'*64,1)})
        with self.assertRaises(ValueError):train_command(bad,Path('/in'),Path('/out'))
        checkpoint=StageManifest('run',RELEASE,{'head.safetensors':ObjectRef('b'*64,1)})
        with self.assertRaises(ValueError):reload_command(self.inputs(),checkpoint,Path('/in'),Path('/head'),Path('/out'))
