import copy
import tempfile
import unittest
from pathlib import Path
from coppertop.gpu_smoke import window, normalized_targets, conditioning, flow_loss, velocity, trainable_head, HEAD_MODULES
from coppertop.smoke_runtime import verify_snapshot


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.rows=[{'observation.state':[0.]*16,'action':[float(d+i) for d in range(12)],
                    'timestamp':i/20} for i in range(10)]
        self.record={'length':10,'episode_id':'source@revision','instruction':'Close lid',
            'action_layout':'EEtranslation3/EErotation3/gripper1/base4/mode1'}

    def test_causal_history_and_target_boundary(self):
        start=window(self.rows,self.record,0)
        self.assertEqual(start['history_indices'],[0,0,0,0])
        self.assertEqual(start['history_valid'],[False,False,False,True])
        tail=window(self.rows,self.record,9)
        self.assertEqual(tail['history_indices'],[3,5,7,9])
        self.assertEqual(sum(map(sum,tail['target_valid'])),12)
        self.assertEqual(tail['actions_native'][1],[0.]*60)
        self.assertEqual(tail['actions_native'][0][:12],self.rows[9]['action'])
        self.assertIsNone(tail['success'])

    def test_normalization_uses_active_and_observed_masks(self):
        sample=window(self.rows,self.record,9)
        config={'mean':[[2.]*60 for _ in range(16)],'std':[[2.]*60 for _ in range(16)]}
        config['std'][0][11]=0
        values,mask=normalized_targets(sample,config)
        self.assertEqual(values[0][0],3.5)
        self.assertEqual(values[0][11],0.)
        self.assertFalse(mask[0][11]);self.assertFalse(any(mask[1]))
        self.assertEqual(sum(map(sum,mask)),11)
        self.assertEqual(values[1],[0.]*60)

    def test_rejects_layout_and_invalid_statistics(self):
        self.record['action_layout']='washer'
        with self.assertRaises(ValueError):window(self.rows,self.record,0)
        config={'mean':[[0.]*60 for _ in range(16)],'std':[[0.]*60 for _ in range(16)]}
        self.record['action_layout']='EEtranslation3/EErotation3/gripper1/base4/mode1'
        with self.assertRaises(ValueError):normalized_targets(window(self.rows,self.record,0),config)

    def test_view_does_not_modify_canonical_rows(self):
        before=copy.deepcopy(self.rows);window(self.rows,self.record,6)
        self.assertEqual(self.rows,before)

    def test_snapshot_hash_rejects_same_size_corruption(self):
        from coppertop.gpu_smoke import MODEL_REPO,MODEL_REVISION
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);data=b'native code'
            (root/'model.py').write_bytes(data)
            pins={'repo':MODEL_REPO,'revision':MODEL_REVISION,'files':[{'name':'model.py',
                'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}]}
            verify_snapshot(root,pins);(root/'model.py').write_bytes(b'x'*len(data))
            with self.assertRaises(ValueError):verify_snapshot(root,pins)


try:
    import torch
except ImportError:
    torch=None


@unittest.skipIf(torch is None,'Existing optional CPU torch runtime unavailable')
class DifferentiationTests(unittest.TestCase):
    def test_masked_velocity_loss_backward_update_and_reload(self):
        # Tiny protocol double verifies our bridge, not native model/processor parity.
        class Tiny(torch.nn.Module):
            def __init__(self):
                super().__init__();self.vlm=torch.nn.Linear(4,4)
                for name in HEAD_MODULES:setattr(self,name,torch.nn.Linear(4,4))
            def dit_forward(self,noisy_action,t,action_mask,state_embed,**context):
                return self.action_output_layer(self.dit(self.action_projector(noisy_action*action_mask)+state_embed))
        torch.manual_seed(7);model=Tiny();params=trainable_head(model)
        inputs={'state':torch.ones(1,2,4),'action_mask':torch.ones(1,2,4)}
        noise=torch.ones(1,2,4);t=torch.ones(1,1,1)*.5
        target=torch.zeros_like(noise);mask=torch.zeros_like(noise,dtype=torch.bool);mask[0,0,:2]=True
        pred=velocity(model,inputs,noise,t,{})
        changed=target.clone();changed[~mask]=float('nan')
        self.assertEqual(flow_loss(pred,target,mask),flow_loss(pred,changed,mask))
        before=model.action_output_layer.weight.detach().clone()
        loss=flow_loss(pred,target,mask);loss.backward()
        self.assertTrue(torch.isfinite(model.state_projector.weight.grad).all())
        self.assertGreater(model.state_projector.weight.grad.abs().sum(),0)
        self.assertIsNone(model.vlm.weight.grad)
        torch.optim.SGD(params,lr=.01).step()
        self.assertFalse(torch.equal(before,model.action_output_layer.weight))
        saved={k:v.clone() for k,v in model.state_dict().items()}
        expected=velocity(model,inputs,noise,t,{}).detach()
        with torch.no_grad():model.action_output_layer.weight.zero_()
        model.load_state_dict(saved)
        self.assertTrue(torch.equal(expected,velocity(model,inputs,noise,t,{})))

    def test_native_positions_cache_mask_and_video_inputs(self):
        from types import SimpleNamespace
        captured={}
        class VLM:
            def __call__(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(position_ids=torch.tensor([[[3,7]],[[3,7]],[[3,7]]]),
                    attention_mask=torch.tensor([[1,0]]),past_key_values='native-cache')
        def rotary(mask, positions):
            captured['positions']=positions
            return positions
        model=SimpleNamespace(vlm=VLM(),rotary_emb=rotary)
        inputs={'state':torch.zeros(1,4,60),'action_mask':torch.ones(1,16,60),
            'pixel_values_videos':torch.ones(2,3),'video_grid_thw':torch.ones(3,3),
            'input_ids':torch.ones(1,2,dtype=torch.long),'attention_mask':torch.tensor([[1,0]])}
        result=conditioning(model,inputs)
        self.assertTrue(torch.equal(captured['positions'][0,0],torch.arange(8,29)))
        self.assertIs(captured['pixel_values_videos'],inputs['pixel_values_videos'])
        self.assertIs(captured['video_grid_thw'],inputs['video_grid_thw'])
        self.assertNotIn('state',captured)
        self.assertEqual(result['attn_mask'].shape,(1,1,21,23))
        self.assertTrue(result['attn_mask'][0,0,:,0].all())
        self.assertFalse(result['attn_mask'][0,0,:,1].any())
        self.assertTrue(torch.equal(result['attn_mask'][0,0,:,2:],torch.ones(21,21,dtype=torch.bool).tril()))
