import ast
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch
from coppertop.image_check import (recipe_digest, pinned_dependencies,
    dependency_report, require_dependencies, require_native_interpreter)

ROOT=Path(__file__).resolve().parents[1]
class ImageRecipeTests(unittest.TestCase):
 def test_recipe_command_and_writable_offline_cache(self):
  recipe=(ROOT/'containers/Dockerfile.gpu-smoke').read_text()
  self.assertIn('ENTRYPOINT []',recipe)
  self.assertIn('CMD ["python", "-m", "coppertop.smoke_runtime"]',recipe)
  self.assertIn('HF_HOME=/tmp/coppertop-hf',recipe)
  self.assertIn('--no-deps -r requirements-gpu-smoke.lock',recipe)
  self.assertIn('python -m pip --isolated check',recipe)
  self.assertIn('--only-binary=:all:',recipe)
  self.assertIn('-r requirements-r2.txt',recipe)
  self.assertIn('sha256sum -c - && python -m pip install',recipe)
 def test_receipt_hash_changes_with_source(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'containers').mkdir();(root/'coppertop').mkdir()
   for name in ('containers/Dockerfile.gpu-smoke','containers/gpu-smoke-pins.json','requirements-gpu-smoke.lock','coppertop/runtime.py'):
    (root/name).write_text('pinned')
   old=recipe_digest(root);(root/'coppertop/runtime.py').write_text('changed')
   self.assertNotEqual(old,recipe_digest(root))
 def test_cpu_gate_never_constructs_model_or_cuda_tensors(self):
  tree=ast.parse((ROOT/'coppertop/image_check.py').read_text())
  calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call)]
  self.assertFalse(any(isinstance(n.func,ast.Attribute) and n.func.attr in ('cuda','to') for n in calls))
  self.assertFalse(any('AutoModel' in ast.unparse(n.func) for n in calls))

class DependencyTests(unittest.TestCase):
 def test_build_gate_interpreter_must_be_the_gpu_interpreter(self):
  with patch('coppertop.image_check.os.path.samefile',return_value=True), \
   patch('coppertop.image_check.sys.prefix','/opt/conda'), \
   patch('coppertop.image_check.sys.exec_prefix','/opt/conda'):
   require_native_interpreter()
  with patch('coppertop.image_check.os.path.samefile',return_value=True), \
   patch('coppertop.image_check.sys.prefix','/different-venv'):
   with self.assertRaises(ValueError):require_native_interpreter()
  for result in (False,OSError('absent')):
   options={'side_effect':result} if isinstance(result,Exception) else {'return_value':result}
   with patch('coppertop.image_check.os.path.samefile',**options):
    with self.assertRaisesRegex(ValueError,'native GPU interpreter'):require_native_interpreter()

 def test_all_version_differences_missing_and_unknown_are_reported_together(self):
  expected={'accelerate':'1.11.0','av':'15.1.0','pyarrow':'20.0.0','urllib3':'2.8.0'}
  report=dependency_report(expected,{'accelerate':'1.15.0','av':None,'urllib3':'2.5.0'})
  self.assertEqual(report['matched_count'],0)
  self.assertEqual({r['package']:r['status'] for r in report['differences']},
   {'accelerate':'different','av':'missing','pyarrow':'unknown','urllib3':'different'})
  with patch('coppertop.image_check.installed_dependencies',return_value={name:None for name in expected}):
   with self.assertRaises(ValueError) as caught:require_dependencies(expected)
  actual=json.loads(str(caught.exception).split(': ',1)[1])
  self.assertEqual(len(actual['differences']),4)

 def test_merges_normalized_pins_and_reports_every_conflict(self):
  with tempfile.TemporaryDirectory() as d:
   a,b=Path(d)/'native.lock',Path(d)/'sdk.lock'
   a.write_text('typing_extensions==4.16.0\nurllib3==2.8.0\n')
   b.write_text('typing-extensions==4.16.0\nboto3==1.43.108\n')
   self.assertEqual(pinned_dependencies(a,b),
    {'boto3':'1.43.108','typing-extensions':'4.16.0','urllib3':'2.8.0'})
   b.write_text('typing-extensions==4.14.1\nurllib3==2.5.0\n')
   with self.assertRaises(ValueError) as caught:pinned_dependencies(a,b)
   conflicts=json.loads(str(caught.exception).split(': ',1)[1])
   self.assertEqual({r['package'] for r in conflicts},{'typing-extensions','urllib3'})

 def test_actual_repo_reports_the_whole_historical_seed_before_cloud_access(self):
  from coppertop.modal_image import runtime_dependencies, SEED_IMAGE_ID
  expected=runtime_dependencies(ROOT)
  inventory=json.loads((ROOT/'containers/gpu-smoke-seed-runtime.json').read_text())
  self.assertEqual(inventory['seed_image_id'],SEED_IMAGE_ID)
  self.assertEqual(len(expected),30)
  report=dependency_report(expected,inventory['versions'])
  statuses=[r['status'] for r in report['differences']]
  self.assertEqual(statuses.count('different'),18)
  self.assertEqual(statuses.count('missing'),2)
  self.assertEqual(statuses.count('unknown'),6)
  self.assertEqual(report['matched_count'],4)

 def test_lock_rejects_resolver_options_and_unbounded_input(self):
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'lock'
   for contents in ('--extra-index-url https://example.invalid\n','torch>=2.8\n','a'*65537):
    path.write_text(contents)
    with self.assertRaises(ValueError):pinned_dependencies(path)
