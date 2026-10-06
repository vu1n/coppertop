import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from coppertop.smoke_reload import (MAX_VALIDATION_BYTES, compare_velocity,
    load_head, load_verified_artifacts, validate_case, verify_artifact)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.head = self.root / 'head.safetensors'
        self.case = self.root / 'validation.safetensors'
        self.head.write_bytes(b'weights fixture')
        self.case.write_bytes(b'validation fixture')
        self.head_hash = hashlib.sha256(self.head.read_bytes()).hexdigest()
        self.case_hash = hashlib.sha256(self.case.read_bytes()).hexdigest()
        self.loader = Mock(side_effect=[{'head': 'parsed'}, {'case': 'parsed'}])
        self.modules = {'safetensors': Mock(), 'safetensors.torch': Mock(load_file=self.loader)}

    def load(self, **overrides):
        args = dict(head=self.head, validation=self.case,
            checkpoint_sha256=self.head_hash, validation_sha256=self.case_hash,
            checkpoint_bytes=self.head.stat().st_size, validation_bytes=self.case.stat().st_size)
        args.update(overrides)
        with patch.dict('sys.modules', self.modules):
            return load_verified_artifacts(**args)

    def test_both_verified_before_loading(self):
        self.assertEqual(self.load(), ({'head': 'parsed'}, {'case': 'parsed'}))
        self.assertEqual(self.loader.call_count, 2)
        self.assertEqual(self.loader.call_args_list[0].kwargs, {'device': 'cpu'})

    def test_corrupt_validation_prevents_even_head_loading(self):
        self.case.write_bytes(b'VALIDATION fixture')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.load()
        self.loader.assert_not_called()

    def test_corrupt_head_prevents_loading(self):
        self.head.write_bytes(b'WEIGHTS fixture')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.load()
        self.loader.assert_not_called()

    def test_wrong_size_prevents_loading(self):
        for key in ('checkpoint_bytes', 'validation_bytes'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'size'):
                self.load(**{key: 1})
        self.loader.assert_not_called()

    def test_validation_cap_prevents_loading(self):
        with self.assertRaisesRegex(ValueError, 'bound'):
            self.load(validation_bytes=MAX_VALIDATION_BYTES + 1)
        self.loader.assert_not_called()

    def test_invalid_hash_prevents_loading(self):
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            self.load(validation_sha256='not-a-digest')
        self.loader.assert_not_called()

    def test_symlink_rejected(self):
        link = self.root / 'link'
        link.symlink_to(self.case)
        with self.assertRaisesRegex(ValueError, 'path/size'):
            self.load(validation=link)
        self.loader.assert_not_called()

    def test_hash_and_bytes_returned(self):
        self.assertEqual(verify_artifact(self.case, self.case_hash,
            self.case.stat().st_size, MAX_VALIDATION_BYTES),
            {'sha256': self.case_hash, 'bytes': self.case.stat().st_size})


try:
    import torch
except ImportError:
    torch = None


@unittest.skipIf(torch is None, 'Existing optional CPU torch runtime unavailable')
class ComparisonTests(unittest.TestCase):
    def test_identical_tiny_tensors_report_zero(self):
        tensor = torch.tensor([[[1., -2.]]], dtype=torch.bfloat16)
        result = compare_velocity(tensor, tensor.clone())
        self.assertTrue(result['velocity_exact'])
        self.assertEqual(result['velocity_max_abs_difference'], 0.)
        self.assertEqual(result['velocity_shape'], [1, 1, 2])

    def test_difference_rejected(self):
        with self.assertRaisesRegex(ValueError, 'changed velocity'):
            compare_velocity(torch.tensor([1.]), torch.tensor([1.001]))

    def test_shape_dtype_and_nonfinite_rejected(self):
        for left, right in ((torch.ones(2), torch.ones(1, 2)),
            (torch.ones(2), torch.ones(2, dtype=torch.bfloat16)),
            (torch.tensor([float('nan')]), torch.tensor([float('nan')])),
            (torch.tensor([float('inf')]), torch.tensor([float('inf')]))):
            with self.subTest(left=left, right=right), self.assertRaises(ValueError):
                compare_velocity(left, right)

    def test_validation_case_geometry_and_timestep(self):
        case = {'noisy': torch.ones(1, 16, 60, dtype=torch.bfloat16),
            't': torch.full((1, 1, 1), .5, dtype=torch.bfloat16),
            'expected_velocity': torch.ones(1, 16, 60, dtype=torch.bfloat16)}
        validate_case(case)
        case['t'].fill_(.25)
        with self.assertRaisesRegex(ValueError, 'timestep'):
            validate_case(case)

    def test_head_mapping_rejects_extra_or_missing_keys(self):
        class Tiny(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.vlm = torch.nn.Linear(2, 2)
                self.dit = torch.nn.Linear(2, 2)
        model = Tiny()
        head = {k: v.clone() for k, v in model.state_dict().items() if k.startswith('dit.')}
        load_head(model, head)
        for bad in ({'dit.weight': head['dit.weight']}, dict(head, **{'vlm.weight': model.vlm.weight})):
            with self.assertRaisesRegex(ValueError, 'keys'):
                load_head(model, bad)
        head['dit.weight'] = head['dit.weight'].double()
        with self.assertRaisesRegex(ValueError, 'dtype/shape'):
            load_head(model, head)


if __name__ == '__main__':
    unittest.main()
