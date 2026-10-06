import ast
import tempfile
import unittest
from pathlib import Path
from coppertop.image_check import recipe_digest

ROOT=Path(__file__).resolve().parents[1]
class ImageRecipeTests(unittest.TestCase):
 def test_recipe_command_and_writable_offline_cache(self):
  recipe=(ROOT/'containers/Dockerfile.gpu-smoke').read_text()
  self.assertIn('ENTRYPOINT []',recipe)
  self.assertIn('CMD ["python", "-m", "coppertop.smoke_runtime"]',recipe)
  self.assertIn('HF_HOME=/tmp/coppertop-hf',recipe)
  self.assertIn('--no-deps -r requirements-gpu-smoke.lock',recipe)
  self.assertIn('python -m pip check',recipe)
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
