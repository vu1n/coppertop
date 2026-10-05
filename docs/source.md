# Source attribution

NVIDIA, PhysicalAI-Robotics-Manipulation-Kitchen-Demos, human-pretrain CloseBlenderLid episode 0. [Pinned dataset and card](https://huggingface.co/datasets/nvidia/PhysicalAI-Robotics-Manipulation-Kitchen-Demos/tree/522e4ffa3bb9d9729f79b241c65fc9e589f2659c).

Revision: `522e4ffa3bb9d9729f79b241c65fc9e589f2659c`. Logical dataset: `pretrain/atomic/CloseBlenderLid/20250822/lerobot`. The pinned dataset card declares **CC-BY-4.0**. Preserve this attribution and license with shared dataset releases. Acquired source objects remain byte-identical; exported signals reorder canonical actions back to the source layout and regenerate bounded collection metadata. See `source-files.json` for all 13 exact source URLs, lengths and SHA256 hashes (1,133,145 bytes). The acquisition manifest is metadata, not a dataset download command.

Only source-compatible `episode.py` and `adapter.py` logic was reused from the earlier local training-data milestone; model-specific state conversion was moved into `xiaomi.py`. No internal reports, experiment artifacts, model weights or unrelated source were copied.
